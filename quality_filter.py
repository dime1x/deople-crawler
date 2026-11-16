import os
import time
import logging

from database import _query, _rows

BATCH_SIZE = max(1, int(os.getenv("QUALITY_BATCH_SIZE", "100")))
# 7 bind parameters per website; cap at 14 to stay below a 100-parameter SQL limit.
BULK_UPDATE_SIZE = max(1, min(int(os.getenv("QUALITY_BULK_UPDATE_SIZE", "14")), 14))
MAX_PER_RUN = max(0, int(os.getenv("QUALITY_MAX_PER_RUN", "2000")))

QUALIFIED_SCORE = 65
REVIEW_SCORE = 40

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("DeopleQualityFilter")


def get_pending_quality_websites(limit=100, after_domain=""):
    """Only fetch unscored records. Keep the NULL guard unchanged."""
    sql = """
        SELECT
            domain, title, description, keywords, http_status,
            content_type, crawl_status, word_count, heading_count,
            paragraph_count, image_count, image_alt_count, video_count,
            internal_link_count, external_link_count, table_count,
            list_count, form_count, has_search, has_contact, has_about
        FROM websites
        WHERE quality_status IS NULL
          AND crawl_status IS NOT NULL
          AND crawl_status != 'pending'
          AND domain > ?
        ORDER BY domain ASC
        LIMIT ?
    """
    return _rows(_query(sql, [after_domain, limit]))


def clean_text(value):
    return str(value or "").strip()


ERROR_TITLES = [
    "404 not found", "page not found", "403 forbidden", "access denied",
    "bad gateway", "502 bad gateway", "503 service unavailable",
    "500 internal server error", "website not found", "domain expired",
    "this site can't be reached", "this site can’t be reached",
    "server not found", "default web site page"
]


def is_error_title(title):
    title_lower = title.lower()
    return any(error in title_lower for error in ERROR_TITLES)


def safe_int(item, key):
    try:
        return max(0, int(item.get(key) or 0))
    except (ValueError, TypeError):
        return 0


def evaluate_quality(item):
    """Original Deople quality formula retained: title/description/HTTP/type/keywords."""
    score = 0
    reasons = []
    title = clean_text(item.get("title"))
    description = clean_text(item.get("description"))
    keywords = clean_text(item.get("keywords"))
    content_type = clean_text(item.get("content_type")).lower()
    crawl_status = clean_text(item.get("crawl_status")).lower()
    try:
        http_status = int(item.get("http_status") or 0)
    except (ValueError, TypeError):
        http_status = 0

    if crawl_status != "success":
        return {
            "quality_status": "rejected",
            "quality_score": 0,
            "quality_reason": "网页爬取未成功",
        }

    if 200 <= http_status < 300:
        score += 15
    else:
        reasons.append(f"HTTP状态异常：{http_status}")

    if not title:
        reasons.append("缺少网页标题")
    elif is_error_title(title):
        reasons.append("标题疑似错误页面")
    else:
        score += 30
        if len(title) < 5:
            score -= 10
            reasons.append("标题过短")
        elif len(title) > 120:
            score -= 5
            reasons.append("标题过长")

    if not description:
        reasons.append("缺少网页描述")
    else:
        n = len(description)
        if n >= 80:
            score += 35
        elif n >= 40:
            score += 25
        elif n >= 20:
            score += 15
        else:
            score += 5
            reasons.append("网页描述过短")

    if "text/html" in content_type:
        score += 10
    else:
        reasons.append("非HTML网页或Content-Type缺失")

    if keywords:
        count = len([x for x in keywords.replace("，", ",").split(",") if x.strip()])
        score += 10 if count >= 5 else 7 if count >= 2 else 3
    else:
        reasons.append("缺少关键词")

    if is_error_title(title):
        score = 0
    score = max(0, min(100, score))

    status = (
        "qualified" if score >= QUALIFIED_SCORE
        else "review" if score >= REVIEW_SCORE
        else "rejected"
    )
    if not reasons:
        reasons.append("网页基础信息完整")
    return {
        "quality_status": status,
        "quality_score": score,
        "quality_reason": "；".join(reasons),
    }


