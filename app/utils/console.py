"""控制台工具：把标准流切到 UTF-8。

Windows 控制台默认 gbk 编码，Agent 输出的 emoji（💭🔧💾📋 等）会触发
UnicodeEncodeError 导致程序崩溃。所有会打印 Agent 过程的入口都应先调用本函数。
"""

import sys


def enable_utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
