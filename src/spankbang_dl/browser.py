"""
浏览器引擎：用 Playwright 驱动系统 Chrome。

为什么用 `launch_persistent_context` 而不是 `launch`
----------------------------------------------------
Cloudflare 放行后会种下 `cf_clearance` 之类的 Cookie。持久化 profile 能把这些
保留下来，第二次访问就不用再挑战一遍。也正因如此，每个并行进程必须用**独立的
profile 目录**——Chrome 会锁住 profile，共用会直接启动失败。

----------------------------------------
* `headless=False`：这个站对 headless 判定很严，实测 headless 必被拦。
* `--disable-blink-features=AutomationControlled`：抹掉 `navigator.webdriver`。
* 页面加载前挂 shadowRoot 钩子，以便处理 Turnstile 控件。

实测过的组合（详见 README）
--------------------------
只有「Playwright + 系统 Chrome + 有头 + 持久化 profile」能过本站 Cloudflare；
无头模式、Camoufox(Firefox)、Patchright 都会被 `Just a moment...` 拦住。
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from . import cloudflare
from .config import CHROME_ARGS, VIEWPORT

logger = logging.getLogger(__name__)

# 关闭浏览器最多等这么久：正常情况下不到 1 秒，真卡住了也不能拖死 Ctrl+C
CLOSE_TIMEOUT = 8.0


async def _close_quietly(context) -> None:
    """关浏览器，但绝不因为关不掉而卡住退出。"""
    try:
        await asyncio.wait_for(asyncio.shield(context.close()), timeout=CLOSE_TIMEOUT)
    except asyncio.CancelledError:
        logger.debug("关闭浏览器被取消")
    except TimeoutError:
        logger.warning("关闭浏览器超时（%.0f 秒），直接放弃", CLOSE_TIMEOUT)
    except Exception as exc:
        logger.debug("关闭浏览器出错：%s", exc)


def ensure_writable_temp(fallback_root: Path | None = None,
                         prefer: Path | None = None) -> Path:
    """
    给 Playwright 指定一个确实可写的临时目录，并写回 TEMP / TMP 环境变量。

    为什么不能直接信任 `tempfile.gettempdir()`
    ------------------------------------------
    在 `uv run` 等环境里它可能返回一个不存在或不可写的目录（实测直接返回了项目
    目录），而 Playwright 的 Node 驱动是按 `TEMP` / `TMP` 环境变量自己解析临时
    目录的，两者不一致时就会在启动阶段直接报：
        EPERM: operation not permitted, mkdtemp '...\\playwright-artifacts-XXXXXX'

    所以这里按优先级**实际探测**几个候选目录，挑第一个真能建目录的，
    并同时设置 Python 侧和环境变量，确保 Node 驱动用的是同一个。
    """
    candidates: list[Path] = []
    if prefer:
        candidates.append(Path(prefer))
    candidates.append((fallback_root or Path.cwd()) / ".tmp")
    candidates.append(Path.cwd() / ".tmp")
    try:
        candidates.append(Path(tempfile.gettempdir()))
    except Exception:
        pass

    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = tempfile.mkdtemp(prefix=".probe-", dir=str(candidate))
            os.rmdir(probe)
        except Exception:
            continue
        os.environ["TEMP"] = str(candidate)
        os.environ["TMP"] = str(candidate)
        os.environ["TMPDIR"] = str(candidate)
        tempfile.tempdir = str(candidate)
        logger.info("临时目录 -> %s", candidate)
        return candidate

    raise RuntimeError("找不到可写的临时目录，请用 --tmp-dir 手工指定")

def _chrome_kwargs(profile: Path, headless: bool) -> dict:
    return {
        "user_data_dir": str(profile),
        "channel": "chrome",
        "headless": headless,
        "args": list(CHROME_ARGS),
        "viewport": dict(VIEWPORT),
        "locale": "en-US",
        "ignore_default_args": ["--enable-automation"],
    }


@asynccontextmanager
async def open_browser(profile: Path, headless: bool = False):
    """
    启动一个浏览器实例，yield 一个 Page。

    每个调用都用独立的 profile 目录，互不干扰。
    """
    profile.mkdir(parents=True, exist_ok=True)

    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            **_chrome_kwargs(profile, headless)
        )
        try:
            page = await context.new_page()
            await cloudflare.install_hook(page)
            yield page
        finally:
            await _close_quietly(context)