def evaluate_richness(item):
    """
    Independent homepage richness score, 0-100.
    It measures observable content/structure, not originality or whole-site depth.
    """
    if clean_text(item.get("crawl_status")).lower() != "success":
        return 0, "网页未成功爬取，无法评估丰富度"

    words = safe_int(item, "word_count")
    headings = safe_int(item, "heading_count")
    paragraphs = safe_int(item, "paragraph_count")
    images = safe_int(item, "image_count")
    image_alts = safe_int(item, "image_alt_count")
    videos = safe_int(item, "video_count")
    internal = safe_int(item, "internal_link_count")
    external = safe_int(item, "external_link_count")
    tables = safe_int(item, "table_count")
    lists = safe_int(item, "list_count")
    forms = safe_int(item, "form_count")
    has_search = bool(safe_int(item, "has_search"))
    has_contact = bool(safe_int(item, "has_contact"))
    has_about = bool(safe_int(item, "has_about"))

    # Text content: 0-30
    text_score = 0
    text_score += 15 if words >= 1200 else 12 if words >= 700 else 9 if words >= 300 else 5 if words >= 100 else 0
    text_score += 8 if paragraphs >= 12 else 6 if paragraphs >= 6 else 3 if paragraphs >= 2 else 0
    text_score += 7 if headings >= 8 else 5 if headings >= 4 else 2 if headings >= 1 else 0

    # Visual/media: 0-25
    media_score = 0
    media_score += 12 if images >= 10 else 9 if images >= 5 else 5 if images >= 2 else 2 if images == 1 else 0
    if images:
        alt_ratio = image_alts / images
        media_score += 5 if alt_ratio >= 0.8 else 3 if alt_ratio >= 0.4 else 1
    media_score += 8 if videos >= 2 else 5 if videos == 1 else 0

    # Navigation/connectivity: 0-20
    link_score = (
        (10 if internal >= 20 else 7 if internal >= 10 else 4 if internal >= 3 else 1 if internal else 0)
        + (5 if external >= 10 else 3 if external >= 3 else 1 if external else 0)
        + (5 if has_about and has_contact else 3 if has_about or has_contact else 0)
    )

    # Structured information and interaction: 0-25
    structure_score = 0
    structure_score += 8 if tables >= 2 else 5 if tables == 1 else 0
    structure_score += 7 if lists >= 3 else 4 if lists >= 1 else 0
    structure_score += 5 if forms >= 2 else 3 if forms == 1 else 0
    structure_score += 5 if has_search else 0

    total = min(100, text_score + media_score + link_score + structure_score)
    details = (
        f"文本{ text_score}/30；媒体{media_score}/25；"
        f"链接导航{link_score}/20；结构交互{structure_score}/25"
    )
    return total, details


def save_quality_results(items):
    if not items:
        return
    now = int(time.time())
    values_sql = ", ".join(["(?, ?, ?, ?, ?, ?, ?)"] * len(items))
    sql = f"""
        WITH updates (
            domain, quality_status, quality_score, quality_reason,
            richness_score, richness_reason, quality_checked_at
        ) AS (
            VALUES {values_sql}
        )
        UPDATE websites
        SET
            quality_status = (SELECT quality_status FROM updates WHERE updates.domain = websites.domain),
            quality_score = (SELECT quality_score FROM updates WHERE updates.domain = websites.domain),
            quality_reason = (SELECT quality_reason FROM updates WHERE updates.domain = websites.domain),
            richness_score = (SELECT richness_score FROM updates WHERE updates.domain = websites.domain),
            richness_reason = (SELECT richness_reason FROM updates WHERE updates.domain = websites.domain),
            quality_checked_at = (SELECT quality_checked_at FROM updates WHERE updates.domain = websites.domain)
        WHERE quality_status IS NULL
          AND domain IN (SELECT domain FROM updates)
    """
    params = []
    for item in items:
        result = item["result"]
        params.extend([
            item["domain"],
            result["quality_status"],
            result["quality_score"],
            result["quality_reason"],
            result["richness_score"],
            result["richness_reason"],
            now,
        ])
    return _query(sql, params)


