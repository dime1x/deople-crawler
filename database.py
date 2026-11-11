"""
Deople Cloudflare D1 database helper.

Required environment variables:
    CF_ACCOUNT_ID
    D1_DATABASE_ID
    CF_API_TOKEN

Install:
    pip install httpx

Expected websites table columns include:
    domain, url, final_url, title, description, keywords,
    http_status, content_type, crawl_status, crawl_attempts,
    crawl_error, last_crawled_at, next_crawl_at,
    image_url, image_key, image_status, image_attempts,
    first_seen, updated_at
"""

import logging
import os
import time
from typing import Any, Dict, List, Optional

import httpx


logger = logging.getLogger("deople.database")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
)

CF_ACCOUNT_ID = os.getenv("CF_ACCOUNT_ID", "").strip()
D1_DATABASE_ID = os.getenv("D1_DATABASE_ID", "").strip()
CF_API_TOKEN = os.getenv("CF_API_TOKEN", "").strip()

D1_QUERY_URL = (
    f"https://api.cloudflare.com/client/v4/accounts/"
    f"{CF_ACCOUNT_ID}/d1/database/{D1_DATABASE_ID}/query"
)

HTTP_TIMEOUT = float(os.getenv("D1_HTTP_TIMEOUT", "30"))
BATCH_SIZE = int(os.getenv("D1_BATCH_SIZE", "10"))


def _check_config() -> None:
    missing = []
    if not CF_ACCOUNT_ID:
        missing.append("CF_ACCOUNT_ID")
    if not D1_DATABASE_ID:
        missing.append("D1_DATABASE_ID")
    if not CF_API_TOKEN:
        missing.append("CF_API_TOKEN")

    if missing:
        raise RuntimeError(
            "Missing required environment variables: " + ", ".join(missing)
        )


def _decode_response(response: httpx.Response) -> Dict[str, Any]:
    """Decode Cloudflare's JSON response and preserve useful error details."""
    try:
        data = response.json()
    except Exception:
        data = {"raw_response": response.text}

    if response.is_error:
        raise RuntimeError(
            f"Cloudflare D1 HTTP {response.status_code}: {data}"
        )

    if isinstance(data, dict) and data.get("success") is False:
        raise RuntimeError(f"Cloudflare D1 API error: {data}")

    return data


def _post(payload: Dict[str, Any]) -> Dict[str, Any]:
    _check_config()

    headers = {
        "Authorization": f"Bearer {CF_API_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        response = httpx.post(
            D1_QUERY_URL,
            headers=headers,
            json=payload,
            timeout=HTTP_TIMEOUT,
        )
    except httpx.RequestError as exc:
        raise RuntimeError(f"Cannot connect to Cloudflare D1 API: {exc}") from exc

    return _decode_response(response)


def _query(sql: str, params: Optional[List[Any]] = None) -> Dict[str, Any]:
    """
    Execute one SQL statement through Cloudflare's D1 REST API.

    Parameter values are converted to strings for compatibility with
    the D1 REST API's parameter format. SQLite will coerce numeric strings
    when storing them in INTEGER-affinity columns.
    """
    safe_params = [str(value) if value is not None else None
                   for value in (params or [])]

    data = _post({
        "sql": sql,
        "params": safe_params,
    })

    results = data.get("result") or []
    if not results:
        return {}

    first = results[0]
    if isinstance(first, dict) and first.get("success") is False:
        raise RuntimeError(f"Cloudflare D1 SQL error: {first}")

    return first


