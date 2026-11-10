
import os
import re
import requests
import ipaddress

# =========================
# Cloudflare D1 配置
# =========================

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

BATCH_SIZE = 200


# =========================
# D1 数据库操作
# =========================

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


# =========================
# 域名验证
# =========================

def validate_domain(domain):

    if not domain:
        return None

    domain = domain.strip().lower()

    # 去除通配符
    if domain.startswith("*."):
        domain = domain[2:]

    # 转换国际化域名
    try:
        domain = domain.encode("idna").decode("ascii")
    except Exception:
        return None

    # 长度限制
    if len(domain) > 253:
        return None

    # 最多两个点号
    if domain.count(".") > 2:
        return None

    # 至少一个点号
    if domain.count(".") < 1:
        return None

    # 排除连续点号
    if ".." in domain:
        return None

    # 排除非法字符
    if not re.fullmatch(r"[a-z0-9.-]+", domain):
        return None

    # 检查每个域名标签
    labels = domain.split(".")

    for label in labels:
        if not label or len(label) > 63:
            return None

        if label.startswith("-") or label.endswith("-"):
            return None

    # 排除 IP 地址
    try:
        ipaddress.ip_address(domain)
        return None
    except ValueError:
        pass

    return domain


# =========================
# 获取待过滤域名
# =========================

def get_pending_domains():

    result = d1_query(
        """
        SELECT domain, first_seen
        FROM raw_domains
        WHERE processed = 0
        LIMIT ?
        """,
        [BATCH_SIZE]
    )

    return result.get("results", [])


# =========================
# 批量写入网站表
# =========================

def insert_websites(rows):

    if not rows:
        return 0

    values = []
    params = []

    for domain, first_seen in rows:

        values.append("(?, ?, 'pending')")

        params.extend([
            domain,
            first_seen
        ])

    sql = """
    INSERT OR IGNORE INTO websites
    (domain, first_seen, crawl_status)
    VALUES
    """ + ",".join(values)

    result = d1_query(sql, params)

    return result.get("meta", {}).get("changes", 0)


# =========================
# 标记已经处理
# =========================

def mark_processed(domains):

    if not domains:
        return

    placeholders = ",".join(["?"] * len(domains))

    sql = f"""
    UPDATE raw_domains
    SET processed = 1
    WHERE domain IN ({placeholders})
    """

    d1_query(sql, domains)


# =========================
# 主程序
# =========================

def main():

    total = 0
    inserted = 0

    while True:

        records = get_pending_domains()

        if not records:
            break

        valid_rows = []
        processed_domains = []

        for record in records:

            original_domain = record["domain"]
            first_seen = record["first_seen"]

            domain = validate_domain(original_domain)

            processed_domains.append(original_domain)

            if domain:

                valid_rows.append(
                    (domain, first_seen)
                )

        # 先写入网站表
        count = insert_websites(valid_rows)

        # 再标记原始域名已处理
        mark_processed(processed_domains)

        total += len(records)
        inserted += count

        print(
            f"本批处理: {len(records)}"
            f" | 合格: {len(valid_rows)}"
            f" | 新增: {count}"
        )

    print("=" * 40)
    print("过滤完成")
    print("处理总数:", total)
    print("新增网站:", inserted)


if __name__ == "__main__":
    main()
