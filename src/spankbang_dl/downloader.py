"""内置下载器：HTTP 直链下载，支持 `.part` 断点续传。

设计要点
--------
* 先写 `<文件名>.part`，全部读完才 `os.replace` 成正式文件名。
  这样保存目录里**永远不会出现「下了一半却顶着正式文件名」的文件**，
  下次运行也就不会把半成品误判成已下载。
* 下次运行看到 `.part` 还在，就发 `Range: bytes=<已有字节>-` 从断点继续。
"""

from __future__ import annotations

import os
import random
import ssl
import threading
import time
import urllib.error
import urllib.request

from .config import (
    CHUNK_SIZE,
    DOWNLOAD_REFERER,
    DOWNLOAD_UA,
    PART_SUFFIX,
    SEGMENT_RETRIES,
)
from .console import progress_bar, warn

_ssl_fallback_warned = False


class DownloadCancelled(Exception):
    """下载被中断。"""


def open_url(request: urllib.request.Request, timeout: float = 30.0):
    """
    发请求，优先做正常的 SSL 证书校验。

    万一失败（公司代理、杀软的中间人证书、系统根证书过期都可能触发），
    就降级为不校验再试一次，并且只提示一次。
    """
    global _ssl_fallback_warned
    try:
        return urllib.request.urlopen(
            request, timeout=timeout, context=ssl.create_default_context()
        )
    except urllib.error.URLError as exc:
        # urllib 会把证书错误包在 URLError.reason 里
        if not isinstance(getattr(exc, "reason", None), ssl.SSLCertVerificationError):
            raise
        if not _ssl_fallback_warned:
            _ssl_fallback_warned = True
            print()
            warn("SSL 证书校验失败，已降级为「不校验证书」继续下载")
        return urllib.request.urlopen(
            request, timeout=timeout, context=ssl._create_unverified_context()
        )


def download_http_file(
    url: str,
    dest: str,
    referer: str = DOWNLOAD_REFERER,
    stop: threading.Event | None = None,
    on_progress=None,
    retries: int = 5,
    part_path: str | None = None,
) -> tuple[bool, str, int]:
    """
    下载一条 HTTP 直链到 dest，支持断点续传。

    返回 (是否成功, 错误说明, 已写入字节数)。

    part_path
        断点文件的路径；不给就用 `dest + ".part"`。
        调用方通常传一个**按身份（id + 分辨率）命名**的路径，这样即使最终
        文件名因为磁盘状态变化而换了，中断后依然能找回同一个断点续传。

    关于重试
    --------
    实测这类 CDN 偶发 `SSL: UNEXPECTED_EOF_WHILE_READING`（TLS 握手/读取中途被
    掐断）以及「读到一半连接断掉」。这两种都是**瞬时**问题——同一条直链过一会儿
    再下就正常了。所以这里多给几次机会，并且每次都从 `.part` 断点续传，
    不会白白重下已经拿到的那部分。
    """
    part = part_path or (dest + PART_SUFFIX)
    last_error = ""
    # 断点卡死检测：实测某些视频，服务器每次都在同一个偏移处掐断连接，
    # 于是「续传 -> 立刻失败 -> 再续传」原地打转，进度一点都不涨。
    # 连续两次续传都没进展，就丢掉断点从头下。
    stalled = 0
    last_pos = -1

    for attempt in range(1, retries + 1):
        try:
            pos = os.path.getsize(part) if os.path.isfile(part) else 0
        except OSError:
            pos = 0

        if attempt > 1:
            # 退避 + 抖动，避免多个线程同时重试把对方挤掉
            delay = min(2.0 ** attempt, 15.0) + random.uniform(0, 1.5)

            if pos > 0 and pos == last_pos:
                stalled += 1
                if stalled >= 2:
                    warn(f"断点原地不动（{pos / 1048576:.1f} MB），"
                         f"丢弃断点从头下载…")
                    try:
                        os.remove(part)
                    except OSError:
                        pass
                    pos = 0
                    stalled = 0
            else:
                stalled = 0

            warn(f"第 {attempt}/{retries} 次重试"
                 f"{f'（从 {pos / 1048576:.1f} MB 续传）' if pos else ''}…")
            time.sleep(delay)

        last_pos = pos

        headers = {
            "User-Agent": DOWNLOAD_UA,
            "Referer": referer,
            "Accept": "*/*",
            "Accept-Encoding": "identity",   # 别让 gzip 干扰 Range
            "Connection": "keep-alive",
        }
        if pos > 0:
            headers["Range"] = f"bytes={pos}-"

        try:
            resp = open_url(urllib.request.Request(url, headers=headers), timeout=30)
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 403, 404, 416) and pos > 0:
                # 断点已失效或签名过期，从零重来
                last_error = f"HTTP {exc.code}（断点失效，改为从头下载）"
                pos = 0
                try:
                    os.remove(part)
                except OSError:
                    pass
                continue
            last_error = f"HTTP {exc.code} {exc.reason}"
            continue
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            continue

        with resp:
            if pos > 0 and resp.status != 206:
                pos = 0                      # 服务器忽略了 Range

            total = 0
            content_range = resp.headers.get("Content-Range")
            if content_range and "/" in content_range:
                try:
                    total = int(content_range.rsplit("/", 1)[1])
                except ValueError:
                    total = 0
            if not total:
                try:
                    total = pos + int(resp.headers.get("Content-Length") or 0)
                except ValueError:
                    total = 0

            try:
                with open(part, "ab" if pos else "wb") as handle:
                    while True:
                        if stop is not None and stop.is_set():
                            raise DownloadCancelled()
                        block = resp.read(CHUNK_SIZE)
                        if not block:
                            break
                        handle.write(block)
                        pos += len(block)
                        if on_progress:
                            on_progress(pos, total)
            except DownloadCancelled:
                return False, "已中断（进度已保存，可续传）", pos
            except Exception as exc:
                last_error = f"读取/写入失败：{type(exc).__name__}: {exc}"
                continue

        if total and pos < total:
            last_error = f"数据不完整（{pos}/{total} 字节）"
            continue

        try:
            os.replace(part, dest)
        except OSError as exc:
            # 极少数情况：另一个进程/线程还占着这个文件
            last_error = f"改名失败：{exc}"
            continue
        return True, "", pos

    return False, last_error or "下载失败", 0


