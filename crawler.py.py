
import os
import re
import time
import socket
import asyncio
import ipaddress

import requests
import httpx

from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup


# =====================================
# Cloudflare D1 配置
# =====================================

ACCOUNT_ID = os.environ["CF_ACCOUNT_ID"]
DATABASE_ID = os.environ["D1_DATABASE_ID"]
API_TOKEN = os.environ["CF_API_TOKEN"]

API_URL = (
    f"https://api.cloudflare.com/client/v4/accounts/"
    f"{ACCOUNT_ID}/d1/database/{DATABASE_ID}/query"
)

HEADERS = {
    "Authorization": f"Bearer {API_TOKEN}",
    "Content-Type": "application/json"
}


# =====================================
# 运行参数
# =====================================

READ_LIMIT = 100
CONCURRENCY = 20
WRITE_BATCH_SIZE = 10

TIMEOUT = 15
MAX_HTML_SIZE = 2 * 1024 * 1024

USER_AGENT = (
    "Mozilla/5.0 (compatible; DeopleBot/1.0; "
    "+https://deople.ai)"
)


# =====================================
# D1 数据库操作
# =====================================

def d1_query(sql, params=None):

    response = requests.post(
        API_URL,
        headers=HEADERS,
        json={
            "sql": sql,
            "params": params or []
        },
        timeout=60
    )

    response.raise_for_status()

    result = response.json()

    if not result.get("success"):
        raise Exception(result.get("errors"))

    return result["result"][0]


def get_pending_domains():

    result = d1_query(
        """
        SELECT domain
        FROM websites
        WHERE crawl_status = 'pending'
        ORDER BY first_seen ASC
        LIMIT ?
        """,
        [READ_LIMIT]
    )

    return [
        row["domain"]
        for row in result.get("results", [])
    ]


# =====================================
# 安全检查：阻止访问内网地址
# =====================================

def is_public_host(host):

    try:
        addresses = socket.getaddrinfo(
            host,
            None,
            type=socket.SOCK_STREAM
        )

        if not addresses:
            return False

        for item in addresses:

            ip = ipaddress.ip_address(
                item[4][0]
            )

            if not ip.is_global:
                return False

        return True

    except Exception:
        return False


# =====================================
# 提取 HTML 信息
# =====================================

def extract_metadata(html):

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    title = ""

    if soup.title:
        title = soup.title.get_text(
            " ",
            strip=True
        )

    description = ""

    description_tag = soup.find(
        "meta",
        attrs={"name": re.compile("^description$", re.I)}
    )

    if description_tag:
        description = (
            description_tag.get("content", "")
        ).strip()

    if not description:

        og = soup.find(
            "meta",
            attrs={"property": "og:description"}
        )

        if og:
            description = (
                og.get("content", "")
            ).strip()

    keywords = ""

    keywords_tag = soup.find(
        "meta",
        attrs={"name": re.compile("^keywords$", re.I)}
    )

    if keywords_tag:
        keywords = (
            keywords_tag.get("content", "")
        ).strip()

    # 限制字段长度
    title = title[:500]
    description = description[:2000]
    keywords = keywords[:1000]

    return title, description, keywords


# =====================================
# 下载网页
# =====================================

