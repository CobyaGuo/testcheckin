# -*- coding: utf-8 -*-
"""commander 启动入口：双栏文件管理器。

用法：pythonw commander.pyw（无黑框）或 python commander.pyw
"""
import os
import sys


def _early_app_identity():
    """在导入 Qt / 创建任何窗口之前注册 AppUserModelID。

    Windows 任务栏按钮归属哪个 AppUserModelID 是在进程首个窗口创建时
    确定的；一旦错过，后续再注册也不会刷新，按钮会一直显示 pythonw.exe
    的默认图标。必须在 `from lib.commander.main import main`（会连带导入
    PySide6）之前完成。FreeConsole 与注册解耦：前者在某些启动方式下会
    抛异常，若共用 try 会连带吞掉 AppUserModelID 注册，这正是此前
    「图标有时有、有时没有」的时序根因。
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        try:
            ctypes.windll.kernel32.FreeConsole()
        except Exception:
            pass
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "cobyg.desktools.commander.v3")
    except Exception:
        pass


_early_app_identity()

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_TOOLS_DIR = os.path.join(_SCRIPT_DIR, "app")   # 程序主体都在 app/ 下
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from lib.commander.main import main

if __name__ == "__main__":
    main()
