# -*- coding: utf-8 -*-
import asyncio
import base64
import json
import logging
import re
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, asdict
from typing import List, Optional
from urllib.parse import quote, urljoin
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from backend.storage import write_json
from spider.notify_email import notify_cookie_invalid

# -------------------- 常量配置 --------------------
POSTS_SEARCH_URL = "https://s.weibo.com/weibo?q=%23{}%23&xsort=hot&suball=1&tw=hotweibo"
MAX_POSTS_TO_FETCH = 20          # 每个话题抓取的微博数量上限
MAX_SEARCH_PAGES = 2             # 搜索列表最多翻页数
SCROLL_COUNT = 2                 # 每页滚动次数，帮助加载更多
SCROLL_DELAY_MS = 2000           # 每次滚动等待(ms)

BASE_DIR = Path(__file__).parent
USER_DATA_DIR = BASE_DIR / "browser_data_detail"  # 持久化上下文目录（保存登录态）
COOKIES_PATH = BASE_DIR / "weibo_cookies.json"    # 兼容旧流程：可选地写出 cookies
AUTH_STATE_PATH = BASE_DIR / "auth_state.json"    # Playwright storage_state 文件
ENV_PATH = BASE_DIR.parent / ".env"
ENV_COOKIE_KEY = "WEIBO_COOKIE"
QR_CODE_PATH = BASE_DIR / "weibo_login_qrcode.png"

# “正常抓取”全程无UI；登录流程使用 headless 获取二维码
HEADLESS = True
LOGIN_HEADLESS = True

# 登录页与成功判断
LOGIN_URL = "https://passport.weibo.com/sso/signin?entry=miniblog&source=miniblog"
LOGIN_COOKIE_KEYS = ("SSOLoginState", "SUB", "SUBP", "ALF")
LOGIN_TIMEOUT_SECONDS = 180
LOGIN_POLL_INTERVAL_SECONDS = 1
LOGIN_RETRY_TIMES = 3
LOGIN_SUCCESS_WAIT_SECONDS = 5
QR_CODE_SELECTOR = "img.w-full.h-full"
COOKIE_NOTIFY_LABEL = "topic_detail_login_refresh"
CONNECTION_NOTIFY_LABEL = "topic_detail_connection_error"

# -------------------- 数据结构 --------------------
@dataclass
class WeiboPost:
    author: str
    content: str
    timestamp: str
    source: str
    forwards_count: int
    comments_count: int
    likes_count: int
    image_links: List[str]
    video_link: str
    detail_url: str

    def to_dict(self):
        d = asdict(self)
        # 对外不暴露 detail_url（如你不希望前端看到，可保留；若需要可删除下面一行）
        d.pop("detail_url", None)
        return d

# -------------------- 工具函数 --------------------
def _cn_number_to_int(text: str) -> int:
    """将“12万”“3.4亿”等中文计数转换为整数"""
    if not text:
        return 0
    text = str(text).strip()
    try:
        if "亿" in text:
            return int(float(text.replace("亿", "")) * 100000000)
        if "万" in text:
            return int(float(text.replace("万", "")) * 10000)
        # 去掉非数字
        digits = re.sub(r"[^\d.]", "", text)
        if digits == "":
            return 0
        return int(float(digits))
    except Exception:
        return 0

def _persist_login_state(storage_state: dict) -> List[dict]:
    """把 storage_state 和 cookies 写回到本地，便于兼容旧流程"""
    try:
        # 写 storage_state
        write_json(AUTH_STATE_PATH, storage_state)
    except Exception:
        pass

    cookies = storage_state.get("cookies", [])
    try:
        write_json(COOKIES_PATH, cookies)
    except Exception:
        pass
    try:
        cookie_str = build_cookie_string(cookies)
        if cookie_str:
            _write_env_file(ENV_PATH, ENV_COOKIE_KEY, cookie_str)
    except Exception:
        pass
    return cookies

