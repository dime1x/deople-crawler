"""
Deople homepage crawler.
Collects metadata plus HTML-derived structure/content statistics.
This is homepage-only analysis; it does not render JavaScript or crawl subpages.
"""
import asyncio
import ipaddress
import os
import socket
from typing import Dict
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from database import get_pending_crawl_websites, save_crawl_result

READ_LIMIT = max(1, min(int(os.getenv("CRAWL_READ_LIMIT", "100")), 500))
MAX_BATCHES = max(1, int(os.getenv("CRAWL_MAX_BATCHES", "5")))
CONCURRENCY = max(1, min(int(os.getenv("CRAWL_CONCURRENCY", "10")), 50))
TIMEOUT = float(os.getenv("CRAWL_TIMEOUT", "15"))
MAX_HTML_SIZE = int(os.getenv("CRAWL_MAX_HTML_SIZE", str(2 * 1024 * 1024)))
MAX_REDIRECTS = 5
USER_AGENT = "Mozilla/5.0 (compatible; DeopleBot/1.0; +https://deople.ai)"
semaphore = asyncio.Semaphore(max(1, CONCURRENCY))


def is_public_host(host: str) -> bool:
    """Reject private, loopback, link-local, reserved and non-public IPs."""
    try:
        addresses = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        if not addresses:
            return False
        for entry in addresses:
            ip = ipaddress.ip_address(entry[4][0])
            if not ip.is_global:
                return False
        return True
    except Exception:
        return False


def extract_page_data(html: str, page_url: str) -> Dict:
    soup = BeautifulSoup(html, "html.parser")

    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    description = ""
    desc_tag = soup.find(
        "meta", attrs={"name": lambda v: v and v.lower() == "description"}
    )
    if desc_tag:
        description = (desc_tag.get("content") or "").strip()
    if not description:
        og = soup.find(
            "meta", attrs={"property": lambda v: v and v.lower() == "og:description"}
        )
        if og:
            description = (og.get("content") or "").strip()
    keywords_tag = soup.find(
        "meta", attrs={"name": lambda v: v and v.lower() == "keywords"}
    )
    keywords = (keywords_tag.get("content") or "").strip() if keywords_tag else ""

    # Count structure/media from the original DOM.
    headings = len(soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]))
    paragraphs = len(soup.find_all("p"))
    images = soup.find_all("img")
    image_count = len(images)
    image_alt_count = sum(1 for img in images if str(img.get("alt") or "").strip())
    video_count = len(soup.find_all("video"))
    for frame in soup.find_all("iframe"):
        src = str(frame.get("src") or "").lower()
        if any(host in src for host in (
            "youtube.com", "youtu.be", "vimeo.com", "dailymotion.com",
            "twitch.tv", "player.vimeo.com"
        )):
            video_count += 1

    tables = len(soup.find_all("table"))
    lists = len(soup.find_all(["ul", "ol"]))
    forms = soup.find_all("form")
    form_count = len(forms)

    parsed_page = urlparse(page_url)
    page_host = (parsed_page.hostname or "").lower()
    internal_links = 0
    external_links = 0
    has_contact = False
    has_about = False
    has_search = bool(soup.find("input", attrs={"type": "search"}))

    for link in soup.find_all("a", href=True):
        href = str(link.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(page_url, href)
        parsed_link = urlparse(absolute)
        if parsed_link.scheme not in ("http", "https"):
            continue
        link_host = (parsed_link.hostname or "").lower()
        if link_host == page_host or link_host.endswith("." + page_host):
            internal_links += 1
            path_text = (parsed_link.path + " " + link.get_text(" ", strip=True)).lower()
            if any(word in path_text for word in ("contact", "support", "联系我们", "联系")):
                has_contact = True
            if any(word in path_text for word in ("about", "关于我们", "关于")):
                has_about = True
            if "search" in path_text or "搜索" in path_text:
                has_search = True
        else:
            external_links += 1

    for form in forms:
        form_text = (
            str(form.get("action") or "") + " " +
            form.get_text(" ", strip=True)
        ).lower()
        if "search" in form_text or "搜索" in form_text:
            has_search = True
        for field in form.find_all(["input", "button"]):
            field_text = (
                str(field.get("name") or "") + " " +
                str(field.get("placeholder") or "") + " " +
                str(field.get("aria-label") or "")
            ).lower()
            if str(field.get("type") or "").lower() == "search" or "search" in field_text or "搜索" in field_text:
                has_search = True

    # Prefer the main/article region for visible-text estimation.
    content_root = soup.find("main") or soup.find("article") or soup.body or soup
    content_soup = BeautifulSoup(str(content_root), "html.parser")
    for node in content_soup(["script", "style", "noscript", "svg", "template",
                              "nav", "footer", "form"]):
        node.decompose()
    visible_text = content_soup.get_text(" ", strip=True)
    words = len(visible_text.split())

    return {
        "title": title[:500],
        "description": description[:2000],
        "keywords": keywords[:1000],
        "word_count": words,
        "heading_count": headings,
        "paragraph_count": paragraphs,
        "image_count": image_count,
        "image_alt_count": image_alt_count,
        "video_count": video_count,
        "internal_link_count": internal_links,
        "external_link_count": external_links,
        "table_count": tables,
        "list_count": lists,
        "form_count": form_count,
        "has_search": has_search,
        "has_contact": has_contact,
        "has_about": has_about,
    }


def normalize_start_url(row: Dict) -> str:
    candidate = row.get("final_url") or row.get("url")
    if not candidate:
        candidate = "https://" + str(row["domain"])
    parsed = urlparse(str(candidate))
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Invalid website URL")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing credentials are not allowed")
    if parsed.port and parsed.port not in (80, 443):
        raise ValueError("Non-standard ports are not allowed")
    return str(candidate)


