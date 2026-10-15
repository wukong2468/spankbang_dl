"""
页面操作：采集列表条目、解析视频页的最高清晰度直链。

两种列表页
----------
SpankBang 有两类「列表」：

    播放列表   https://spankbang.com/ceha4/playlist/asian+baddies/
    搜索列表   https://spankbang.com/s/vam/
               https://zh.spankbang.com/s/%E5%90%8E%E5%85%A5/

它们的**条目容器不同**（搜索页没有 `div.main_results`）、**条目链接也不同**
（搜索页直接指向视频页，播放列表页指向需要跳转的列表条目地址），
但都满足：

* 条目元素带 `.js-video-item` 类
* 条目上有 `data-id`，**它就等于 stream id**（实测 3/3 与直链里的 `_tid` 一致）
* 分页都是 `.pagination li.next a[href]`

所以采集只认这两点，不碰链接内容 —— 这样两类页面用同一套逻辑。
"""

from __future__ import annotations

import logging

from . import cloudflare
from .config import (
    NAV_TIMEOUT_MS,
    QUALITY_ORDER,
    SEL_LIST_ITEMS,
    SITE_ORIGIN,
    STREAM_WAIT_MS,
)

logger = logging.getLogger(__name__)

# 采集列表条目：只要 data-id（= stream id）+ 标题，链接仅作兜底
_JS_ITEMS = """
(sel) => {
    // 顶部导航里也塞了同类元素（8 个），要排除掉
    const items = [...document.querySelectorAll(sel)].filter(x => !x.closest('header'));
    const rows = items.map(x => {
        const a = x.querySelector('a[href]');
        const img = x.querySelector('img');
        const ta = x.querySelector('a[title]');
        return {
            href: a ? a.getAttribute('href') : null,
            title: (img && img.getAttribute('alt')) || (ta && ta.getAttribute('title')) || '',
            stream_id: (x.dataset && x.dataset.id) ? String(x.dataset.id) : null
        };
    }).filter(r => r.href || r.stream_id);
    const nextEl = document.querySelector('div.pagination li.next a[href]');
    return {
        rows: rows,
        next: nextEl ? nextEl.getAttribute('href') : null,
        pageInfo: (document.querySelector('.pagination-page-info') || {}).textContent || ''
    };
}
"""

# 视频页：标题、时长、各档位直链
_JS_STREAM = """
() => {
    const out = { url: location.href, title: document.title,
                  hasStream: (typeof stream_data !== 'undefined') && !!stream_data };
    if (!out.hasStream) return out;

    const sd = stream_data;
    out.length = sd.length || null;
    out.streamId = sd.stream_raw_id || null;
    out.qualities = {};
    for (const k of ['4k','1080p','720p','480p','320p','240p']) {
        const arr = sd[k];
        if (Array.isArray(arr) && arr.length && arr[0]) out.qualities[k] = arr[0];
    }
    out.hls = (Array.isArray(sd.m3u8) && sd.m3u8[0]) || null;
    out.mpd = (Array.isArray(sd.mpd) && sd.mpd[0]) || null;
    out.main = (Array.isArray(sd.main) && sd.main[0]) || null;

    const v = document.querySelector('video#main_video_player_html5_api')
           || [...document.querySelectorAll('video')].sort((a,b)=>b.duration-a.duration)[0];
    if (v && isFinite(v.duration)) out.playerDuration = v.duration;
    return out;
}
"""

_JS_STREAM_READY = "() => typeof stream_data !== 'undefined' && !!stream_data"

# 等「有一条真正可见的正文条目」出现。
# 不能用 wait_for_selector('.js-video-item')：它只盯着**第一个**匹配元素，
# 而第一个恰好是顶部导航里那个隐藏的，于是永远等不到 visible，白等到超时。
_JS_ITEMS_READY = """
() => {
    const items = [...document.querySelectorAll('.js-video-item')];
    const visible = items.filter(x => !x.closest('header')
        && x.offsetParent !== null
        && x.getBoundingClientRect().height > 0);
    return visible.length > 0;
}
"""


