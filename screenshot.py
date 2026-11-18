import os
import io
import re
import time
import socket
import ipaddress
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
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
IMAGE_BASE_URL = os.getenv("IMAGE_BASE_URL", "https://img.deople.ai").rstrip("/")

BATCH_SIZE = int(os.getenv("SCREENSHOT_BATCH_SIZE", "100"))
MAX_BATCHES = int(os.getenv("SCREENSHOT_MAX_BATCHES", "5"))
WORKERS = max(1, min(int(os.getenv("SCREENSHOT_WORKERS", "3")), 5))

IMAGE_WIDTHS = [240, 300]
IMAGE_QUALITY = 75
PAGE_TIMEOUT = 30000
WAIT_AFTER_LOAD = 1000
VIEWPORT_WIDTH = 1280
VIEWPORT_HEIGHT = 800
MAX_SCREENSHOT_HEIGHT = 12000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s",
)
logger = logging.getLogger("DeopleScreenshot")


# ============================================================
# 2. R2
# ============================================================
def check_config():
    required = {
        "R2_ACCOUNT_ID": R2_ACCOUNT_ID,
        "R2_ACCESS_KEY": R2_ACCESS_KEY,
        "R2_SECRET_KEY": R2_SECRET_KEY,
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise RuntimeError("缺少R2环境变量：" + ", ".join(missing))


def create_r2_client():
    check_config()
    return boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto",
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 3, "mode": "standard"},
            max_pool_connections=10,
        ),
    )


# ============================================================
# 3. 网站安全检查（防止访问内网地址）
# ============================================================
def is_safe_url(url):
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False
        if parsed.username or parsed.password:
            return False
        port = parsed.port
        if port is not None and port not in (80, 443):
            return False
        addresses = socket.getaddrinfo(
            parsed.hostname,
            port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
        if not addresses:
            return False
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if not ip.is_global:
                return False
        return True
    except Exception:
        return False


def check_browser_request(route):
    if not is_safe_url(route.request.url):
        route.abort()
    else:
        route.continue_()


# ============================================================
# 4. 领取合格的待截图网站
# ============================================================
def claim_pending_screenshots(limit=100):
    limit = max(1, min(int(limit), 100))
    sql = """
        SELECT domain, url, final_url
        FROM websites
        WHERE quality_status = 'qualified'
          AND crawl_status = 'success'
          AND image_status = 'pending'
        ORDER BY first_seen ASC
        LIMIT ?
    """
    websites = _rows(_query(sql, [limit]))
    claimed = []

    for website in websites:
        domain = website.get("domain")
        if not domain:
            continue
        update_sql = """
            UPDATE websites
            SET image_status = 'processing', updated_at = ?
            WHERE domain = ?
              AND quality_status = 'qualified'
              AND crawl_status = 'success'
              AND image_status = 'pending'
        """
        _query(update_sql, [int(time.time()), domain])
        claimed.append(website)

    logger.info("领取待截图网站：%s个", len(claimed))
    return claimed


# ============================================================
# 5. 网页截图
# ============================================================
def capture_screenshot(browser, url):
    if not is_safe_url(url):
        raise ValueError("网站地址不安全或无法解析")

    context = browser.new_context(
        viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
        device_scale_factor=1,
        ignore_https_errors=False,
    )
    try:
        context.route("**/*", check_browser_request)
        page = context.new_page()
        page.set_default_timeout(PAGE_TIMEOUT)
        response = page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        if response and response.status >= 400:
            raise RuntimeError(f"网页HTTP状态异常：{response.status}")

        page.wait_for_timeout(WAIT_AFTER_LOAD)
        page.evaluate("""() => {
            document.documentElement.style.maxHeight = '12000px';
            document.documentElement.style.overflow = 'hidden';
            if (document.body) {
                document.body.style.maxHeight = '12000px';
                document.body.style.overflow = 'hidden';
            }
        }""")
        return page.screenshot(
            full_page=True,
            type="jpeg",
            quality=85,
            animations="disabled",
        )
    finally:
        context.close()


# ============================================================
# 6. 图片压缩及R2上传
# ============================================================
def compress_image(image_bytes, target_width):
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    width, height = image.size
    if height > MAX_SCREENSHOT_HEIGHT:
        image = image.crop((0, 0, width, MAX_SCREENSHOT_HEIGHT))
    width, height = image.size
    if width > target_width:
        new_height = max(1, int(height * target_width / width))
        image = image.resize((target_width, new_height), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=IMAGE_QUALITY, optimize=True)
    return output.getvalue()


def make_image_key(domain, width):
    safe_domain = re.sub(r"[^a-z0-9.-]", "_", str(domain).lower().strip())
    if not safe_domain:
        raise ValueError("无效的网站域名")
    return f"{safe_domain}_{width}.jpg"


def upload_to_r2(s3, domain, image_bytes, width):
    image_key = make_image_key(domain, width)
    s3.put_object(
        Bucket=R2_BUCKET,
        Key=image_key,
        Body=image_bytes,
        ContentType="image/jpeg",
        CacheControl="public, max-age=31536000, immutable",
    )
    return f"{IMAGE_BASE_URL}/{image_key}", image_key


# ============================================================
# 7. 单个网站处理
# ============================================================
def process_website(browser, s3, website):
    domain = website.get("domain", "")
    url = website.get("final_url") or website.get("url") or ""
    logger.info("开始截图：%s", domain)

    try:
        if not url:
            raise ValueError("网站URL为空")

        image_bytes = capture_screenshot(browser, url)
        image_urls = {}

        for width in IMAGE_WIDTHS:
            compressed = compress_image(image_bytes, width)
            image_url, _ = upload_to_r2(s3, domain, compressed, width)
            image_urls[str(width)] = image_url
            logger.info(
                "[IMAGE] %s | %spx | %.1f KB | %s",
                domain, width, len(compressed) / 1024, image_url,
            )

        update_image(domain, image_urls["240"], f"{domain}_240.jpg")
        logger.info("[SUCCESS] %s", domain)
        return True

    except Exception as exc:
        error = str(exc)[:500]
        try:
            mark_image_failed(domain, error)
        except Exception:
            logger.exception("更新截图失败状态也失败：%s", domain)
        logger.error("[FAILED] %s | %s", domain, error)
        return False


# ============================================================
# 8. 并行工作进程：每个线程独立启动 Playwright 和 Chromium
# ============================================================
def worker_main(worker_id, websites):
    local_totals = {"success": 0, "failed": 0}
    if not websites:
        return local_totals

    logger.info("工作进程 %s 启动，分配网站：%s", worker_id, len(websites))
    s3 = create_r2_client()

    # Playwright 的同步 API 不跨线程共享；每个 worker 自己启动实例和浏览器。
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--no-sandbox"],
        )
        try:
            for website in websites:
                ok = process_website(browser, s3, website)
                local_totals["success" if ok else "failed"] += 1
        finally:
            browser.close()

    logger.info(
        "工作进程 %s 完成 | 成功：%s | 失败：%s",
        worker_id, local_totals["success"], local_totals["failed"],
    )
    return local_totals


