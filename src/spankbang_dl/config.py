"""全局配置常量。"""

from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------- 站点
SITE_ORIGIN = "https://spankbang.com"

# 列表条目：播放列表页和搜索页都用这个类；采集时还要排除顶部导航里的同类元素
SEL_LIST_ITEMS = ".js-video-item"
# 分页「下一页」（两类页面结构一致）
SEL_PLAYLIST_NEXT = "div.pagination li.next a[href]"

# 视频页：各档清晰度在页面全局变量 stream_data 里
QUALITY_ORDER = ["4k", "1080p", "720p", "480p", "320p", "240p"]

# ---------------------------------------------------------------- 下载
DOWNLOAD_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
)
DOWNLOAD_REFERER = SITE_ORIGIN + "/"
CHUNK_SIZE = 1 << 20            # 1 MB
PART_SUFFIX = ".part"

# ---------------------------------------------------------------- 浏览器
# 每个并行解析进程一份独立的 profile（Chrome 会锁 profile 目录，不能共用）
DEFAULT_PROFILE_DIR = Path("profiles")
# 现在每条都要开视频页解析身份，解析进程多一点总耗时才不至于太长
DEFAULT_RESOLVERS = 10          # 并行 Chrome 进程数
DEFAULT_WORKERS = 50            # 并行下载数

CHROME_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--no-default-browser-check",
    "--no-first-run",
]

NAV_TIMEOUT_MS = 60_000         # 单次导航超时
STREAM_WAIT_MS = 30_000         # 等 stream_data 出现
# Cloudflare：没有挑战时立即返回，不受这个值影响。
# 挑战页常常「其实已经过了但页面停在原地」，所以要主动刷新才会进内容页，
# 因此这里给一个不算长的等待 + 多次重试，而不是傻等。
CF_WAIT_SECONDS = 20.0
CF_RETRY = 3

# 有头模式。实测只有「真实 Chrome + 有头 + 持久化 profile」能稳定绕过本站
# Cloudflare；无头模式必被拦。
DEFAULT_HEADLESS = False

# 窗口尺寸（有头模式下启动的 Chrome 窗口）
VIEWPORT = {"width": 1440, "height": 900}


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