def _load_cookies_from_file() -> Optional[List[dict]]:
    """（可选）从 weibo_cookies.json 预置一份 Cookie 到上下文"""
    if COOKIES_PATH.exists():
        try:
            data = json.loads(COOKIES_PATH.read_text(encoding="utf-8"))
            # 兼容两种结构：纯 list 或 {"cookies": [...]}
            if isinstance(data, dict) and "cookies" in data:
                return data["cookies"]
            if isinstance(data, list):
                return data
        except Exception:
            return None
    return None

def _cookie_dict(cookies: List[dict]) -> dict:
    result = {}
    for item in cookies:
        name = item.get("name")
        value = item.get("value")
        if name and value is not None:
            result[name] = str(value)
    return result

def _is_logged_in(cookies: List[dict]) -> bool:
    cookie_map = _cookie_dict(cookies)
    return any(cookie_map.get(key) for key in LOGIN_COOKIE_KEYS)

def build_cookie_string(cookies: List[dict]) -> str:
    ordered = OrderedDict()
    for item in cookies:
        name = item.get("name")
        value = item.get("value")
        if not name or value is None:
            continue
        if name in ordered:
            ordered.pop(name)
        ordered[name] = str(value)
    return "; ".join(f"{name}={value}" for name, value in ordered.items())

def _write_env_file(path: Path, key: str, value: str) -> None:
    escaped = value.replace('"', '\\"')
    line = f'{key}="{escaped}"'
    if not path.exists():
        path.write_text(line + "\n", encoding="utf-8")
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    updated = False
    for idx, raw in enumerate(lines):
        if raw.strip().startswith(f"{key}="):
            lines[idx] = line
            updated = True
            break
    if not updated:
        lines.append(line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

async def _save_login_qrcode(page) -> Path:
    QR_CODE_PATH.parent.mkdir(exist_ok=True)
    qr = page.locator(QR_CODE_SELECTOR).first
    await qr.wait_for(state="visible", timeout=15000)
    src = await qr.evaluate("el => el.currentSrc || el.src || el.getAttribute('src')")
    saved = False

    if src:
        src = str(src).strip()
        if src.startswith("data:image"):
            _, encoded = src.split(",", 1)
            QR_CODE_PATH.write_bytes(base64.b64decode(encoded))
            saved = True
        else:
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = urljoin(page.url, src)
            elif not src.startswith("http"):
                try:
                    QR_CODE_PATH.write_bytes(base64.b64decode(src))
                    saved = True
                except Exception:
                    src = urljoin(page.url, src)

            if not saved and src.startswith("http"):
                def _download(url: str) -> bytes:
                    req = urllib.request.Request(
                        url,
                        headers={"User-Agent": "Mozilla/5.0"},
                    )
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        return resp.read()

                loop = asyncio.get_running_loop()
                data = await loop.run_in_executor(None, _download, src)
                if data:
                    QR_CODE_PATH.write_bytes(data)
                    saved = True

    if not saved:
        try:
            handle = await qr.element_handle()
            if handle:
                try:
                    await page.wait_for_function(
                        "(img) => img && img.complete && img.naturalWidth > 0",
                        handle,
                        timeout=15000,
                    )
                except PlaywrightTimeout:
                    pass
        except Exception:
            pass
        await qr.screenshot(path=str(QR_CODE_PATH))
    return QR_CODE_PATH


def _is_connection_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        token in text
        for token in (
            "net::err",
            "connection closed",
            "connection reset",
            "connection aborted",
            "timeout",
            "ssl",
            "unexpected eof",
        )
    )


def _notify_connection_error(reason: str) -> None:
    try:
        notify_cookie_invalid(CONNECTION_NOTIFY_LABEL, reason)
    except Exception:
        logging.exception("Failed to notify connection error.")

async def _wait_for_login(context, timeout_seconds: int, no_logged_in_session: str) -> List[dict]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        cookies = await context.cookies()
        if _is_logged_in(cookies):
            return cookies
        if no_logged_in_session:
            current_session = _cookie_dict(cookies).get("WBPSESS")
            if current_session and current_session != no_logged_in_session:
                return cookies
        await asyncio.sleep(LOGIN_POLL_INTERVAL_SECONDS)
    return []