async def fetch_website(client, domain):

    result = {
        "domain": domain,
        "title": "",
        "description": "",
        "keywords": "",
        "http_status": None,
        "crawl_status": "http_error"
    }

    semaphore = fetch_semaphore

    async with semaphore:

        try:

            current_url = "https://" + domain

            for redirect_count in range(6):

                parsed = urlparse(current_url)

                if parsed.scheme not in ("http", "https"):
                    raise ValueError("Invalid URL scheme")

                if not parsed.hostname:
                    raise ValueError("Invalid hostname")

                # 禁止访问本机和内网地址
                safe = await asyncio.to_thread(
                    is_public_host,
                    parsed.hostname
                )

                if not safe:
                    raise ValueError("Blocked non-public host")

                async with client.stream(
                    "GET",
                    current_url,
                    headers={"User-Agent": USER_AGENT},
                    follow_redirects=False
                ) as response:

                    status = response.status_code

                    result["http_status"] = status

                    # 处理跳转
                    if status in (301, 302, 303, 307, 308):

                        location = response.headers.get(
                            "location"
                        )

                        if not location:
                            break

                        current_url = urljoin(
                            current_url,
                            location
                        )

                        continue

                    if status < 200 or status >= 300:

                        result["crawl_status"] = "http_error"
                        return result

                    content_type = response.headers.get(
                        "content-type", ""
                    ).lower()

                    if "text/html" not in content_type:

                        result["crawl_status"] = "empty_content"
                        return result

                    # 限制下载大小，避免超大网页
                    body = bytearray()

                    async for chunk in response.aiter_bytes():

                        body.extend(chunk)

                        if len(body) > MAX_HTML_SIZE:
                            break

                    if not body:

                        result["crawl_status"] = "empty_content"
                        return result

                    encoding = response.encoding or "utf-8"

                    html = bytes(body).decode(
                        encoding,
                        errors="replace"
                    )

                    title, description, keywords = (
                        extract_metadata(html)
                    )

                    if not title and not description:

                        result["crawl_status"] = "empty_content"
                        return result

                    result.update({
                        "title": title,
                        "description": description,
                        "keywords": keywords,
                        "crawl_status": "success"
                    })

                    return result

            result["crawl_status"] = "http_error"
            return result

        except httpx.TimeoutException:

            result["crawl_status"] = "timeout"

        except (httpx.ConnectError, httpx.NetworkError):

            result["crawl_status"] = "dns_failed"

        except Exception as e:

            print("错误:", domain, str(e)[:200])

            result["crawl_status"] = "http_error"

        return result


# =====================================
# 批量更新 D1
# =====================================

def save_results(results):

    if not results:
        return

    now = int(time.time())

    values = []
    params = []

    for item in results:

        values.append(
            "(?, ?, ?, ?, ?, ?, ?, ?)"
        )

        params.extend([
            item["domain"],
            item["title"],
            item["description"],
            item["keywords"],
            item["http_status"],
            item["crawl_status"],
            now,
            now
        ])

    sql = """
    INSERT INTO websites (
        domain,
        title,
        description,
        keywords,
        http_status,
        crawl_status,
        last_checked,
        updated_at
    )
    VALUES
    """ + ",".join(values) + """

    ON CONFLICT(domain) DO UPDATE SET
        title = excluded.title,
        description = excluded.description,
        keywords = excluded.keywords,
        http_status = excluded.http_status,
        crawl_status = excluded.crawl_status,
        last_checked = excluded.last_checked,
        updated_at = excluded.updated_at
    """

    d1_query(sql, params)


# =====================================
# 主程序
# =====================================

fetch_semaphore = asyncio.Semaphore(
    CONCURRENCY
)


async def main():

    total = 0

    limits = httpx.Limits(
        max_connections=CONCURRENCY,
        max_keepalive_connections=CONCURRENCY
    )

    timeout = httpx.Timeout(TIMEOUT)

    async with httpx.AsyncClient(
        timeout=timeout,
        limits=limits
    ) as client:

        while True:

            domains = get_pending_domains()

            if not domains:
                break

            tasks = [
                fetch_website(client, domain)
                for domain in domains
            ]

            results = await asyncio.gather(*tasks)

            # 分批写入 D1
            for i in range(
                0,
                len(results),
                WRITE_BATCH_SIZE
            ):

                batch = results[
                    i:i + WRITE_BATCH_SIZE
                ]

                save_results(batch)

            success = sum(
                1 for x in results
                if x["crawl_status"] == "success"
            )

            failed = len(results) - success

            total += len(results)

            print(
                f"本批: {len(results)}"
                f" | 成功: {success}"
                f" | 失败: {failed}"
                f" | 累计: {total}"
            )

    print("=" * 40)
    print("Crawler 完成")
    print("总检查数量:", total)


if __name__ == "__main__":
    asyncio.run(main())