async def fetch_website(client: httpx.AsyncClient, row: Dict) -> Dict:
    domain = str(row["domain"])
    result = {
        "domain": domain, "title": "", "description": "", "keywords": "",
        "http_status": None, "final_url": "", "content_type": "",
        "crawl_status": "failed",
    }
    async with semaphore:
        try:
            current_url = normalize_start_url(row)
            for _ in range(MAX_REDIRECTS + 1):
                parsed = urlparse(current_url)
                if parsed.scheme not in ("http", "https") or not parsed.hostname:
                    raise ValueError("Invalid redirect URL")
                if parsed.port and parsed.port not in (80, 443):
                    raise ValueError("Redirect to non-standard port blocked")
                if not await asyncio.to_thread(is_public_host, parsed.hostname):
                    result["crawl_status"] = "blocked"
                    raise ValueError("Host resolves to a non-public IP")

                async with client.stream(
                    "GET", current_url,
                    headers={"User-Agent": USER_AGENT},
                    follow_redirects=False,
                ) as response:
                    result["http_status"] = response.status_code
                    result["final_url"] = str(response.url)
                    result["content_type"] = (
                        response.headers.get("content-type", "")
                        .split(";")[0].strip().lower()
                    )
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            raise ValueError("Redirect response without Location")
                        current_url = urljoin(current_url, location)
                        continue
                    if not 200 <= response.status_code < 300:
                        raise ValueError(f"HTTP status {response.status_code}")
                    if "text/html" not in result["content_type"]:
                        raise ValueError("Response is not HTML")

                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        remaining = MAX_HTML_SIZE - len(body)
                        if remaining <= 0:
                            break
                        body.extend(chunk[:remaining])
                        if len(body) >= MAX_HTML_SIZE:
                            break
                    if not body:
                        raise ValueError("Empty HTML response")
                    html = bytes(body).decode(response.encoding or "utf-8", errors="replace")
                    page_data = extract_page_data(html, result["final_url"] or current_url)
                    result.update(page_data)
                    result["crawl_status"] = "success"
                    return result
            raise ValueError("Too many redirects")
        except httpx.TimeoutException:
            print(f"[TIMEOUT] {domain}")
        except (httpx.ConnectError, httpx.NetworkError) as exc:
            print(f"[NETWORK ERROR] {domain}: {str(exc)[:180]}")
        except Exception as exc:
            print(f"[CRAWL ERROR] {domain}: {str(exc)[:180]}")
        return result


async def main():
    total = succeeded = failed = 0
    d1_write_error = False
    limits = httpx.Limits(
        max_connections=max(1, CONCURRENCY),
        max_keepalive_connections=max(1, CONCURRENCY),
    )
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(TIMEOUT), limits=limits,
        follow_redirects=False, trust_env=False,
    ) as client:
        # Hard cap the number of database batches per workflow run.
        # Defaults: 100 rows per batch x 5 batches = at most 500 websites.
        for batch_number in range(1, MAX_BATCHES + 1):
            print(f"[BATCH START] {batch_number}/{MAX_BATCHES}")
            # This query is designed to use idx_websites_crawl_pending.
            rows = get_pending_crawl_websites(READ_LIMIT)
            if not rows:
                print("[DONE] No pending crawl websites found.")
                break

            results = await asyncio.gather(*(fetch_website(client, row) for row in rows))
            batch_saved = 0
            batch_success = 0
            batch_failed = 0

            for item in results:
                try:
                    save_crawl_result(item)
                    total += 1
                    batch_saved += 1
                    if item["crawl_status"] == "success":
                        succeeded += 1
                        batch_success += 1
                    else:
                        failed += 1
                        batch_failed += 1
                except Exception as exc:
                    failed += 1
                    d1_write_error = True
                    print(f"[D1 WRITE ERROR] {item['domain']}: {str(exc)[:250]}")
                    # Do not query the same pending rows repeatedly after a D1
                    # write failure. Unwritten rows remain pending for the next run.
                    break

            print(
                f"[BATCH] fetched={len(rows)} saved={batch_saved} "
                f"success={batch_success} failed={batch_failed}"
            )

            if d1_write_error:
                print("[STOP] D1 write failed; stopping to avoid rereading pending rows.")
                break

            # Safety guard: if no row was saved, another SELECT would return
            # the same pending batch and could consume the read quota repeatedly.
            if batch_saved == 0:
                print("[STOP] No rows were saved; stopping to prevent a repeated query loop.")
                break
        else:
            print(f"[LIMIT] Reached CRAWL_MAX_BATCHES={MAX_BATCHES}; remaining websites will wait for the next run.")

    print("=" * 45)
    print("Crawler finished")
    print("Saved:", total, "Success:", succeeded, "Failed:", failed)
    if d1_write_error:
        print("Result: incomplete; retry remaining pending websites in the next run.")


if __name__ == "__main__":
    asyncio.run(main())