# ==============================================================================
# 分段下载
#
# 后期只剩少量视频时，一条连接吃不饱带宽。这里把单个直链切成若干区间并发拉，
# 每个区间写进同一个文件的不同偏移处（独立句柄 seek 写），所以：
#   * 不需要「每段一个文件再拼接」——省掉一次全量读写，峰值磁盘占用也只要 1 份
#   * 并发总数由调用方用信号量统一封顶，这里只管下载
# ==============================================================================

def probe_range(url: str, referer: str = DOWNLOAD_REFERER,
                timeout: float = 30.0) -> tuple[int, str]:
    """
    探测服务器是否支持 Range，顺便拿到总大小。

    返回 `(总大小, 错误说明)`；总大小 > 0 表示支持分段。

    做法是请求 `Range: bytes=0-0`：
        206 + Content-Range: bytes 0-0/179438228  -> 支持，总大小 179438228
        200                                       -> 不支持（老实退回单连接）
        4xx / 异常                                -> 报错
    """
    headers = {
        "User-Agent": DOWNLOAD_UA,
        "Referer": referer,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Range": "bytes=0-0",
    }
    try:
        resp = open_url(urllib.request.Request(url, headers=headers), timeout=timeout)
    except urllib.error.HTTPError as exc:
        # 有的服务器对 0-0 也回 416，那就当作不支持
        if exc.code == 416:
            return 0, "服务器不支持 Range（416）"
        return 0, f"HTTP {exc.code} {exc.reason}"
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {exc}"

    with resp:
        if resp.status != 206:
            return 0, "服务器不支持 Range（返回 200）"
        content_range = resp.headers.get("Content-Range") or ""
        if "/" not in content_range:
            return 0, "响应里没有 Content-Range"
        try:
            total = int(content_range.rsplit("/", 1)[1])
        except ValueError:
            return 0, f"Content-Range 解析失败：{content_range}"

    if total <= 0:
        return 0, "拿不到文件总大小"
    return total, ""


def download_range(url: str, path: str, start: int, end: int,
                   referer: str = DOWNLOAD_REFERER,
                   stop: threading.Event | None = None,
                   on_bytes=None,
                   retries: int = SEGMENT_RETRIES) -> tuple[bool, str, int]:
    """
    下载 `[start, end)` 区间，写进 `path` 的对应偏移处。

    返回 (是否成功, 错误说明, 这次写入的字节数)。

    每次都新开一个文件句柄 seek 过去再写，所以多个分段互不干扰，不需要加锁。
    `end` 是**闭区间**（HTTP Range 的语义）。
    """
    want = end - start + 1
    if want <= 0:
        return True, "", 0

    last_error = ""
    for attempt in range(1, retries + 1):
        if stop is not None and stop.is_set():
            return False, "已中断", 0
        if attempt > 1:
            time.sleep(min(1.5 * attempt, 8.0) + random.uniform(0, 0.8))

        headers = {
            "User-Agent": DOWNLOAD_UA,
            "Referer": referer,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Range": f"bytes={start}-{end}",
        }
        try:
            resp = open_url(urllib.request.Request(url, headers=headers), timeout=30)
        except urllib.error.HTTPError as exc:
            # 416 = 区间不被接受（有些 CDN 对部分偏移直接 404/416，实测过）
            last_error = f"HTTP {exc.code} {exc.reason}"
            continue
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            continue

        written = 0
        try:
            with resp, open(path, "r+b") as handle:
                handle.seek(start)
                while True:
                    if stop is not None and stop.is_set():
                        raise DownloadCancelled()
                    block = resp.read(CHUNK_SIZE)
                    if not block:
                        break
                    handle.write(block)
                    written += len(block)
                    if on_bytes:
                        on_bytes(len(block))
        except DownloadCancelled:
            return False, "已中断", written
        except Exception as exc:
            last_error = f"读取/写入失败：{type(exc).__name__}: {exc}"
            continue

        if written != want:
            last_error = f"区间不完整（{written}/{want} 字节）"
            continue
        return True, "", written

    return False, last_error or "分段下载失败", 0


__all__ = [
    "DownloadCancelled",
    "download_http_file",
    "download_range",
    "open_url",
    "probe_range",
    "progress_bar",
]
