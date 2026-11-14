
import asyncio
import ipaddress
import socket
import threading
import time
import logging
import os
from urllib.parse import urlparse, urljoin, urlencode

import certstream
import httpx

from database import insert_websites


# ==================================================
# 1. 基础配置
# ==================================================

CERTSTREAM_API_KEY = os.environ.get("CERTSTREAM_API_KEY", "").strip()

if not CERTSTREAM_API_KEY:
    raise RuntimeError(
        "缺少环境变量 CERTSTREAM_API_KEY。"
        "请在 GitHub Settings → Secrets and variables → Actions 中配置。"
    )

CERTSTREAM_URL = (
    "wss://api.certstream.dev/?"
    + urlencode({"key": CERTSTREAM_API_KEY})
)

# 同时检测的网站数量
WORKERS = 30

# 域名等待检测队列
QUEUE_SIZE = 10000

# 等待写入 D1 的网站数量
WRITE_BATCH_SIZE = 10

# D1 批量写入等待时间
FLUSH_INTERVAL = 5

# 域名最多允许两个点号
MAX_DOTS = 2

# 最大跳转次数
MAX_REDIRECTS = 3

# HTTP 超时时间
HTTP_TIMEOUT = 12

# 只收录这些状态码
ACCEPT_STATUS = set(range(200, 400))

# 内存去重上限
MAX_SEEN = 100000

# 日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

logger = logging.getLogger("DeopleCollector")


# ==================================================
# 2. 域名格式过滤
# ==================================================

def normalize_domain(raw_domain):

    if not isinstance(raw_domain, str):
        return None

    domain = raw_domain.strip().lower().rstrip(".")

    if not domain:
        return None

    # 排除通配符
    if domain.startswith("*."):
        return None

    # 排除 URL、端口、路径、用户名
    if any(char in domain for char in ["/", ":", "@", " "]):
        return None

    # 排除过长域名
    if len(domain) > 253:
        return None

    # 统计点号数量
    dot_count = domain.count(".")

    # 必须包含点号
    if dot_count == 0:
        return None

    # 最多允许两个点号
    if dot_count > MAX_DOTS:
        return None

    # 两个点号的域名，只允许以下例外
    if dot_count == 2:

        allowed = (
            domain.startswith("www.")
            or domain.endswith(".pages.dev")
            or domain.endswith(".co.uk")
        )

        if not allowed:
            return None

    # 排除 IP 地址
    try:
        ipaddress.ip_address(domain)
        return None
    except ValueError:
        pass

    # 域名转换成 ASCII
    try:
        domain = domain.encode("idna").decode("ascii")
    except UnicodeError:
        return None

    labels = domain.split(".")

    for label in labels:

        if not label:
            return None

        if len(label) > 63:
            return None

        if label.startswith("-") or label.endswith("-"):
            return None

        if not all(
            char.isalnum() or char == "-"
            for char in label
        ):
            return None

    return domain


# ==================================================
# 3. DNS 公网地址检查
# ==================================================

async def is_public_domain(domain):

    try:

        loop = asyncio.get_running_loop()

        results = await loop.getaddrinfo(
            domain,
            443,
            type=socket.SOCK_STREAM
        )

        if not results:
            return False

        # 所有解析结果都必须是公网 IP
        for result in results:

            ip = ipaddress.ip_address(result[4][0])

            if not ip.is_global:
                return False

        return True

    except Exception:

        return False


# ==================================================
# 4. 检查网站是否可访问
# ==================================================

async def check_website(client, domain):

    # 第一次 DNS 检查
    if not await is_public_domain(domain):
        return None

    # HTTPS 优先，失败后尝试 HTTP
    for scheme in ("https", "http"):

        current_url = f"{scheme}://{domain}/"

        try:

            for redirect_count in range(MAX_REDIRECTS + 1):

                parsed = urlparse(current_url)

                # 只允许 HTTP / HTTPS
                if parsed.scheme not in ("http", "https"):
                    break

                if not parsed.hostname:
                    break

                if parsed.username or parsed.password:
                    break

                if parsed.port not in (None, 80, 443):
                    break

                # 检查跳转目标
                if not await is_public_domain(parsed.hostname):
                    break

                async with client.stream(
                    "GET",
                    current_url,
                    follow_redirects=False,
                    headers={
                        "User-Agent": "DeopleBot/1.0 (+https://deople.ai)",
                        "Accept": "text/html,application/xhtml+xml"
                    }
                ) as response:

                    status = response.status_code

                    # 处理跳转
                    if status in (301, 302, 303, 307, 308):

                        location = response.headers.get("location")

                        if not location:
                            break

                        if redirect_count >= MAX_REDIRECTS:
                            break

                        current_url = urljoin(
                            str(response.url),
                            location
                        )

                        continue

                    # 只接受 200-399
                    if status in ACCEPT_STATUS:

                        content_type = response.headers.get(
                            "content-type", ""
                        ).lower()

                        # 必须是网页，不收录 PDF、图片、视频等
                        if (
                            "text/html" not in content_type
                            and "application/xhtml+xml" not in content_type
                        ):
                            return None

                        return {
                            "domain": domain,
                            "url": f"{scheme}://{domain}",
                            "final_url": str(response.url),
                            "http_status": status
                        }

                    # 其他状态码暂不收录
                    break

        except Exception:
            continue

    return None