# -------------------- 登录流程（二维码登录） --------------------
async def _login_and_update_cookies(
    playwright,
    *,
    notify_label: Optional[str] = None,
    notify_reason: str = "",
) -> Optional[List[dict]]:
    """
    触发二维码登录（无UI）。成功后持久化到 USER_DATA_DIR，并写回 auth_state.json / weibo_cookies.json。
    """
    USER_DATA_DIR.mkdir(exist_ok=True)

    for attempt in range(1, LOGIN_RETRY_TIMES + 1):
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            headless=LOGIN_HEADLESS,
            args=["--disable-gpu", "--disable-dev-shm-usage", "--no-sandbox"]
        )
        try:
            cookies = await context.cookies()
            if _is_logged_in(cookies):
                logging.info("已通过保存的登录状态自动登录。")
                storage = await context.storage_state()
                return _persist_login_state(storage)

            page = await context.new_page()
            await page.goto(LOGIN_URL, wait_until="load", timeout=60000)

            try:
                qr_path = await _save_login_qrcode(page)
            except Exception as exc:
                logging.warning(f"未能获取二维码（第 {attempt} 次）：{exc}")
                continue

            logging.info(f"二维码已保存：{qr_path}，请使用手机微博扫码登录。")
            if notify_label:
                try:
                    notify_cookie_invalid(notify_label, notify_reason)
                except Exception:
                    logging.exception("Failed to notify cookie invalid.")

            cookie_map = _cookie_dict(await context.cookies())
            no_logged_in_session = cookie_map.get("WBPSESS", "")
            cookies = await _wait_for_login(
                context,
                LOGIN_TIMEOUT_SECONDS,
                no_logged_in_session
            )

            if not cookies:
                logging.warning(f"登录超时（第 {attempt} 次），将重试。")
                continue

            await asyncio.sleep(LOGIN_SUCCESS_WAIT_SECONDS)
            storage = await context.storage_state()
            logging.info("登录成功，正在更新本地状态。")
            return _persist_login_state(storage)
        finally:
            try:
                await context.close()
            except Exception:
                pass

    logging.error("多次登录失败，未能更新 Cookie。")
    return None

# -------------------- 详情页抓取 --------------------
async def _get_post_details(context, detail_url: str, base_data: dict) -> Optional[WeiboPost]:
    """
    进入单条微博详情页，补充图片/视频等信息。失败返回 None。
    """
    page = await context.new_page()
    try:
        await page.goto(detail_url, wait_until="load", timeout=60000)
        # 等待正文区域出现（尽量宽松一些）
        try:
            await page.wait_for_selector("article, div.Detail, div.WB_detail", timeout=10000)
        except PlaywrightTimeout:
            pass

        html = await page.content()
        soup = BeautifulSoup(html, "html.parser")

        # 图片
        image_links = []
        for img in soup.select("img"):
            src = img.get("src") or img.get("data-src") or ""
            if src and ("wx" in src or "sinaimg" in src or "mw" in src):
                if src.startswith("//"):
                    src = "https:" + src
                image_links.append(src)
        # 去重
        image_links = list(dict.fromkeys(image_links))

        # 视频（非常粗略的抓取方式，足够用）
        video_link = ""
        video_tag = soup.find("video")
        if video_tag and video_tag.get("src"):
            video_link = video_tag.get("src")
            if video_link.startswith("//"):
                video_link = "https:" + video_link

        return WeiboPost(
            author=base_data.get("author", ""),
            content=base_data.get("content", ""),
            timestamp=base_data.get("timestamp", ""),
            source=base_data.get("source", ""),
            forwards_count=base_data.get("forwards_count", 0),
            comments_count=base_data.get("comments_count", 0),
            likes_count=base_data.get("likes_count", 0),
            image_links=image_links,
            video_link=video_link,
            detail_url=detail_url
        )
    except Exception as e:
        if _is_connection_error(e):
            _notify_connection_error(f"detail_page:{detail_url}:{e}")
        logging.warning(f"详情页抓取失败：{detail_url}，原因：{e}")
        return None
    finally:
        try:
            await page.close()
        except Exception:
            pass

