"""
Cloudflare 挑战处理。

之所以需要这些代码
------------------
SpankBang 前面挂着 Cloudflare。普通 HTTP 请求会被返回 403，即使换成
真实浏览器，遇到 `Just a moment...` 的 JS 挑战时也需要等它自己放行；
如果弹出的是 Turnstile 人机校验控件，还得真的去点那个复选框。

难点在于：Turnstile 的复选框藏在**多层 Shadow DOM** 里，普通的
`page.query_selector` 根本够不着。这里沿用
https://github.com/wukong2468/cloudflare_captcha_solver 的思路：
在页面加载前挂一个 init script，把每次 `attachShadow` 产生的 shadowRoot
额外暴露到 `element.shadowRootUnl` 上，之后就能顺着这条链子找到控件并点击。
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

# 页面加载前挂上：把 shadowRoot 额外暴露一份出来
SHADOW_ROOT_HOOK = """
(() => {
    const originalAttachShadow = Element.prototype.attachShadow;
    Element.prototype.attachShadow = function (init) {
        const shadowRoot = originalAttachShadow.call(this, init);
        this.shadowRootUnl = shadowRoot;
        return shadowRoot;
    };
})();
"""

# 收集页面上所有（含嵌套）shadowRoot
_JS_COLLECT_SHADOW_ROOTS = """
() => {
    const roots = [];
    function collect(node) {
        if (!node) return;
        if (node.shadowRootUnl) {
            roots.push(node.shadowRootUnl);
            node = node.shadowRootUnl;
        }
        for (const el of node.querySelectorAll('*')) {
            if (el.shadowRootUnl) collect(el);
        }
    }
    collect(document);
    return roots;
}
"""

_CHALLENGE_TITLES = ("just a moment", "attention required", "checking your browser")
_TURNSTILE_SELECTORS = (
    'input[name="cf-turnstile-response"]',
    'script[src*="challenges.cloudflare.com/turnstile"]',
)
# 挑战 iframe 的 src 里会带这些片段
_FRAME_HINTS = (
    "challenges.cloudflare.com",
    "cdn-cgi/challenge-platform",
)


async def install_hook(page) -> None:
    """挂 shadowRoot 钩子。必须在 goto 之前调用。"""
    try:
        await page.add_init_script(SHADOW_ROOT_HOOK)
    except Exception as exc:  # 个别引擎不支持 add_init_script，失败就算了
        logger.debug("add_init_script 失败：%s", exc)


async def detect_challenge(page) -> tuple[bool, str]:
    """
    判断当前是不是 Cloudflare 挑战页。

    返回 (是否被拦, 说明)。常见形态：
      * `Just a moment...` —— JS 挑战页，通常几秒后自己放行
      * Turnstile 控件 —— 需要点复选框
    """
    try:
        title = (await page.title()) or ""
    except Exception:
        return False, ""

    if any(t in title.lower() for t in _CHALLENGE_TITLES):
        return True, f"挑战页标题 {title!r}"

    for selector in _TURNSTILE_SELECTORS:
        try:
            if await page.query_selector(selector):
                return True, "检测到 Turnstile 控件"
        except Exception:
            continue

    return False, ""


async def _collect_shadow_roots(page) -> list:
    try:
        handle = await page.evaluate_handle(_JS_COLLECT_SHADOW_ROOTS)
        properties = await handle.get_properties()
        return [h.as_element() for h in properties.values() if h.as_element()]
    except Exception as exc:
        logger.debug("收集 shadowRoot 失败：%s", exc)
        return []


async def _click_if_visible(element) -> bool:
    try:
        if await element.is_visible():
            await element.click()
            logger.info("已点击 Cloudflare 复选框")
            return True
    except Exception as exc:
        logger.debug("点击复选框失败：%s", exc)
    return False


async def _find_shadow_elements(queryable, selector: str) -> list:
    """
    在 queryable（Page 或 Frame）的所有 shadowRoot 里按选择器找元素。

    普通 `query_selector` 进不去 shadow DOM，所以要先靠 init script 暴露出来的
    `shadowRootUnl` 把整棵 shadow 树收集起来，再逐层查询。
    """
    found = []
    for root in await _collect_shadow_roots(queryable):
        try:
            handle = await root.evaluate_handle(
                f"shadow => shadow.querySelector({selector!r})"
            )
            element = handle.as_element()
            if element:
                found.append(element)
        except Exception:
            continue
    return found


async def _challenge_frames(page) -> list:
    """
    找出 Cloudflare 挑战所在的 iframe。

    Turnstile 的 iframe 常常挂在 shadow DOM 里，普通 `page.frames` 不一定能直接
    拿到，所以两条路都走一遍。
    """
    frames = []

    # ① 直接挂在文档里的 iframe
    try:
        for element in await page.query_selector_all("iframe"):
            src = (await element.get_attribute("src")) or ""
            if any(hint in src for hint in _FRAME_HINTS):
                frame = await element.content_frame()
                if frame:
                    frames.append(frame)
    except Exception:
        pass

    # ② 藏在 shadow DOM 里的 iframe
    for element in await _find_shadow_elements(page, "iframe"):
        try:
            src = await (await element.get_property("src")).json_value()
            if src and any(hint in src for hint in _FRAME_HINTS):
                frame = await element.content_frame()
                if frame:
                    frames.append(frame)
        except Exception:
            continue

    return frames


async def try_click_turnstile(page) -> bool:
    """
    找到并点击 Turnstile 的复选框。

    兜底顺序：主文档 shadow DOM → 主文档普通选择器 → 挑战 iframe 内部（含它
    自己的 shadow DOM）→ 直接点 iframe 中心。Cloudflare 各版本 DOM 结构差异
    很大，多试几种总有一种能命中。
    """
    clicked = False

    # ① 主文档的 shadow DOM
    for element in await _find_shadow_elements(page, 'input[type="checkbox"]'):
        clicked = await _click_if_visible(element) or clicked

    # ② 主文档的普通选择器（有些版本不打 shadow DOM）
    try:
        for element in await page.query_selector_all('input[type="checkbox"]'):
            clicked = await _click_if_visible(element) or clicked
    except Exception:
        pass

    # ③ 挑战 iframe 内部
    frames = await _challenge_frames(page)
    for frame in frames:
        for element in await _find_shadow_elements(frame, 'input[type="checkbox"]'):
            clicked = await _click_if_visible(element) or clicked
        try:
            for element in await frame.query_selector_all('input[type="checkbox"]'):
                clicked = await _click_if_visible(element) or clicked
        except Exception:
            pass

    # ④ 实在找不到控件，就直接点 iframe 中心碰运气
    if not clicked:
        for element in await _find_shadow_elements(page, "iframe"):
            try:
                if await element.is_visible():
                    box = await element.bounding_box()
                    if box and box["width"] > 20:
                        await element.click(position={"x": 30, "y": box["height"] / 2})
                        logger.info("已按坐标点击挑战区域")
                        clicked = True
            except Exception:
                continue

    return clicked


async def _solved(frame) -> bool:
    """挑战 iframe 里出现成功标记就算过了。"""
    for element in await _find_shadow_elements(frame, 'div[id="success"]'):
        try:
            if await element.is_visible():
                return True
        except Exception:
            continue
    return False


async def wait_until_cleared(page, timeout: float = 20.0, poll: float = 1.0,
                            reload_every: float = 8.0) -> bool:
    """
    等 Cloudflare 放行。

    返回 True 表示已经不在挑战页上（可以继续抓内容）。

    有些情况下挑战其实已经过了，但页面停在原地不动，需要**重新加载**才会跳到
    真正的内容页，所以这里每隔 `reload_every` 秒主动 reload 一次。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    reported = False
    last_reload = loop.time()

    while loop.time() < deadline:
        blocked, reason = await detect_challenge(page)
        if not blocked:
            if reported:
                logger.info("Cloudflare 已放行")
            return True

        if not reported:
            logger.info("遇到 Cloudflare 拦截（%s），等待放行…", reason)
            reported = True

        # 有 Turnstile 控件就主动点一下
        await try_click_turnstile(page)

        # 停太久就刷新一次，挑战通过后需要重新加载才能进内容页
        if loop.time() - last_reload > reload_every:
            last_reload = loop.time()
            try:
                await page.reload(wait_until="domcontentloaded", timeout=30_000)
                logger.debug("挑战页停留过久，已刷新")
            except Exception as exc:
                logger.debug("刷新失败：%s", exc)

        await asyncio.sleep(poll)

    logger.warning("等待 Cloudflare 放行超时（%.0f 秒）", timeout)
    return False


async def goto_with_cf(page, url: str, timeout_ms: int,
                       cf_wait: float | None = None, retry: int | None = None):
    """
    导航到 url，遇到 Cloudflare 挑战就等它过去 / 点掉它。

    返回 playwright 的 Response 对象。
    """
    from .config import CF_RETRY, CF_WAIT_SECONDS

    cf_wait = CF_WAIT_SECONDS if cf_wait is None else cf_wait
    retry = CF_RETRY if retry is None else retry

    response = None
    for attempt in range(1, retry + 1):
        response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        if await wait_until_cleared(page, timeout=cf_wait):
            return response
        logger.warning("第 %d/%d 次仍被 Cloudflare 拦住，重试", attempt, retry)
        await asyncio.sleep(2)
    return response