# ==================================================
# 5. CertStream 回调
# ==================================================

def start_certstream(loop, queue):

    def callback(message, context):

        try:

            message_type = message.get("message_type")
            data = message.get("data", {})

            # Hosted CertStream 的 domains-only 流格式
            if message_type == "dns_entries":

                domains = data if isinstance(data, list) else []

            # 兼容 lite / 原始 CertStream 格式
            elif message_type == "certificate_update":

                cert = data.get("leaf_cert", {})
                domains = cert.get("all_domains", [])

            else:
                return

            for raw_domain in domains:

                domain = normalize_domain(raw_domain)

                # 不符合规则的域名直接过滤
                if not domain:
                    continue

                # 从 CertStream 线程安全地把域名放入 asyncio 队列
                def enqueue(d):

                    try:

                        queue.put_nowait(d)

                    except asyncio.QueueFull:

                        logger.warning(
                            "检测队列已满，丢弃域名：%s",
                            d
                        )

                if not loop.is_closed():

                    loop.call_soon_threadsafe(enqueue, domain)

        except Exception as e:

            logger.exception("CertStream 回调异常：%s", e)

    while True:

        try:

            logger.info("正在连接 CertStream domains-only 数据流...")

            certstream.listen_for_events(
                callback,
                url=CERTSTREAM_URL
            )

        except Exception as e:

            logger.error("CertStream 连接断开：%s", e)

        logger.info("5 秒后重新连接")

        time.sleep(5)


# ==================================================
# 6. 网站检测 Worker
# ==================================================

async def worker(queue, result_queue, client, seen, seen_lock):

    while True:

        domain = await queue.get()

        try:

            # 内存去重
            async with seen_lock:

                if domain in seen:
                    continue

                seen.add(domain)

                if len(seen) > MAX_SEEN:
                    seen.clear()
                    seen.add(domain)

            # 检查网站
            result = await check_website(
                client,
                domain
            )

            if result:

                await result_queue.put(result)

                logger.info(
                    "网站合格：%s | HTTP %s",
                    domain,
                    result["http_status"]
                )

        except Exception as e:

            logger.error(
                "网站检测错误：%s | %s",
                domain,
                e
            )

        finally:

            queue.task_done()


# ==================================================
# 7. D1 批量写入
# ==================================================

async def database_writer(result_queue):

    batch = []

    while True:

        try:

            result = await asyncio.wait_for(
                result_queue.get(),
                timeout=FLUSH_INTERVAL
            )

            batch.append(result)

            result_queue.task_done()

        except asyncio.TimeoutError:
            pass

        if not batch:
            continue

        if (
            len(batch) >= WRITE_BATCH_SIZE
            or result_queue.empty()
        ):

            current_batch = batch[:]

            try:

                inserted = await asyncio.to_thread(
                    insert_websites,
                    current_batch
                )

                batch.clear()

                logger.info(
                    "D1 写入完成：新增 %s 条，提交 %s 条",
                    inserted,
                    len(current_batch)
                )

            except Exception as e:

                logger.error(
                    "D1 写入失败，保留 %s 条等待重试：%s",
                    len(current_batch),
                    e
                )

                # 保留数据，不直接丢弃
                await asyncio.sleep(5)


# ==================================================
# 8. 主程序
# ==================================================

async def main():

    logger.info("Deople Collector 启动")

    # 域名检测队列
    queue = asyncio.Queue(
        maxsize=QUEUE_SIZE
    )

    # 合格网站队列
    result_queue = asyncio.Queue()

    # 内存去重集合
    seen = set()

    seen_lock = asyncio.Lock()

    # HTTP 连接池
    limits = httpx.Limits(
        max_connections=WORKERS,
        max_keepalive_connections=WORKERS
    )

    timeout = httpx.Timeout(
        HTTP_TIMEOUT
    )

    async with httpx.AsyncClient(
        timeout=timeout,
        limits=limits,
        follow_redirects=False,
        trust_env=False
    ) as client:

        loop = asyncio.get_running_loop()

        # CertStream 独立线程
        certstream_thread = threading.Thread(
            target=start_certstream,
            args=(loop, queue),
            daemon=True
        )

        certstream_thread.start()

        # 网站检测任务
        workers = [
            asyncio.create_task(
                worker(
                    queue,
                    result_queue,
                    client,
                    seen,
                    seen_lock
                )
            )
            for _ in range(WORKERS)
        ]

        # D1 写入任务
        writer = asyncio.create_task(
            database_writer(result_queue)
        )

        logger.info(
            "采集系统运行中：Workers=%s",
            WORKERS
        )

        try:

            await asyncio.gather(
                *workers,
                writer
            )

        finally:

            for task in workers:
                task.cancel()

            writer.cancel()


# ==================================================
# 9. 程序入口
# ==================================================

if __name__ == "__main__":

    try:

        asyncio.run(main())

    except KeyboardInterrupt:

        logger.info("Deople Collector 已停止")
