"""
「已下载完成.json」——一份跨运行累积的**已下载账本**。

和 `下载清单.json` 的区别
-------------------------
* `下载清单.json` 是**每次运行重写的运行报告**（含失败清单、本次统计）。
* `已下载完成.json` 是**只增不删的账本**，用来记住「这些 id 我确实拿到过」。

为什么需要它
------------
判断「要不要下载」看两处：

    ① 递归扫描保存目录，读每个视频内嵌的 stream id
    ② 这份账本里的 id

只靠 ① 的话，视频一旦被移出目录、或者删掉，就会被当成没下过而重下。
账本正好补上这一块：**只要 id 还记在里面，删掉文件也不会重下**；
想重下必须同时把账本里那一条也删掉。

格式
----
以 stream id 为键，值为：

    {
      "分辨率": "1080p",
      "标题": "...",
      "文件名": "...mp4",
      "来源": "https://vdownload-45.sb-cd.com/1/7/17026324-1080p.mp4",
      "来自列表": "https://zh.spankbang.com/dup8k/playlist/ai+porn",
      "时长": "3:50",
      "文件大小": "150.76 MB"
    }

写入策略是**合并，不是重建**：以现有内容为底，只更新/新增本次下载的 id，
没碰过的条目原样保留（连字段顺序都不动），免得把手工整理的内容弄坏。
"""

from __future__ import annotations

import json
import logging
import os

from .console import human_duration, human_size
from .naming import resolution_of

logger = logging.getLogger(__name__)

DEFAULT_DONE_FILE = "已下载完成.json"

# 条目字段顺序（写出来的 JSON 就按这个顺序，方便人看）
FIELDS = ("分辨率", "标题", "文件名", "来源", "来自列表", "时长", "文件大小")


def done_path(outdir: str, name: str = DEFAULT_DONE_FILE) -> str:
    return os.path.join(str(outdir), name)


def load_done(path: str) -> dict:
    """
    读账本，返回 `{stream_id: 条目}`。

    文件不存在、内容坏了、不是对象——一律当作空账本，不报错也不挡流程：
    这东西是辅助，不该因为它坏掉就跑不起来。
    """
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("读取 %s 失败（当作空账本）：%s", path, exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("%s 的内容不是对象（当作空账本）", path)
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


def build_entry(info: dict) -> dict:
    """
    把一条视频信息整理成账本条目。

    info 可以是两种来源，字段名略有不同，这里统一：
    * 扫描出来的（metadata.scan_library）：有 source / 时长 / 文件大小
    * 刚下完的（pipeline 写入）：有 标题 / 来源 / 时长 / 文件大小
    """
    source = info.get("来源") or info.get("source") or ""
    size = info.get("文件大小") or 0
    return {
        "分辨率": resolution_of(source) or info.get("分辨率", ""),
        "标题": info.get("标题", ""),
        "文件名": info.get("文件名", ""),
        "来源": source,
        "来自列表": info.get("来自列表") or info.get("来源列表", ""),
        "时长": human_duration(info.get("时长") or 0),
        "文件大小": human_size(size),
    }


def _sort_key(key: str):
    """id 是纯数字的按数值排；不是数字的排后面，按字符串。"""
    try:
        return (0, int(key), "")
    except (TypeError, ValueError):
        return (1, 0, str(key))


def merge_done(path: str, new_entries: dict) -> tuple[bool, str, int]:
    """
    把 new_entries（`{stream_id: 信息}`）合并进账本并落盘。

    返回 (是否成功, 错误说明, 合并后的总条目数)。

    没被 new_entries 覆盖的旧条目原样保留（字段顺序都不动），
    所以如果你手工往里面加过条目，脚本不会把它们弄坏。
    """
    existing = {}
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
        if isinstance(raw, dict):
            existing = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        # 读坏了不能直接覆盖——那等于把用户的东西删了。宁可报错停下来。
        return False, f"现有账本读取失败，为避免覆盖已中止：{exc}", 0

    merged = dict(existing)
    for stream_id, info in new_entries.items():
        merged[str(stream_id)] = build_entry(info)

    ordered = {k: merged[k] for k in sorted(merged, key=_sort_key)}

    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(ordered, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    except OSError as exc:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False, f"写入失败：{exc}", len(ordered)

    logger.debug("账本合并完成：共 %d 条", len(ordered))
    return True, "", len(ordered)


__all__ = [
    "DEFAULT_DONE_FILE",
    "FIELDS",
    "build_entry",
    "done_path",
    "load_done",
    "merge_done",
]