def _result_rows(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = result.get("results", []) if isinstance(result, dict) else []
    return rows if isinstance(rows, list) else []


def insert_websites(websites: List[Dict[str, Any]]) -> int:
    """
    Insert validated websites into D1.

    Expected input fields:
        domain, url, final_url, http_status
    Optional:
        content_type

    Existing domains are ignored. Returns the number of input records
    submitted successfully (not necessarily the number of newly inserted
    rows, because INSERT OR IGNORE may skip duplicates).
    """
    if not websites:
        return 0

    now = int(time.time())
    statements = []

    sql = """
        INSERT OR IGNORE INTO websites (
            domain,
            url,
            final_url,
            http_status,
            content_type,
            crawl_status,
            image_status,
            first_seen,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, 'pending', 'pending', ?, ?)
    """

    for item in websites:
        domain = str(item.get("domain") or "").strip().lower()
        url = str(item.get("url") or "").strip()

        if not domain or not url:
            logger.warning("Skip website with missing domain or url: %s", item)
            continue

        final_url = item.get("final_url") or url
        status = item.get("http_status")
        content_type = item.get("content_type") or ""

        params = [
            domain,
            url,
            str(final_url),
            str(status) if status is not None else None,
            str(content_type),
            str(now),
            str(now),
        ]

        statements.append({
            "sql": sql,
            "params": params,
        })

    if not statements:
        return 0

    submitted = 0

    for start in range(0, len(statements), BATCH_SIZE):
        batch = statements[start:start + BATCH_SIZE]

        # The D1 API accepts a batch of SQL statements at the /query endpoint.
        data = _post({"batch": batch})

        results = data.get("result") or []
        for index, result in enumerate(results):
            if isinstance(result, dict) and result.get("success") is False:
                raise RuntimeError(
                    f"Cloudflare D1 batch SQL error at item {index}: {result}"
                )

        submitted += len(batch)
        logger.info(
            "D1 batch insert submitted: %s/%s",
            min(start + len(batch), len(statements)),
            len(statements),
        )

    logger.info("D1 写入完成：提交 %s 条网站记录", submitted)
    return submitted


def claim_pending_screenshots(limit: int = 20) -> List[Dict[str, Any]]:
    """
    Get pending screenshot jobs and mark them as processing.

    Returns rows containing at least domain and url.
    """
    limit = max(1, min(int(limit), 100))

    result = _query(
        """
        SELECT domain, url, final_url
        FROM websites
        WHERE image_status = 'pending'
        ORDER BY first_seen ASC
        LIMIT ?
        """,
        [limit],
    )

    rows = _result_rows(result)
    claimed: List[Dict[str, Any]] = []

    for row in rows:
        domain = row.get("domain")
        if not domain:
            continue

        update_result = _query(
            """
            UPDATE websites
            SET image_status = 'processing',
                image_attempts = COALESCE(image_attempts, 0) + 1,
                updated_at = ?
            WHERE domain = ?
              AND image_status = 'pending'
            """,
            [int(time.time()), domain],
        )

        # D1 reports changes in meta.changes. If unavailable, keep the row
        # only when the update response does not explicitly report zero.
        meta = update_result.get("meta", {}) if isinstance(update_result, dict) else {}
        changes = meta.get("changes")
        if changes is None or changes > 0:
            claimed.append(row)

    logger.info("Claimed %s pending screenshot jobs", len(claimed))
    return claimed


def update_image(domain: str, image_url: str, image_key: str) -> None:
    """Mark a website screenshot as successfully uploaded to R2."""
    _query(
        """
        UPDATE websites
        SET image_url = ?,
            image_key = ?,
            image_status = 'ready',
            updated_at = ?
        WHERE domain = ?
        """,
        [image_url, image_key, int(time.time()), domain],
    )
    logger.info("Screenshot saved in D1: %s", domain)


def mark_image_failed(domain: str, error: str) -> None:
    """Mark a screenshot job as failed and store its error message."""
    _query(
        """
        UPDATE websites
        SET image_status = 'failed',
            crawl_error = ?,
            updated_at = ?
        WHERE domain = ?
        """,
        [str(error)[:2000], int(time.time()), domain],
    )
    logger.warning("Screenshot failed: %s - %s", domain, error)


def test_connection() -> None:
    """Read-only connection test."""
    result = _query("SELECT COUNT(*) AS total FROM websites")
    rows = _result_rows(result)
    total = rows[0].get("total", 0) if rows else 0
    logger.info("Cloudflare D1 connection OK. websites rows: %s", total)


if __name__ == "__main__":
    test_connection()
