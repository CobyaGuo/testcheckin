# -*- coding: utf-8 -*-
"""commander 启动入口：双栏文件管理器。

用法：pythonw commander.pyw（无黑框）或 python commander.pyw
"""
import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_TOOLS_DIR = os.path.join(_SCRIPT_DIR, "app")   # 程序主体都在 app/ 下
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from lib.commander.main import main

if __name__ == "__main__":
    main()
