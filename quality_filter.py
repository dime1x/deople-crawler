
import os
import time
import logging

from database import _query, _rows


# ============================================================
# 配置
# ============================================================

BATCH_SIZE = int(os.getenv("QUALITY_BATCH_SIZE", "100"))

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

def get_pending_quality_websites(limit=100):
    """
    获取已经爬取完成、但尚未进行质量筛选的网站。
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
        ORDER BY last_checked ASC
        LIMIT ?
    """

    result = _query(sql, [limit])

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

def save_quality_result(domain, result):
    """
    将质量评分保存到 D1。
    只更新尚未筛选的网站。
    """

    sql = """
        UPDATE websites
        SET
            quality_status = ?,
            quality_score = ?,
            quality_reason = ?,
            quality_checked_at = ?
        WHERE domain = ?
          AND quality_status IS NULL
    """

    params = [
        result["quality_status"],
        result["quality_score"],
        result["quality_reason"],
        int(time.time()),
        domain
    ]

    return _query(sql, params)


# ============================================================
# 6. 批量处理
# ============================================================

def process_batch(websites, totals):
    """
    处理一批网站。
    """

    batch_checked = 0
    batch_qualified = 0
    batch_review = 0
    batch_rejected = 0
    batch_failed = 0

    for website in websites:

        domain = website.get("domain", "")

        try:
            result = evaluate_website(website)

            save_quality_result(domain, result)

            batch_checked += 1
            totals["checked"] += 1

            status = result["quality_status"]

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

        except Exception as e:

            batch_failed += 1
            totals["failed"] += 1

            logger.exception(
                "[ERROR] 网站处理失败：%s | %s",
                domain,
                e
            )

    return {
        "checked": batch_checked,
        "qualified": batch_qualified,
        "review": batch_review,
        "rejected": batch_rejected,
        "failed": batch_failed
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
    logger.info("每批处理数量：%s", BATCH_SIZE)
    logger.info("目标：全部待筛选网站")
    logger.info("====================================")

    batch_number = 0

    while True:

        try:
            websites = get_pending_quality_websites(BATCH_SIZE)

        except Exception as e:
            logger.exception("读取 D1 待筛选网站失败：%s", e)
            break

        # 没有待筛选网站，结束
        if not websites:
            logger.info("没有更多待筛选网站")
            break

        batch_number += 1

        logger.info(
            "开始第 %s 批，本批网站数量：%s",
            batch_number,
            len(websites)
        )

        result = process_batch(websites, totals)

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

        # 防止整批都处理失败，造成无限循环
        if result["checked"] == 0:
            logger.error(
                "本批没有成功保存任何筛选结果，停止运行，避免无限循环"
            )
            break

    # ========================================================
    # 最终统计
    # ========================================================

    logger.info("")
    logger.info("====================================")
    logger.info("Deople 质量筛选完成")
    logger.info("====================================")

    logger.info("总检查数量：%s", totals["checked"])
    logger.info("合格网站：%s", totals["qualified"])
    logger.info("待复核网站：%s", totals["review"])
    logger.info("不合格网站：%s", totals["rejected"])
    logger.info("处理失败：%s", totals["failed"])

    logger.info("====================================")


# ============================================================
# 8. 程序入口
# ============================================================

if __name__ == "__main__":
    main()
