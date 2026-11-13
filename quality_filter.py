
"""
Deople Website Quality Filter
------------------------------
从 Cloudflare D1 读取已经爬取的网站，
使用 Python 规则进行质量筛选，并将结果写回 D1。

筛选结果：
qualified = 合格，可以进入截图流程
review    = 待人工检查
rejected  = 不合格

依赖：
pip install httpx

环境变量：
CF_ACCOUNT_ID
D1_DATABASE_ID
CF_API_TOKEN
QUALITY_BATCH_SIZE (默认 100)
"""

import os
import re
import time
import logging

from database import _query, _rows


# =====================================
# 配置
# =====================================

BATCH_SIZE = max(
    1,
    min(int(os.getenv("QUALITY_BATCH_SIZE", "100")), 200)
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

logger = logging.getLogger("deople.quality")


# =====================================
# 基础文本处理
# =====================================

def clean_text(value):
    """清理文本中的多余空格和换行。"""

    if not value:
        return ""

    return re.sub(r"\s+", " ", str(value)).strip()


# =====================================
# 判断是否为错误页面标题
# =====================================

ERROR_TITLES = [
    "404 not found",
    "page not found",
    "403 forbidden",
    "access denied",
    "bad gateway",
    "502 bad gateway",
    "503 service unavailable",
    "500 internal server error",
    "site not found",
    "website not found",
    "domain not found",
    "under construction",
]


def is_error_title(title):
    """判断标题是否明显属于错误页面。"""

    value = title.lower().strip()

    for keyword in ERROR_TITLES:
        if value == keyword or value.startswith(keyword + " -"):
            return True

    return False


# =====================================
# 关键词数量
# =====================================

def count_keywords(keywords):
    if not keywords:
        return 0

    items = re.split(r"[,，;；|]", keywords)

    return len([
        item.strip()
        for item in items
        if len(item.strip()) >= 2
    ])


# =====================================
# 网站质量评分
# =====================================

def evaluate_website(item):
    """
    返回：
    {
        quality_status,
        quality_score,
        quality_reason
    }
    """

    title = clean_text(item.get("title"))
    description = clean_text(item.get("description"))
    keywords = clean_text(item.get("keywords"))

    crawl_status = clean_text(
        item.get("crawl_status")
    ).lower()

    content_type = clean_text(
        item.get("content_type")
    ).lower()

    http_status = item.get("http_status")

    reasons = []

    # ---------------------------------
    # 一、硬性排除条件
    # ---------------------------------

    if crawl_status != "success":
        return {
            "quality_status": "rejected",
            "quality_score": 0,
            "quality_reason": "页面爬取未成功"
        }

    if not title:
        return {
            "quality_status": "rejected",
            "quality_score": 0,
            "quality_reason": "网页标题为空"
        }

    if is_error_title(title):
        return {
            "quality_status": "rejected",
            "quality_score": 0,
            "quality_reason": "疑似错误页面标题"
        }

    try:
        status = int(http_status)
    except (TypeError, ValueError):
        status = 0

    if not 200 <= status < 300:
        return {
            "quality_status": "rejected",
            "quality_score": 0,
            "quality_reason": "HTTP 状态码不是 2xx"
        }

    # ---------------------------------
    # 二、标题评分 30 分
    # ---------------------------------

    score = 0

    title_length = len(title)

    if 5 <= title_length <= 100:
        score += 30
    elif 101 <= title_length <= 200:
        score += 20
        reasons.append("标题偏长")
    elif 1 <= title_length < 5:
        score += 8
        reasons.append("标题过短")
    else:
        score += 10
        reasons.append("标题长度异常")

    # ---------------------------------
    # 三、描述评分 35 分
    # ---------------------------------

    description_length = len(description)

    if description_length >= 100:
        score += 35
    elif description_length >= 50:
        score += 30
    elif description_length >= 20:
        score += 20
        reasons.append("描述较短")
    elif description_length > 0:
        score += 10
        reasons.append("描述过短")
    else:
        reasons.append("缺少网站描述")

    # ---------------------------------
    # 四、HTTP 状态评分 15 分
    # ---------------------------------

    if status == 200:
        score += 15
    else:
        score += 12

    # ---------------------------------
    # 五、页面类型评分 10 分
    # ---------------------------------

    if "text/html" in content_type:
        score += 10
    elif not content_type:
        score += 5
        reasons.append("缺少 Content-Type")
    else:
        reasons.append("页面不是标准 HTML 类型")

    # ---------------------------------
    # 六、关键词评分 10 分
    # ---------------------------------

    keyword_count = count_keywords(keywords)

    if keyword_count >= 3:
        score += 10
    elif keyword_count >= 1:
        score += 5
    else:
        reasons.append("缺少 SEO 关键词")

    # ---------------------------------
    # 七、最终分类
    # ---------------------------------

    if score >= 65:
        quality_status = "qualified"

    elif score >= 40:
        quality_status = "review"

    else:
        quality_status = "rejected"

    if not reasons:
        reasons.append("基础质量检查通过")

    return {
        "quality_status": quality_status,
        "quality_score": score,
        "quality_reason": "；".join(reasons)
    }


# =====================================
# 从 D1 获取待筛选网站
# =====================================

def get_pending_quality_websites(limit):

    result = _query(
        """
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
        """,
        [limit]
    )

    return _rows(result)


# =====================================
# 保存筛选结果
# =====================================

def save_quality_result(domain, result):

    _query(
        """
        UPDATE websites
        SET quality_status = ?,
            quality_score = ?,
            quality_reason = ?,
            quality_checked_at = ?
        WHERE domain = ?
          AND quality_status IS NULL
        """,
        [
            result["quality_status"],
            result["quality_score"],
            result["quality_reason"][:1000],
            int(time.time()),
            domain
        ]
    )


# =====================================
# 主程序
# =====================================

def main():

    logger.info("Deople Quality Filter started")

    websites = get_pending_quality_websites(BATCH_SIZE)

    if not websites:
        logger.info("没有等待筛选的网站")
        return

    total = 0
    qualified = 0
    review = 0
    rejected = 0
    errors = 0

    logger.info("本次读取 %s 个网站", len(websites))

    for item in websites:

        domain = item.get("domain")

        if not domain:
            continue

        try:

            result = evaluate_website(item)

            save_quality_result(domain, result)

            total += 1

            status = result["quality_status"]

            if status == "qualified":
                qualified += 1

            elif status == "review":
                review += 1

            else:
                rejected += 1

            logger.info(
                "[%s] %s | score=%s | %s",
                status.upper(),
                domain,
                result["quality_score"],
                result["quality_reason"]
            )

        except Exception as exc:

            errors += 1

            logger.error(
                "[ERROR] %s: %s",
                domain,
                str(exc)[:300]
            )

    logger.info("==============================")
    logger.info("本次筛选完成")
    logger.info("处理数量：%s", total)
    logger.info("合格：%s", qualified)
    logger.info("待检查：%s", review)
    logger.info("不合格：%s", rejected)
    logger.info("失败：%s", errors)
    logger.info("==============================")


if __name__ == "__main__":
    main()
