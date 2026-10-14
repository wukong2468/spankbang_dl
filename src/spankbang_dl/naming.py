"""文件名清洗、直链解析、文件名分配。"""

from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime

_INVALID = re.compile(r'[\\/:*?"<>|]')
_WS = re.compile(r"\s+")
_SECURE = re.compile(r"secure=[^,&]*,(\d{9,12})")
# 直链路径形如 /1/5/15964832-480p.mp4；分辨率最短是 `4k`（2 个字符），别写成 {3,6}
_URL_PARTS = re.compile(r"/(\d+)-([0-9a-z]{2,7})\.(?:mp4|m4v|ts)\b", re.I)
_TID = re.compile(r"[?&]_tid=(\d+)")


def sanitize_filename(name: str, max_len: int = 110) -> str:
    """
    把标题清洗成合法的 Windows 文件名。

    **刻意不按冒号截断。**
    SpankBang 视频页的标题形如 `标题: 标签1, 标签2 Porn - SpankBang`，看着可以
    按第一个冒号把标签切掉，但标题本身也可能含冒号，例如：

        +賴gg9875或TG:@gg9875約妹大奶處女台灣吃魚喝茶汽旅飯店出差叫小姐
        +賴gg9875或TG:@gg9875台北台中高雄彰化南投台南新竹全台約妹大奶

    按第一个冒号切，这两条会变成同一个 `+賴gg9875或TG`，互相覆盖。
    好在**播放列表页上的标题本来就是干净的**，脚本优先用它，不需要截断。
    """
    name = re.sub(r"[\r\n\t]+", " ", name or "")

    # 只去掉明确的站点后缀
    name = re.sub(r"\s*[-–|]?\s*(HD Porn Videos\s*[-–]?\s*)?SpankBang\s*$", "",
                  name, flags=re.I)
    name = re.sub(r"\s+Porn\s*$", "", name, flags=re.I)

    name = _INVALID.sub(" ", name)
    name = _WS.sub(" ", name).strip(" .")

    try:
        from pathvalidate import sanitize_filename as _pv_sanitize

        name = _pv_sanitize(name, platform="windows", replacement_text=" ").strip(" .")
    except Exception:
        pass

    if len(name) > max_len:
        name = name[:max_len].rstrip(" .")
    return name or "video"


# ------------------------------------------------------------------------------
# 从直链里取身份
# ------------------------------------------------------------------------------

def canonical_url(url: str) -> str:
    """去掉签名查询串，得到稳定形态的直链。"""
    return (url or "").split("?", 1)[0]


def stream_id_of(url: str) -> str | None:
    """
    取出 stream id（纯数字）。

    优先读 `_tid=` 参数，读不到就从路径里抠 `/1/5/15964832-480p.mp4` 这一段。
    """
    url = url or ""
    m = _TID.search(url)
    if m:
        return m.group(1)
    m = _URL_PARTS.search(url)
    return m.group(1) if m else None


def resolution_of(url: str) -> str | None:
    """取出分辨率标签（`480p` / `1080p` / `4k` …）。"""
    m = _URL_PARTS.search(url or "")
    return m.group(2).lower() if m else None


def secure_expiry_epoch(url: str) -> int | None:
    """从直链的 `secure=xxx,<时间戳>` 里取出过期时间戳；取不到返回 None。"""
    m = _SECURE.search(url or "")
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def format_expiry(url: str) -> str:
    ts = secure_expiry_epoch(url)
    if ts is None:
        return ""
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return ""


def url_still_valid(url: str, margin: int = 300) -> bool:
    """签名直链现在还能用吗（留 margin 秒余量，避免刚提交就过期）。"""
    ts = secure_expiry_epoch(url)
    if ts is None:
        return True
    return ts - margin > time.time()


# ------------------------------------------------------------------------------
# 文件名分配
# ------------------------------------------------------------------------------

class FilenameAllocator:
    """
    决定每个视频最终落盘叫什么名字。

    规则（按需求）：
        基础名 = 标题
        若磁盘上已存在 `{标题}.mp4` → 用 `{标题} - {stream_id}.mp4`
        否则                      → 用 `{标题}.mp4`

    同一个运行期内还要防止两条并发下载抢同一个名字，所以内部记着
    「已被占用的名字」，并且带锁。
    """

    def __init__(self, outdir: str):
        self.outdir = outdir
        self.taken: set[str] = set()
        self._lock = threading.Lock()
        self._load_existing()

    def _load_existing(self) -> None:
        """把目录里已有的文件名记下来（含 .part，避免和未完成的抢名字）。"""
        try:
            for entry in os.scandir(self.outdir):
                try:
                    if entry.is_file():
                        self.taken.add(entry.name.lower())
                except OSError:
                    continue
        except OSError:
            pass

    def exists(self, name: str) -> bool:
        return name.lower() in self.taken

    def allocate(self, title: str, stream_id: str, ext: str = ".mp4") -> str:
        """给这个视频挑一个可用的文件名，并占住它。"""
        with self._lock:
            plain = f"{title}{ext}"
            if plain.lower() not in self.taken:
                self.taken.add(plain.lower())
                return plain
            tagged = f"{title} - {stream_id}{ext}"
            self.taken.add(tagged.lower())
            return tagged

    def reserve(self, name: str) -> str:
        """直接占用一个既定名字（例如沿用某个已存在的文件名）。"""
        with self._lock:
            self.taken.add(name.lower())
            return name

    def release(self, name: str) -> None:
        with self._lock:
            self.taken.discard(name.lower())


def file_ready(path: str) -> tuple[bool, int]:
    """目标文件是否已经下好（只看磁盘，0 字节当作没下好）。"""
    try:
        if os.path.isfile(path):
            size = os.path.getsize(path)
            if size > 0:
                return True, size
    except OSError:
        pass
    return False, 0