# ============================================================
# 9. 主程序
# ============================================================
def split_into_groups(items, group_count):
    groups = [[] for _ in range(group_count)]
    for index, item in enumerate(items):
        groups[index % group_count].append(item)
    return [group for group in groups if group]


def main():
    logger.info("====================================")
    logger.info("Deople Screenshot 启动（并行版）")
    logger.info("并行浏览器：%s", WORKERS)
    logger.info("每批数量：%s", BATCH_SIZE)
    logger.info("最多批次：%s", MAX_BATCHES)
    logger.info("单次理论领取上限：%s", min(BATCH_SIZE, 100) * MAX_BATCHES)
    logger.info("图片宽度：%s", IMAGE_WIDTHS)
    logger.info("R2 Bucket：%s", R2_BUCKET)
    logger.info("====================================")

    check_config()
    totals = {"success": 0, "failed": 0}

    for batch_number in range(1, MAX_BATCHES + 1):
        websites = claim_pending_screenshots(BATCH_SIZE)
        if not websites:
            logger.info("没有更多合格的待截图网站")
            break

        logger.info("开始第%s批，领取网站：%s", batch_number, len(websites))
        groups = split_into_groups(websites, min(WORKERS, len(websites)))

        with ThreadPoolExecutor(max_workers=len(groups), thread_name_prefix="shot") as executor:
            futures = [
                executor.submit(worker_main, worker_id, group)
                for worker_id, group in enumerate(groups, start=1)
            ]
            for future in as_completed(futures):
                try:
                    result = future.result()
                    totals["success"] += result["success"]
                    totals["failed"] += result["failed"]
                except Exception:
                    # 若 worker 在处理循环外崩溃，日志会保留异常；对应 processing 状态需人工检查/恢复。
                    logger.exception("截图工作进程异常退出")

        logger.info(
            "第%s批完成 | 累计成功：%s | 累计失败：%s",
            batch_number, totals["success"], totals["failed"],
        )

    logger.info("====================================")
    logger.info("Deople Screenshot 完成")
    logger.info("截图成功：%s", totals["success"])
    logger.info("截图失败：%s", totals["failed"])
    logger.info("====================================")


if __name__ == "__main__":
    main()
