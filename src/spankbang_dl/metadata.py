"""
视频身份元数据：读写 MP4 内嵌的 stream id 与来源直链。

身份是什么
----------
**唯一标识就是 stream id**，例如 `15964832`。

它是从列表页条目的 `data-id` 直接拿到的（实测与直链里的 `_tid` 一致），
所以**列表阶段就能判断要不要下**，不用打开视频页、不用解析直链。

为什么写进文件里
----------------
标题和文件名都不可靠——同一个播放列表里出现过两条标题完全一样的视频，
也出现过标题自带冒号被截断后撞名的。把 id 写进文件本身，就变成：

* 改文件名不影响识别
* 上传者改标题不影响识别
* 两条同名视频不会互相干扰
* 删掉文件 → 身份也一起没了 → 下次自动重下

两个标签：

    ----:spankbang-dl:stream_id        = 15964832
    ----:spankbang-dl:source           = https://vdownload-43.sb-cd.com/1/5/15964832-480p.mp4

`source` 存的是**去掉签名查询串**的规范直链（带签名的那段 12 小时就失效，
存了没意义）。想从里面看实际下到的分辨率，看 `-480p` 那一段即可。

关于标签名里的 `spankbang-dl`
-----------------------------
MP4 的自定义 atom 格式是 `----:<mean>:<name>`，`mean` 按惯例写成反向域名
（比如 `com.apple.iTunes`）只是**为了避免不同软件之间撞名，并不是格式要求**。
实测 `spankbang-dl` / `dl` 这类简单名字同样能正常写入读回，所以这里就用
不带 `com.` 的短名字。

这些都是 iTunes 风格的自定义 atom，播放器会忽略，不影响播放。
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

TAG_ID = "----:spankbang-dl:stream_id"
TAG_SOURCE = "----:spankbang-dl:source"
TAG_COMMENT = "\xa9cmt"          # 给人看的备注

# 只认这些扩展名；正常下载只会得到 .mp4
SCANNABLE_EXTS = (".mp4", ".m4v", ".mov")


def _first(tags, key) -> str | None:
    """从 mutagen 标签里取出第一个值，统一成 str。"""
    value = tags.get(key)
    if not value:
        return None
    item = value[0]
    if isinstance(item, bytes):
        return item.decode("utf-8", "replace")
    return str(item)


def write_source(path: str, stream_id: str, source: str) -> tuple[bool, str]:
    """
    把身份写进 MP4。

    返回 (是否成功, 错误说明)。写失败不算致命——文件本身已经下好了，
    只是下次会被当成「没记录身份」而已，所以调用方只记警告就好。
    """
    if not str(path).lower().endswith(SCANNABLE_EXTS):
        return False, "该格式不支持写入 MP4 标签"

    try:
        from mutagen.mp4 import MP4
    except ImportError:
        return False, "未安装 mutagen"

    try:
        audio = MP4(path)
        audio[TAG_ID] = [str(stream_id).encode()]
        audio[TAG_SOURCE] = [str(source).encode()]
        audio[TAG_COMMENT] = [f"来源: {source}"]
        audio.save()
        return True, ""
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def read_source(path: str) -> dict | None:
    """
    读回身份。读不到（没有标签 / 不是 MP4 / 文件损坏）返回 None。

    顺带把时长取出来——mutagen 构造 MP4 对象时本来就会解析 moov，
    `info.length` 是白拿的，不额外花时间。
    """
    if not str(path).lower().endswith(SCANNABLE_EXTS):
        return None
    try:
        from mutagen.mp4 import MP4

        tags = MP4(path)
        stream_id = _first(tags, TAG_ID)
        if not stream_id:
            return None
        try:
            duration = float(tags.info.length) if tags.info else 0.0
        except Exception:
            duration = 0.0
        return {
            "stream_id": stream_id,
            "source": _first(tags, TAG_SOURCE) or "",
            "时长": duration,
        }
    except Exception as exc:
        logger.debug("读取 %s 的标签失败：%s", path, exc)
        return None


def walk_videos(root: str):
    """
    递归遍历目录下所有视频文件，产出 `(相对路径, 绝对路径, DirEntry)`。

    会跳过以 `.` 开头的目录（`.git` / `.venv` / `.tmp` 之类），
    免得把无关的东西也扫一遍。
    """
    root = str(root)
    if not os.path.isdir(root):
        return
    for dirpath, dirnames, filenames in os.walk(root):
        # 原地改 dirnames 才能真的阻止 os.walk 往下走
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if not name.lower().endswith(SCANNABLE_EXTS):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            yield rel, full, name


def scan_library(outdir: str) -> dict:
    """
    递归扫描保存目录，建一张 `{stream_id: 信息}` 的表。

    这就是「磁盘上已经有什么」的权威来源——不看清单、不看文件名，
    只看每个文件自己内嵌的 id。

    **递归**：下载好的视频常被手动归类到子目录里，只扫顶层会漏掉。
    `文件名` 只记纯文件名（不带目录），`相对路径` 另外记一份以便定位。
    """
    found: dict = {}
    for rel, full, name in walk_videos(outdir):
        try:
            info = read_source(full)
            if not info:
                continue
            info["文件名"] = name
            info["相对路径"] = rel
            info["路径"] = full
            info["文件状态"] = "在磁盘上"
            try:
                info["文件大小"] = os.path.getsize(full)
            except OSError:
                info["文件大小"] = 0
            found[info["stream_id"]] = info
        except OSError:
            continue
    return found


def list_unidentified(outdir: str) -> list[str]:
    """递归列出目录里没有身份标签的视频文件（手工放进去的、或老版本下的）。"""
    names = []
    for rel, full, _name in walk_videos(outdir):
        try:
            if read_source(full) is None:
                names.append(rel)
        except OSError:
            continue
    return names


def part_name(title: str, stream_id: str, ext: str = ".mp4") -> str:
    """
    断点文件的文件名：`{标题} {id}.mp4.part`

    刻意用「身份」而不是「最终文件名」来命名，这样即使磁盘上的同名文件
    增删变化、导致最终文件名换了，中断后依然能找到同一个 `.part` 续传。
    """
    return f"{title} {stream_id}{ext}.part"


__all__ = [
    "TAG_ID",
    "TAG_SOURCE",
    "list_unidentified",
    "part_name",
    "read_source",
    "scan_library",
    "walk_videos",
    "write_source",
]