async def collect_list(page, list_url: str, max_pages: int = 50,
                       on_page=None) -> list[dict]:
    """
    翻页采集一个列表（播放列表 / 搜索列表都行）的全部条目。

    返回 `[{"href", "title", "stream_id"}, ...]`，按 stream_id 或 href 去重并保持顺序。
    on_page: 可选的每页回调，签名 `on_page(页码, 本页条数, 新增条数, page_info)`
    """
    collected: list[dict] = []
    seen: set[str] = set()
    visited: set[str] = set()
    page_url = list_url
    page_no = 0

    while page_url and page_no < max_pages:
        page_no += 1
        if page_url in visited:
            break
        visited.add(page_url)

        await cloudflare.goto_with_cf(page, page_url, NAV_TIMEOUT_MS)
        try:
            await page.wait_for_function(_JS_ITEMS_READY, timeout=25_000)
        except Exception as exc:
            blocked, reason = await cloudflare.detect_challenge(page)
            where = page.url
            if blocked:
                logger.warning("第 %d 页仍被 Cloudflare 拦截：%s", page_no, reason)
                print(f"      {list_url} 第 {page_no} 页被 Cloudflare 拦住（{reason}）")
            else:
                logger.warning("第 %d 页找不到条目元素：%s（当前地址 %s）",
                               page_no, exc, where)
                print(f"      第 {page_no} 页没有找到条目元素"
                      f"（{type(exc).__name__}），当前地址：{where[:90]}")
            break

        data = await page.evaluate(_JS_ITEMS, SEL_LIST_ITEMS)
        rows = data.get("rows") or []
        added = 0
        for row in rows:
            href = row.get("href") or ""
            stream_id = row.get("stream_id") or ""
            key = stream_id or href
            if not key or key in seen:
                continue
            seen.add(key)
            collected.append({
                "href": href,
                "title": (row.get("title") or "").strip(),
                "stream_id": stream_id or None,
                "来源列表": list_url,
            })
            added += 1

        page_info = (data.get("pageInfo") or "").strip()
        if on_page:
            on_page(page_no, len(rows), added, page_info)

        nxt = data.get("next")
        if not nxt:
            break
        page_url = nxt if nxt.startswith("http") else SITE_ORIGIN + nxt

    return collected


def pick_best_quality(info: dict, prefer: str | None = None) -> tuple[str, str]:
    """
    挑出最高清晰度的**渐进式 MP4** 直链，返回 (档位标签, 直链)。

    只认 MP4。没有 MP4 档位时返回 ("", "")，由调用方报错跳过 ——
    不做 HLS（m3u8）回退：HLS 拼出来的不是 MP4，写不进内嵌身份标签，
    结果就是「下过了但识别不出来」，每次运行都重下。
    """
    qualities = info.get("qualities") or {}
    order = list(QUALITY_ORDER)
    if prefer and prefer in order:
        order.remove(prefer)
        order.insert(0, prefer)

    for quality in order:
        if qualities.get(quality):
            return quality, qualities[quality]
    if info.get("main"):
        return "main", info["main"]
    return "", ""


async def resolve_video(page, href: str, prefer: str | None = None,
                        retries: int = 2) -> dict:
    """
    打开一个条目（播放列表条目会自动 301 跳到视频页），解析出最高清晰度 MP4 直链。

    成功返回含 url / stream_id / 时长 的字典；失败返回 `{"error": ...}`。
    """
    url = href if href.startswith("http") else SITE_ORIGIN + href
    last_error = ""

    for _ in range(retries):
        try:
            await cloudflare.goto_with_cf(page, url, NAV_TIMEOUT_MS)
            if "/video/" not in page.url:
                await page.wait_for_url("**/video/**", timeout=15_000)
            await page.wait_for_function(_JS_STREAM_READY, timeout=STREAM_WAIT_MS)
            info = await page.evaluate(_JS_STREAM)
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {str(exc)[:120]}"
            blocked, reason = await cloudflare.detect_challenge(page)
            if blocked:
                last_error = f"被 Cloudflare 拦截（{reason}）"
            continue

        if not isinstance(info, dict) or not info.get("hasStream"):
            last_error = "页面里没有 stream_data"
            continue

        quality, direct = pick_best_quality(info, prefer)
        if not direct:
            # 区分「压根没有流」和「只有 HLS」，报错才有意义
            if info.get("hls"):
                last_error = "该视频没有 MP4 档位（只有 HLS 流），已跳过"
            else:
                last_error = "没有解析到任何 MP4 档位"
            break

        return {
            "url": direct,
            "quality": quality,
            "stream_id": str(info.get("streamId") or "") or None,
            "duration": info.get("playerDuration") or info.get("length") or 0,
            # 视频播放页地址，例如 https://spankbang.com/9pf3w/video/xxx
            # 只用来记录 / 方便回看，**不作为身份**（slug 会随标题变）
            "page_url": info.get("url") or page.url,
        }

    return {"error": last_error or "解析失败", "page_url": url}
