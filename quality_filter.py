
import os
import time
import logging

from database import _query, _rows


# ============================================================
# 配置
# ============================================================

BATCH_SIZE = max(1, int(os.getenv("QUALITY_BATCH_SIZE", "100")))
# 每条批量 UPDATE 最多处理 15 个网站，避免 SQL 绑定参数过多。
BULK_UPDATE_SIZE = max(1, min(int(os.getenv("QUALITY_BULK_UPDATE_SIZE", "15")), 15))
# 每次运行最多筛选 2000 个网站；设置为 0 表示不限制。
MAX_PER_RUN = max(0, int(os.getenv("QUALITY_MAX_PER_RUN", "20000")))

# 最低合格分数
QUALIFIED_SCORE = 65

# 待复核最低分数
REVIEW_SCORE = 40

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

logger = logging.getLogger("DeopleQualityFilter")


# ============================================================
# 1. 获取待筛选网站
# ============================================================

def get_pending_quality_websites(limit=100, after_domain=""):
    """
    获取尚未筛选且爬取流程已结束的网站。
    使用 domain 游标分页：失败记录在本次运行中不会被反复取出；
    下一次运行游标重置后，仍会重新尝试所有 quality_status IS NULL 的记录。
    """

    sql = """
        SELECT
            domain,
            title,
            description,
            keywords,
            http_status,
            content_type,
            crawl_status
        FROM websites
        WHERE quality_status IS NULL
          AND crawl_status IS NOT NULL
          AND crawl_status != 'pending'
          AND domain > ?
        ORDER BY domain ASC
        LIMIT ?
    """

    result = _query(sql, [after_domain, limit])
    return _rows(result)

# ============================================================
# 2. 文本清理
# ============================================================

def clean_text(value):
    if value is None:
        return ""

    return str(value).strip()


# ============================================================
# 3. 判断是否为错误页面
# ============================================================

ERROR_TITLES = [
    "404 not found",
    "page not found",
    "403 forbidden",
    "access denied",
    "bad gateway",
    "502 bad gateway",
    "503 service unavailable",
    "500 internal server error",
    "website not found",
    "domain expired",
    "this site can't be reached",
    "this site can’t be reached",
    "server not found",
    "default web site page"
]


def is_error_title(title):
    title_lower = title.lower()

    for error in ERROR_TITLES:
        if error in title_lower:
            return True

    return False


# ============================================================
# 4. 网站质量评分
# ============================================================

def evaluate_website(item):
    """
    使用规则判断网站质量。

    总分 100：
    title       30
    description 35
    HTTP        15
    content     10
    keywords    10
    """

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

    # --------------------------------------------------------
    # 检查爬取状态
    # --------------------------------------------------------

    if crawl_status != "success":
        return {
            "quality_status": "rejected",
            "quality_score": 0,
            "quality_reason": "网页爬取未成功"
        }

    # --------------------------------------------------------
    # HTTP 状态
    # --------------------------------------------------------

    if 200 <= http_status < 300:
        score += 15
    else:
        reasons.append(f"HTTP状态异常：{http_status}")

    # --------------------------------------------------------
    # Title：30分
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Description：35分
    # --------------------------------------------------------

    if not description:
        reasons.append("缺少网页描述")

    else:
        description_length = len(description)

        if description_length >= 80:
            score += 35

        elif description_length >= 40:
            score += 25

        elif description_length >= 20:
            score += 15

        else:
            score += 5
            reasons.append("网页描述过短")

    # --------------------------------------------------------
    # Content-Type：10分
    # --------------------------------------------------------

    if "text/html" in content_type:
        score += 10

    else:
        reasons.append("非HTML网页或Content-Type缺失")

    # --------------------------------------------------------
    # Keywords：10分
    # --------------------------------------------------------

    if keywords:
        keyword_count = len([
            x for x in keywords.replace("，", ",").split(",")
            if x.strip()
        ])

        if keyword_count >= 5:
            score += 10

        elif keyword_count >= 2:
            score += 7

        else:
            score += 3

    else:
        reasons.append("缺少关键词")

    # --------------------------------------------------------
    # 错误页面直接拒绝
    # --------------------------------------------------------

    if is_error_title(title):
        score = 0

    # 分数限制在 0-100
    score = max(0, min(100, score))

    # --------------------------------------------------------
    # 最终评级
    # --------------------------------------------------------

    if score >= QUALIFIED_SCORE:
        status = "qualified"

    elif score >= REVIEW_SCORE:
        status = "review"

    else:
        status = "rejected"

    if not reasons:
        reasons.append("网页基础信息完整")

    return {
        "quality_status": status,
        "quality_score": score,
        "quality_reason": "；".join(reasons)
    }


# ============================================================
# 5. 保存筛选结果
# ============================================================

def save_quality_results(items):
    """
    批量保存一组评分结果。
    只更新 quality_status 仍为 NULL 的网站，避免覆盖已筛选结果。
    使用 CTE + VALUES，一条 SQL 更新多个网站行。
    """

    if not items:
        return

    now = int(time.time())
    values_sql = ", ".join(["(?, ?, ?, ?, ?)"] * len(items))

    sql = f"""
        WITH updates (
            domain,
            quality_status,
            quality_score,
            quality_reason,
            quality_checked_at
        ) AS (
            VALUES {values_sql}
        )
        UPDATE websites
        SET
            quality_status = (
                SELECT quality_status
                FROM updates
                WHERE updates.domain = websites.domain
            ),
            quality_score = (
                SELECT quality_score
                FROM updates
                WHERE updates.domain = websites.domain
            ),
            quality_reason = (
                SELECT quality_reason
                FROM updates
                WHERE updates.domain = websites.domain
            ),
            quality_checked_at = (
                SELECT quality_checked_at
                FROM updates
                WHERE updates.domain = websites.domain
            )
        WHERE quality_status IS NULL
          AND domain IN (SELECT domain FROM updates)
    """

    params = []
    for item in items:
        params.extend([
            item["domain"],
            item["result"]["quality_status"],
            item["result"]["quality_score"],
            item["result"]["quality_reason"],
            now,
        ])

    return _query(sql, params)