def process_batch(websites, totals):
    prepared = []
    batch_failed = 0
    for website in websites:
        domain = str(website.get("domain") or "").strip()
        if not domain:
            batch_failed += 1
            totals["failed"] += 1
            continue
        try:
            quality = evaluate_quality(website)
            richness_score, richness_reason = evaluate_richness(website)
            quality.update({
                "richness_score": richness_score,
                "richness_reason": richness_reason,
            })
            prepared.append({"domain": domain, "result": quality})
        except Exception:
            batch_failed += 1
            totals["failed"] += 1
            logger.exception("评分失败：%s", domain)

    batch_checked = batch_qualified = batch_review = batch_rejected = 0
    write_failed = False
    for start in range(0, len(prepared), BULK_UPDATE_SIZE):
        chunk = prepared[start:start + BULK_UPDATE_SIZE]
        try:
            save_quality_results(chunk)
        except Exception:
            write_failed = True
            totals["failed"] += len(chunk)
            batch_failed += len(chunk)
            logger.exception("批量保存 D1 失败")
            break
        for item in chunk:
            result = item["result"]
            status = result["quality_status"]
            batch_checked += 1
            totals["checked"] += 1
            totals[status] += 1
            if status == "qualified":
                batch_qualified += 1
            elif status == "review":
                batch_review += 1
            else:
                batch_rejected += 1
            logger.info(
                "[%s] %s | quality=%s | richness=%s | %s",
                status.upper(), item["domain"], result["quality_score"],
                result["richness_score"], result["richness_reason"]
            )
    return {
        "checked": batch_checked, "qualified": batch_qualified,
        "review": batch_review, "rejected": batch_rejected,
        "failed": batch_failed, "write_failed": write_failed,
    }


def main():
    totals = {"checked": 0, "qualified": 0, "review": 0, "rejected": 0, "failed": 0}
    logger.info("Deople 质量与丰富度筛选启动；仅处理 quality_status IS NULL")
    logger.info("每次读取：%s；每次运行上限：%s（0=不限）", BATCH_SIZE, MAX_PER_RUN)
    after_domain = ""
    batch_number = 0

    while True:
        if MAX_PER_RUN and totals["checked"] >= MAX_PER_RUN:
            logger.info("达到本次运行上限：%s", MAX_PER_RUN)
            break
        remaining = BATCH_SIZE
        if MAX_PER_RUN:
            remaining = min(BATCH_SIZE, MAX_PER_RUN - totals["checked"])
        try:
            websites = get_pending_quality_websites(remaining, after_domain)
        except Exception:
            logger.exception("读取 D1 待筛选网站失败")
            break
        if not websites:
            logger.info("没有更多待筛选网站")
            break

        batch_number += 1
        last_domain = str(websites[-1].get("domain") or "").strip()
        result = process_batch(websites, totals)
        after_domain = last_domain
        logger.info(
            "第%s批：检查%s，合格%s，复核%s，不合格%s，失败%s",
            batch_number, result["checked"], result["qualified"],
            result["review"], result["rejected"], result["failed"]
        )
        if result["write_failed"] or not last_domain:
            break

    logger.info(
        "完成：检查%s，合格%s，复核%s，不合格%s，失败%s",
        totals["checked"], totals["qualified"], totals["review"],
        totals["rejected"], totals["failed"]
    )


if __name__ == "__main__":
    main()
