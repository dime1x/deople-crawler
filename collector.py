import certstream
import sqlite3
import time
import re

DB = "deople.db"

conn = sqlite3.connect(DB)
conn.execute("""
CREATE TABLE IF NOT EXISTS raw_domains (
    domain TEXT PRIMARY KEY,
    first_seen INTEGER
)
""")
conn.commit()


def valid_domain(domain):
    domain = domain.lower().strip()

    if domain.startswith("*."):
        domain = domain[2:]

    if len(domain) > 253:
        return None

    if "." not in domain:
        return None

    if not re.fullmatch(
        r"[a-z0-9.-]+",
        domain
    ):
        return None

    if ".." in domain or domain.startswith("."):
        return None

    return domain


def callback(message, context):
    try:
        domains = message.get(
            "data", {}
        ).get(
            "leaf_cert", {}
        ).get(
            "all_domains", []
        )

        for item in domains:
            domain = valid_domain(item)

            if not domain:
                continue

            conn.execute(
                """
                INSERT OR IGNORE INTO raw_domains
                (domain, first_seen)
                VALUES (?, ?)
                """,
                (domain, int(time.time()))
            )

        conn.commit()

    except Exception as e:
        print("处理错误:", e)


print("Deople Collector started")

certstream.listen_for_events(
    callback,
    url="wss://certstream.calidog.io/"
)