# ============================================================
# 6. 批量处理
# ============================================================

def process_batch(websites, totals):
    """
    先在本地计算评分，再分小批次写入 D1。
    单个网站评分异常不会阻止同批其他网站继续处理。
    """

    prepared = []
    batch_failed = 0

    for website in websites:
        domain = str(website.get("domain") or "").strip()
        if not domain:
            batch_failed += 1
            totals["failed"] += 1
            logger.error("[ERROR] 网站缺少 domain，跳过该记录")
            continue

        try:
            result = evaluate_website(website)
            prepared.append({
                "domain": domain,
                "result": result,
            })
        except Exception as e:
            batch_failed += 1
            totals["failed"] += 1
            logger.exception("[ERROR] 网站评分失败：%s | %s", domain, e)

    batch_checked = 0
    batch_qualified = 0
    batch_review = 0
    batch_rejected = 0
    write_failed = False

    for start in range(0, len(prepared), BULK_UPDATE_SIZE):
        chunk = prepared[start:start + BULK_UPDATE_SIZE]

        try:
            save_quality_results(chunk)
        except Exception as e:
            write_failed = True
            failed_count = len(chunk)
            batch_failed += failed_count
            totals["failed"] += failed_count
            logger.exception(
                "[ERROR] 批量保存 D1 失败，本批 %s 个网站未确认保存：%s",
                failed_count,
                e
            )
            # D1 写入异常时停止后续写入，避免持续请求一个故障接口。
            break

        for item in chunk:
            domain = item["domain"]
            result = item["result"]
            status = result["quality_status"]

            batch_checked += 1
            totals["checked"] += 1

            if status == "qualified":
                batch_qualified += 1
                totals["qualified"] += 1
            elif status == "review":
                batch_review += 1
                totals["review"] += 1
            else:
                batch_rejected += 1
                totals["rejected"] += 1

            logger.info(
                "[%s] %s | score=%s | %s",
                status.upper(),
                domain,
                result["quality_score"],
                result["quality_reason"]
            )

    return {
        "checked": batch_checked,
        "qualified": batch_qualified,
        "review": batch_review,
        "rejected": batch_rejected,
        "failed": batch_failed,
        "write_failed": write_failed,
    }

# ============================================================
# 7. 主程序：循环检查全部待筛选网站
# ============================================================

def main():

    totals = {
        "checked": 0,
        "qualified": 0,
        "review": 0,
        "rejected": 0,
        "failed": 0
    }

    logger.info("====================================")
    logger.info("Deople 网站质量筛选启动")
    logger.info("每次读取数量：%s", BATCH_SIZE)
    logger.info("每条批量 UPDATE 数量：%s", BULK_UPDATE_SIZE)
    logger.info("本次运行最大处理数量：%s（0 表示不限制）", MAX_PER_RUN)
    logger.info("筛选条件：仅 quality_status IS NULL，且爬取流程已结束")
    logger.info("====================================")

    batch_number = 0
    after_domain = ""
    reached_limit = False

    while True:

        if MAX_PER_RUN and totals["checked"] >= MAX_PER_RUN:
            reached_limit = True
            logger.info("达到本次运行数量上限：%s", MAX_PER_RUN)
            break

        remaining = BATCH_SIZE
        if MAX_PER_RUN:
            remaining = min(BATCH_SIZE, MAX_PER_RUN - totals["checked"])

        try:
            websites = get_pending_quality_websites(remaining, after_domain)
        except Exception as e:
            logger.exception("读取 D1 待筛选网站失败：%s", e)
            break

        if not websites:
            logger.info("没有更多待筛选网站")
            break

        batch_number += 1
        # 无论评分是否成功，都推进游标，避免失败记录在本次运行中无限重试。
        last_domain = str(websites[-1].get("domain") or "").strip()

        logger.info(
            "开始第 %s 批，本批网站数量：%s，游标：%s",
            batch_number,
            len(websites),
            last_domain
        )

        result = process_batch(websites, totals)
        after_domain = last_domain

        logger.info("------------------------------------")
        logger.info(
            "第 %s 批完成：检查 %s，合格 %s，复核 %s，不合格 %s，失败 %s",
            batch_number,
            result["checked"],
            result["qualified"],
            result["review"],
            result["rejected"],
            result["failed"]
        )

        logger.info(
            "累计检查：%s | 合格：%s | 复核：%s | 不合格：%s | 失败：%s",
            totals["checked"],
            totals["qualified"],
            totals["review"],
            totals["rejected"],
            totals["failed"]
        )

        if result["write_failed"]:
            logger.error("D1 批量写入失败，停止本次运行")
            break

        if not last_domain:
            logger.error("本批最后一条记录缺少 domain，停止运行")
            break

    logger.info("")
    logger.info("====================================")
    logger.info("Deople 质量筛选完成")
    logger.info("====================================")
    logger.info("总检查数量：%s", totals["checked"])
    logger.info("合格网站：%s", totals["qualified"])
    logger.info("待复核网站：%s", totals["review"])
    logger.info("不合格网站：%s", totals["rejected"])
    logger.info("处理失败：%s", totals["failed"])
    logger.info("是否达到本次运行上限：%s", reached_limit)
    logger.info("====================================")

# ============================================================
# 8. 程序入口
# ============================================================

if __name__ == "__main__":
    main()
