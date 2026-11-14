
import os
import io
import re
import time
import socket
import ipaddress
import logging

from urllib.parse import urlparse

import boto3
from botocore.config import Config
from PIL import Image
from playwright.sync_api import sync_playwright

from database import (
    _query,
    _rows,
    update_image,
    mark_image_failed,
)


# ============================================================
# 1. 基础配置
# ============================================================

R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID", "").strip()
R2_ACCESS_KEY = os.getenv("R2_ACCESS_KEY", "").strip()
R2_SECRET_KEY = os.getenv("R2_SECRET_KEY", "").strip()

R2_BUCKET = os.getenv("R2_BUCKET", "deople-images")

IMAGE_BASE_URL = os.getenv(
    "IMAGE_BASE_URL",
    "https://img.deople.ai"
).rstrip("/")

# 每批截图网站数量
BATCH_SIZE = int(
    os.getenv("SCREENSHOT_BATCH_SIZE", "100")
)

# 每次最多运行批次数量
MAX_BATCHES = int(
    os.getenv("SCREENSHOT_MAX_BATCHES", "20")
)

# 最终只保存一张电脑端截图，固定尺寸 240 × 320
IMAGE_WIDTH = 240
IMAGE_HEIGHT = 320

# JPEG压缩质量
IMAGE_QUALITY = 75

# 浏览器配置
PAGE_TIMEOUT = 30000
WAIT_AFTER_LOAD = 1200

VIEWPORT_WIDTH = 1280
VIEWPORT_HEIGHT = 800

# 最大截图高度
MAX_SCREENSHOT_HEIGHT = 12000


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

logger = logging.getLogger("DeopleScreenshot")


# ============================================================
# 2. 检查R2配置
# ============================================================

def check_config():

    required = {
        "R2_ACCOUNT_ID": R2_ACCOUNT_ID,
        "R2_ACCESS_KEY": R2_ACCESS_KEY,
        "R2_SECRET_KEY": R2_SECRET_KEY,
    }

    missing = [
        key for key, value in required.items()
        if not value
    ]

    if missing:
        raise RuntimeError(
            "缺少R2环境变量：" + ", ".join(missing)
        )


# ============================================================
# 3. 创建R2客户端
# ============================================================

def create_r2_client():

    check_config()

    return boto3.client(
        "s3",
        endpoint_url=(
            f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
        ),
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto",
        config=Config(
            signature_version="s3v4",
            retries={
                "max_attempts": 3,
                "mode": "standard"
            }
        )
    )


# ============================================================
# 4. 网站安全检查
# ============================================================

def is_safe_url(url):

    try:

        parsed = urlparse(url)

        if parsed.scheme not in ("http", "https"):
            return False

        if not parsed.hostname:
            return False

        if parsed.username or parsed.password:
            return False

        port = parsed.port

        if port is not None and port not in (80, 443):
            return False

        addresses = socket.getaddrinfo(
            parsed.hostname,
            port or (
                443 if parsed.scheme == "https" else 80
            ),
            type=socket.SOCK_STREAM
        )

        if not addresses:
            return False

        for address in addresses:

            ip = ipaddress.ip_address(
                address[4][0]
            )

            if not ip.is_global:
                return False

        return True

    except Exception:
        return False


# ============================================================
# 5. 浏览器请求安全检查
# ============================================================

def check_browser_request(route):

    request_url = route.request.url

    parsed = urlparse(request_url)

    if parsed.scheme not in ("http", "https"):
        route.abort()
        return

    if not is_safe_url(request_url):
        route.abort()
        return

    route.continue_()


# ============================================================
# 6. 获取合格的待截图网站
# ============================================================

def claim_pending_screenshots(limit=20):

    limit = max(1, min(int(limit), 100))

    sql = """
        SELECT
            domain,
            url,
            final_url
        FROM websites
        WHERE quality_status = 'qualified'
          AND crawl_status = 'success'
          AND image_status = 'pending'
        ORDER BY first_seen ASC
        LIMIT ?
    """

    result = _query(sql, [limit])

    websites = _rows(result)

    claimed = []

    for website in websites:

        domain = website.get("domain")

        if not domain:
            continue

        update_sql = """
            UPDATE websites
            SET
                image_status = 'processing',
                updated_at = ?
            WHERE domain = ?
              AND quality_status = 'qualified'
              AND image_status = 'pending'
        """

        _query(
            update_sql,
            [
                int(time.time()),
                domain
            ]
        )

        claimed.append(website)

    logger.info(
        "领取待截图网站：%s个",
        len(claimed)
    )

    return claimed


# ============================================================
# 7. 网页截图
# ============================================================

def capture_screenshot(browser, url):

    if not is_safe_url(url):
        raise ValueError(
            "网站地址不安全或无法解析"
        )

    context = browser.new_context(
        viewport={
            "width": VIEWPORT_WIDTH,
            "height": VIEWPORT_HEIGHT
        },
        device_scale_factor=1,
        ignore_https_errors=False
    )

    try:

        context.route(
            "**/*",
            check_browser_request
        )

        page = context.new_page()

        page.set_default_timeout(PAGE_TIMEOUT)

        response = page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT
        )

        if response and response.status >= 400:

            raise RuntimeError(
                f"网页HTTP状态异常：{response.status}"
            )

        page.wait_for_timeout(
            WAIT_AFTER_LOAD
        )

        page.evaluate(
            """() => {
                document.documentElement.style.maxHeight = "12000px";
                document.documentElement.style.overflow = "hidden";

                if (document.body) {
                    document.body.style.maxHeight = "12000px";
                    document.body.style.overflow = "hidden";
                }
            }"""
        )

        image_bytes = page.screenshot(
            full_page=True,
            type="jpeg",
            quality=85,
            animations="disabled"
        )

        return image_bytes

    finally:

        context.close()