# -------------------- 列表页抓取（默认静默） --------------------
async def get_top_20_hot_posts(topic_title: str) -> List[WeiboPost]:
    """
    抓取一个话题的热门微博（最多 20 条）。
    常态：全程 headless 静默，不弹窗。
    仅当检测到登录态失效时，临时弹窗一次完成登录，然后回到静默抓取。
    """
    search_url = POSTS_SEARCH_URL.format(quote(topic_title.replace("#", "")))
    logging.info(f"开始抓取话题 '{topic_title}' 的热门微博...")

    seed_cookies = _load_cookies_from_file()  # 仅用于冷启动时的种子 Cookie

    async with async_playwright() as p:
        attempt = 0
        posts: List[WeiboPost] = []

        while attempt < 2:  # 最多两轮：第一轮失败 -> 执行登录 -> 再试一轮
            attempt += 1

            USER_DATA_DIR.mkdir(exist_ok=True)
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(USER_DATA_DIR),
                headless=HEADLESS,  # ★ 常态：静默
                args=["--disable-gpu", "--disable-dev-shm-usage", "--no-sandbox", "--window-position=-32000,-32000"]
            )

            # 冷启动可补种 cookie（不会覆盖 storage_state 里已有cookie）
            try:
                if seed_cookies:
                    await context.add_cookies(seed_cookies)
            except Exception:
                pass

            page = await context.new_page()

            try:
                await page.goto(search_url, wait_until="load", timeout=60000)
                await page.wait_for_selector("div.card-wrap", timeout=10000)
                # 如果能到这里，说明无需登录，直接解析列表
            except Exception as exc:
                if _is_connection_error(exc):
                    _notify_connection_error(f"list_page:{topic_title}:{exc}")
                await context.close()

                if attempt >= 2:
                    logging.warning("连续两次访问失败，可能仍需登录。")
                    return []

                # ★ 仅此时触发登录流程（会弹窗），成功后进入下一轮静默抓取
                logging.info("疑似 Cookies/登录状态失效，开始登录刷新（将弹出浏览器窗口）...")
                seed_cookies = await _login_and_update_cookies(
                    p,
                    notify_label=COOKIE_NOTIFY_LABEL,
                    notify_reason=f"topic_detail_login_refresh:{topic_title}:{exc}",
                )
                if not seed_cookies:
                    logging.error("登录失败，放弃本话题抓取。")
                    return []
                logging.info("登录成功，已刷新状态，准备重新尝试访问。")
                continue

            # ------- 解析列表页（含滚动 & 翻页） -------
            try:
                base_search_url = search_url
                page_urls = [base_search_url] + [
                    f"{base_search_url}&page={i}" for i in range(2, MAX_SEARCH_PAGES + 1)
                ]

                initial_posts: List[dict] = []
                seen_detail_urls = set()

                async def collect_from_page(page_obj, page_index: int) -> bool:
                    # 滚动加载更多
                    if SCROLL_COUNT > 0:
                        for _ in range(SCROLL_COUNT):
                            await page_obj.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                            await page_obj.wait_for_timeout(SCROLL_DELAY_MS)
                    html = await page_obj.content()
                    soup = BeautifulSoup(html, "html.parser")
                    cards = soup.select("div.card-wrap")
                    logging.info(f"Search page {page_index} returned {len(cards)} cards.")

                    for card in cards:
                        try:
                            # 文本
                            txt = card.select_one("p.txt")
                            if not txt:
                                continue
                            content = txt.get_text(strip=True) or ""

                            # 作者
                            name_node = card.select_one(".content .info .name")
                            author = name_node.get_text(strip=True) if name_node else ""

                            # 时间 与 详情链接
                            detail_url, timestamp = "", ""
                            from_node = card.select_one(".content .from")
                            if from_node:
                                a = from_node.select_one("a")
                                if a:
                                    href = a.get("href", "")
                                    if href.startswith("//"):
                                        detail_url = "https:" + href
                                    elif href.startswith("http"):
                                        detail_url = href
                                    else:
                                        detail_url = urljoin("https://weibo.com", href)
                                    timestamp = a.get_text(strip=True)

                            if not detail_url or detail_url in seen_detail_urls:
                                continue

                            # 转评赞
                            actions = card.select(".card-act ul li a")
                            forwards = _cn_number_to_int(actions[0].get_text(strip=True)) if len(actions) > 0 else 0
                            comments = _cn_number_to_int(actions[1].get_text(strip=True)) if len(actions) > 1 else 0
                            likes = _cn_number_to_int(actions[2].get_text(strip=True)) if len(actions) > 2 else 0

                            seen_detail_urls.add(detail_url)
                            initial_posts.append({
                                "author": author,
                                "content": content,
                                "timestamp": timestamp,
                                "source": "",
                                "forwards_count": forwards,
                                "comments_count": comments,
                                "likes_count": likes,
                                "image_links": [],
                                "video_link": "",
                                "detail_url": detail_url
                            })

                            if len(initial_posts) >= MAX_POSTS_TO_FETCH:
                                return True
                        except Exception:
                            continue
                    return False

                # 第1页
                await collect_from_page(page, 1)

                # 后续页
                page_idx = 2
                while len(initial_posts) < MAX_POSTS_TO_FETCH and page_idx <= MAX_SEARCH_PAGES:
                    page_url = page_urls[page_idx - 1]
                    logging.info(f"Fetching additional search page {page_idx}: {page_url}")
                    extra = await context.new_page()
                    page_completed = False
                    try:
                        await extra.goto(page_url, wait_until="load", timeout=60000)
                        await extra.wait_for_selector("div.card-wrap", timeout=10000)
                        page_completed = await collect_from_page(extra, page_idx)
                    except Exception as exc:
                        logging.warning(f"Failed to load search page {page_idx}: {exc}")
                    finally:
                        try:
                            await extra.close()
                        except Exception:
                            pass
                    if page_completed:
                        break
                    page_idx += 1

                # 关闭搜索页
                try:
                    await page.close()
                except Exception:
                    pass

                logging.info(f"列表页抓取完成：共 {len(initial_posts)} 条，开始抓取详情页...")

                # 详情页并发抓取（适度并发，避免过快）
                sem = asyncio.Semaphore(4)

                async def wrap_detail(d):
                    async with sem:
                        return await _get_post_details(context, d.pop("detail_url"), d)

                tasks = [wrap_detail(d) for d in initial_posts]
                results = await asyncio.gather(*tasks)
                posts = [r for r in results if r is not None]

            except Exception:
                logging.error(f"抓取话题 '{topic_title}' 时发生错误", exc_info=True)
                posts = []
            finally:
                try:
                    await context.close()
                except Exception:
                    pass

            break  # 第一轮成功就跳出 while

    logging.info(f"成功抓取到 {len(posts)} 条关于 '{topic_title}' 的微博详情")
    return posts



# ---------------- 入口 / 测试 ----------------
if __name__ == '__main__':
    async def main():
        test_topic = "卢浮宫劫案可能是粉红豹所为"
        print(f"正在测试：获取话题 '{test_topic}' 的前{MAX_POSTS_TO_FETCH} 条热门微博...")
        top_posts = await get_top_20_hot_posts(test_topic)

        if top_posts:
            print(f"\n成功获取 {len(top_posts)} 条微博：")
            posts_as_dicts = [post.to_dict() for post in top_posts]
            print(json.dumps(posts_as_dicts, ensure_ascii=False, indent=2))
        else:
            print("未能获取到任何微博内容。")


    asyncio.run(main())
