"""
打包成 exe 时的入口脚本。

为什么不直接用 `src/spankbang_dl/__main__.py`：那个文件里是相对导入
（`from .cli import main`），只有作为包的一部分运行时才成立。PyInstaller
把入口脚本当顶层模块分析，相对导入会直接失败。所以这里用绝对导入。
"""

import sys

from spankbang_dl.cli import main

if __name__ == "__main__":
    sys.exit(main())