# ============================================================
# 8. 图片压缩
#    固定生成240 × 320px图片，从网页顶部开始裁切
# ============================================================

def compress_image(image_bytes):

    image = Image.open(
        io.BytesIO(image_bytes)
    ).convert("RGB")

    width, height = image.size

    # 目标比例为 3:4。按原图宽度计算需要的裁切高度，
    # 从网页顶部开始保留内容，不再生成手机版图片。
    target_ratio = IMAGE_WIDTH / IMAGE_HEIGHT
    crop_height = int(width / target_ratio)

    if height >= crop_height:
        image = image.crop((0, 0, width, crop_height))
    else:
        # 页面内容不足时，在底部补白，避免拉伸变形
        canvas = Image.new("RGB", (width, crop_height), "white")
        canvas.paste(image, (0, 0))
        image = canvas

    image = image.resize(
        (IMAGE_WIDTH, IMAGE_HEIGHT),
        Image.Resampling.LANCZOS
    )

    output = io.BytesIO()

    image.save(
        output,
        format="JPEG",
        quality=IMAGE_QUALITY,
        optimize=True
    )

    return output.getvalue()


# ============================================================
# 9. 生成图片文件名
# ============================================================

def make_image_key(domain):

    domain = str(domain).lower().strip()

    safe_domain = re.sub(
        r"[^a-z0-9.-]",
        "_",
        domain
    )

    if not safe_domain:
        raise ValueError("无效的网站域名")

    return f"{safe_domain}.jpg"


# ============================================================
# 10. 上传图片到R2
# ============================================================

def upload_to_r2(s3, domain, image_bytes):

    image_key = make_image_key(domain)

    s3.put_object(
        Bucket=R2_BUCKET,
        Key=image_key,
        Body=image_bytes,
        ContentType="image/jpeg",
        CacheControl=(
            "public, max-age=31536000, immutable"
        )
    )

    image_url = (
        f"{IMAGE_BASE_URL}/{image_key}"
    )

    return image_url, image_key


# ============================================================
# 11. 处理单个网站
# ============================================================

def process_website(
    browser,
    s3,
    website,
    totals
):

    domain = website.get("domain", "")

    url = (
        website.get("final_url")
        or website.get("url")
        or ""
    )

    logger.info(
        "开始截图：%s",
        domain
    )

    try:

        if not url:
            raise ValueError("网站URL为空")

        # 第一步：网页截图一次
        image_bytes = capture_screenshot(
            browser,
            url
        )

        # 第二步：裁切并压缩为固定240 × 320px
        compressed = compress_image(image_bytes)

        # 第三步：只上传一张图片到R2
        image_url, image_key = upload_to_r2(
            s3,
            domain,
            compressed
        )

        logger.info(
            "[IMAGE] %s | 240x320px | %.1f KB | %s",
            domain,
            len(compressed) / 1024,
            image_url
        )

        # 第四步：D1保存唯一图片地址
        update_image(
            domain,
            image_url,
            image_key
        )

        totals["success"] += 1

        logger.info(
            "[SUCCESS] %s",
            domain
        )

    except Exception as e:

        error = str(e)[:500]

        try:

            mark_image_failed(
                domain,
                error
            )

        except Exception:

            logger.exception(
                "更新截图失败状态也失败：%s",
                domain
            )

        totals["failed"] += 1

        logger.error(
            "[FAILED] %s | %s",
            domain,
            error
        )


# ============================================================
# 12. 主程序
# ============================================================

def main():

    logger.info("====================================")
    logger.info("Deople Screenshot启动")
    logger.info("最终图片尺寸：%s × %s px", IMAGE_WIDTH, IMAGE_HEIGHT)
    logger.info("截图模式：仅电脑端，不生成手机版")
    logger.info("每批数量：%s", BATCH_SIZE)
    logger.info("最多处理批次：%s", MAX_BATCHES)
    logger.info("R2 Bucket：%s", R2_BUCKET)
    logger.info("====================================")

    s3 = create_r2_client()

    totals = {
        "success": 0,
        "failed": 0
    }

    batch_number = 0

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-dev-shm-usage",
                "--no-sandbox"
            ]
        )

        try:

            while batch_number < MAX_BATCHES:

                websites = claim_pending_screenshots(
                    BATCH_SIZE
                )

                if not websites:

                    logger.info(
                        "没有更多合格的待截图网站"
                    )

                    break

                batch_number += 1

                logger.info(
                    "开始第%s批，网站数量：%s",
                    batch_number,
                    len(websites)
                )

                for website in websites:

                    process_website(
                        browser,
                        s3,
                        website,
                        totals
                    )

                logger.info(
                    "第%s批完成 | 成功：%s | 失败：%s",
                    batch_number,
                    totals["success"],
                    totals["failed"]
                )

        finally:

            browser.close()

    logger.info("====================================")
    logger.info("Deople Screenshot完成")
    logger.info("截图成功：%s", totals["success"])
    logger.info("截图失败：%s", totals["failed"])
    logger.info("====================================")


if __name__ == "__main__":
    main()
