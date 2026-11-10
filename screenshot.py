
import os
import io
import socket
import ipaddress
from urllib.parse import urlparse

import boto3
from botocore.config import Config
from PIL import Image
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from database import (
    claim_pending_screenshots,
    update_image,
    mark_image_failed,
)


# =====================================
# 基础配置
# =====================================

R2_ACCOUNT_ID = os.environ["R2_ACCOUNT_ID"]
R2_ACCESS_KEY = os.environ["R2_ACCESS_KEY"]
R2_SECRET_KEY = os.environ["R2_SECRET_KEY"]
R2_BUCKET = os.environ.get("R2_BUCKET", "deople-images")

# R2 绑定的公开图片域名
IMAGE_BASE_URL = os.environ.get(
    "IMAGE_BASE_URL",
    "https://img.deople.ai"
).rstrip("/")

# 每次处理数量
BATCH_SIZE = int(os.environ.get("SCREENSHOT_BATCH_SIZE", "20"))

# 图片宽度
IMAGE_WIDTH = 600

# JPG 质量
IMAGE_QUALITY = 75

# 浏览器等待时间
PAGE_TIMEOUT = 30000

# 最大截图高度，防止异常长网页占用过多内存
MAX_SCREENSHOT_HEIGHT = 12000


# =====================================
# R2 客户端
# =====================================

s3 = boto3.client(
    "s3",
    endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
    aws_access_key_id=R2_ACCESS_KEY,
    aws_secret_access_key=R2_SECRET_KEY,
    region_name="auto",
    config=Config(
        signature_version="s3v4",
        retries={"max_attempts": 3}
    )
)


# =====================================
# 网站安全检查
# =====================================

def is_safe_url(url):
    """
    只允许 HTTP/HTTPS 公网网站。
    注意：这只是基础防护，生产环境还需要网络出口层限制。
    """

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
            port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM
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
    """
    检查浏览器每一个网络请求，包括跳转和子资源。
    """

    request_url = route.request.url

    if not is_safe_url(request_url):
        route.abort()
        return

    route.continue_()


# =====================================
# 网页截图
# =====================================

def capture_screenshot(url):
    if not is_safe_url(url):
        raise ValueError("网站地址不安全或无法解析")

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage"]
        )

        try:
            context = browser.new_context(
                viewport={
                    "width": 1280,
                    "height": 800
                },
                device_scale_factor=1,
                ignore_https_errors=False
            )

            context.route("**/*", check_browser_request)

            page = context.new_page()
            page.set_default_timeout(PAGE_TIMEOUT)

            page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=PAGE_TIMEOUT
            )

            # 等待页面基本渲染
            page.wait_for_timeout(1500)

            # 限制网页高度，避免异常页面生成超大图片
            page.evaluate(
                """() => {
                    document.documentElement.style.maxHeight = "12000px";
                    document.body.style.maxHeight = "12000px";
                }"""
            )

            image_bytes = page.screenshot(
                full_page=True,
                type="jpeg",
                quality=80,
                animations="disabled"
            )

            return image_bytes

        finally:
            browser.close()


# =====================================
# 图片压缩
# =====================================

def compress_image(image_bytes):

    image = Image.open(io.BytesIO(image_bytes))
    image = image.convert("RGB")

    width, height = image.size

    # 限制图片高度
    if height > MAX_SCREENSHOT_HEIGHT:
        image = image.crop(
            (0, 0, width, MAX_SCREENSHOT_HEIGHT)
        )

    # 缩小到 600px 宽
    width, height = image.size

    if width > IMAGE_WIDTH:
        new_height = int(height * IMAGE_WIDTH / width)

        image = image.resize(
            (IMAGE_WIDTH, new_height),
            Image.Resampling.LANCZOS
        )

    output = io.BytesIO()

    image.save(
        output,
        format="JPEG",
        quality=IMAGE_QUALITY,
        optimize=True
    )

    output.seek(0)

    return output.getvalue()


# =====================================
# 上传到 Cloudflare R2
# =====================================

def upload_to_r2(domain, image_bytes):

    # 统一使用域名作为图片文件名
    image_key = f"{domain}.jpg"

    s3.put_object(
        Bucket=R2_BUCKET,
        Key=image_key,
        Body=image_bytes,
        ContentType="image/jpeg",
        CacheControl="public, max-age=31536000, immutable"
    )

    image_url = f"{IMAGE_BASE_URL}/{image_key}"

    return image_url, image_key


# =====================================
# 处理单个网站
# =====================================

def process_website(website):

    domain = website["domain"]

    url = (
        website.get("final_url")
        or website.get("url")
        or ""
    )

    print(f"开始截图: {domain}")

    try:
        image_bytes = capture_screenshot(url)

        compressed = compress_image(image_bytes)

        image_url, image_key = upload_to_r2(
            domain,
            compressed
        )

        update_image(
            domain,
            image_url,
            image_key
        )

        print(
            f"截图成功: {domain} "
            f"图片大小: {len(compressed) / 1024:.1f} KB"
        )

    except Exception as e:

        error = str(e)[:500]

        mark_image_failed(domain, error)

        print(f"截图失败: {domain} - {error}")


# =====================================
# 主程序
# =====================================

def main():

    websites = claim_pending_screenshots(BATCH_SIZE)

    if not websites:
        print("没有等待截图的网站")
        return

    print(f"本次需要截图: {len(websites)} 个网站")

    for website in websites:
        process_website(website)

    print("本次截图任务完成")


if __name__ == "__main__":
    main()
