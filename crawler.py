"""
Deople website metadata crawler.

Reads websites with crawl_status='pending', fetches their HTML metadata,
and writes results to the existing Cloudflare D1 websites table.

Required environment variables:
CF_ACCOUNT_ID, D1_DATABASE_ID, CF_API_TOKEN
"""

import asyncio
import ipaddress
import os
import socket
import time
from typing import Dict, Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from database import get_pending_crawl_websites, save_crawl_result

READ_LIMIT = int(os.getenv("CRAWL_READ_LIMIT", "100"))
CONCURRENCY = int(os.getenv("CRAWL_CONCURRENCY", "10"))
TIMEOUT = float(os.getenv("CRAWL_TIMEOUT", "15"))
MAX_HTML_SIZE = int(os.getenv("CRAWL_MAX_HTML_SIZE", str(2 * 1024 * 1024)))
MAX_REDIRECTS = 5

USER_AGENT = (
    "Mozilla/5.0 (compatible; DeopleBot/1.0; +https://deople.ai)"
)
semaphore = asyncio.Semaphore(max(1, CONCURRENCY))


def is_public_host(host: str) -> bool:
    """Reject private, loopback, link-local, reserved and non-public IPs."""
    try:
        addresses = socket.getaddrinfo(
            host, None, type=socket.SOCK_STREAM
        )
        if not addresses:
            return False

        for entry in addresses:
            ip = ipaddress.ip_address(entry[4][0])
            if not ip.is_global:
                return False
        return True
    except Exception:
        return False


def extract_metadata(html: str):
    soup = BeautifulSoup(html, "html.parser")

    title = soup.title.get_text(" ", strip=True) if soup.title else ""

    description = ""
    desc_tag = soup.find(
        "meta", attrs={"name": lambda value: value and value.lower() == "description"}
    )
    if desc_tag:
        description = (desc_tag.get("content") or "").strip()

    if not description:
        og_desc = soup.find(
            "meta",
            attrs={"property": lambda value: value and value.lower() == "og:description"},
        )
        if og_desc:
            description = (og_desc.get("content") or "").strip()

    keywords = ""
    keywords_tag = soup.find(
        "meta", attrs={"name": lambda value: value and value.lower() == "keywords"}
    )
    if keywords_tag:
        keywords = (keywords_tag.get("content") or "").strip()

    return title[:500], description[:2000], keywords[:1000]


def normalize_start_url(row: Dict) -> str:
    candidate = row.get("final_url") or row.get("url")
    if not candidate:
        candidate = "https://" + str(row["domain"])
    parsed = urlparse(str(candidate))
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Invalid website URL")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing credentials are not allowed")
    if parsed.port and parsed.port not in (80, 443):
        raise ValueError("Non-standard ports are not allowed")
    return str(candidate)


async def fetch_website(client: httpx.AsyncClient, row: Dict) -> Dict:
    domain = str(row["domain"])
    result = {
        "domain": domain,
        "title": "",
        "description": "",
        "keywords": "",
        "http_status": None,
        "final_url": "",
        "content_type": "",
        "crawl_status": "failed",
    }

    async with semaphore:
        try:
            current_url = normalize_start_url(row)

            for _ in range(MAX_REDIRECTS + 1):
                parsed = urlparse(current_url)
                if parsed.scheme not in ("http", "https") or not parsed.hostname:
                    raise ValueError("Invalid redirect URL")
                if parsed.port and parsed.port not in (80, 443):
                    raise ValueError("Redirect to non-standard port blocked")

                safe = await asyncio.to_thread(is_public_host, parsed.hostname)
                if not safe:
                    result["crawl_status"] = "blocked"
                    raise ValueError("Host resolves to a non-public IP")

                async with client.stream(
                    "GET",
                    current_url,
                    headers={"User-Agent": USER_AGENT},
                    follow_redirects=False,
                ) as response:
                    result["http_status"] = response.status_code
                    result["final_url"] = str(response.url)
                    result["content_type"] = (
                        response.headers.get("content-type", "").split(";")[0].strip().lower()
                    )

                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            raise ValueError("Redirect response without Location")
                        current_url = urljoin(current_url, location)
                        continue

                    if not 200 <= response.status_code < 300:
                        raise ValueError(f"HTTP status {response.status_code}")

                    if "text/html" not in result["content_type"]:
                        raise ValueError("Response is not HTML")

                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        remaining = MAX_HTML_SIZE - len(body)
                        if remaining <= 0:
                            break
                        body.extend(chunk[:remaining])
                        if len(body) >= MAX_HTML_SIZE:
                            break

                    if not body:
                        raise ValueError("Empty HTML response")

                    encoding = response.encoding or "utf-8"
                    html = bytes(body).decode(encoding, errors="replace")
                    title, description, keywords = extract_metadata(html)

                    result.update({
                        "title": title,
                        "description": description,
                        "keywords": keywords,
                        "crawl_status": "success",
                    })
                    return result

            raise ValueError("Too many redirects")

        except httpx.TimeoutException:
            result["crawl_status"] = "failed"
            print(f"[TIMEOUT] {domain}")
        except (httpx.ConnectError, httpx.NetworkError) as exc:
            result["crawl_status"] = "failed"
            print(f"[NETWORK ERROR] {domain}: {str(exc)[:180]}")
        except Exception as exc:
            print(f"[CRAWL ERROR] {domain}: {str(exc)[:180]}")

        return result


async def main():
    total = 0
    succeeded = 0
    failed = 0

    limits = httpx.Limits(
        max_connections=max(1, CONCURRENCY),
        max_keepalive_connections=max(1, CONCURRENCY),
    )
    timeout = httpx.Timeout(TIMEOUT)

    async with httpx.AsyncClient(
        timeout=timeout,
        limits=limits,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        while True:
            rows = get_pending_crawl_websites(READ_LIMIT)
            if not rows:
                break

            results = await asyncio.gather(
                *(fetch_website(client, row) for row in rows)
            )

            for item in results:
                # Save each record separately so one bad row does not discard a batch.
                try:
                    save_crawl_result(item)
                    total += 1
                    if item["crawl_status"] == "success":
                        succeeded += 1
                    else:
                        failed += 1
                except Exception as exc:
                    failed += 1
                    print(
                        f"[D1 WRITE ERROR] {item['domain']}: "
                        f"{str(exc)[:250]}"
                    )

            print(
                f"[BATCH] processed={len(results)} "
                f"success={sum(x['crawl_status'] == 'success' for x in results)} "
                f"failed={sum(x['crawl_status'] != 'success' for x in results)}"
            )

    print("=" * 45)
    print("Crawler finished")
    print("Saved:", total)
    print("Success:", succeeded)
    print("Failed:", failed)


if __name__ == "__main__":
    asyncio.run(main())
