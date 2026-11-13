
import os
import time
import requests

# =====================================
# Cloudflare D1 配置
# =====================================

CF_ACCOUNT_ID = os.environ["CF_ACCOUNT_ID"]
D1_DATABASE_ID = os.environ["D1_DATABASE_ID"]
CF_API_TOKEN = os.environ["CF_API_TOKEN"]

D1_URL = (
    f"https://api.cloudflare.com/client/v4/accounts/"
    f"{CF_ACCOUNT_ID}/d1/database/{D1_DATABASE_ID}/query"
)

HEADERS = {
    "Authorization": f"Bearer {CF_API_TOKEN}",
    "Content-Type": "application/json"
}

# 每批最多插入 50 个网站
DB_BATCH_SIZE = 50


# =====================================
# 批量写入 D1
# =====================================

def insert_websites(websites):
    """
    websites 格式：
    [
        {
            "domain": "example.com",
            "url": "https://example.com",
            "final_url": "https://example.com/",
            "http_status": 200
        }
    ]
    """

    if not websites:
        return 0

    total_inserted = 0

    for start in range(0, len(websites), DB_BATCH_SIZE):
        batch = websites[start:start + DB_BATCH_SIZE]

        now = int(time.time())

        values = []
        params = []

        for site in batch:
            values.append("(?, ?, ?, ?, 'pending', 'pending', ?, ?)")

            params.extend([
                site["domain"],
                site["url"],
                site["final_url"],
                site["http_status"],
                now,
                now
            ])

        sql = """
        INSERT OR IGNORE INTO websites (
            domain,
            url,
            final_url,
            http_status,
            crawl_status,
            image_status,
            first_seen,
            updated_at
        )
        VALUES
        """ + ", ".join(values)

        payload = {
            "sql": sql,
            "params": params
        }

        response = None

        for attempt in range(3):
            try:
                response = requests.post(
                    D1_URL,
                    headers=HEADERS,
                    json=payload,
                    timeout=30
                )

                response.raise_for_status()

                result = response.json()

                if not result.get("success"):
                    raise RuntimeError(str(result.get("errors")))

                break

            except Exception:
                if attempt == 2:
                    raise

                time.sleep(2 ** attempt)

        # D1 的 meta.changes 是实际插入的行数
        try:
            result = response.json()
            changes = result["result"][0]["meta"]["changes"]
            total_inserted += changes
        except (KeyError, IndexError, TypeError):
            pass

        print(
            f"D1 批量写入完成："
            f"{len(batch)} 条提交，累计新增 {total_inserted} 条"
        )

    return total_inserted
