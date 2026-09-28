"""子进程启动参数：不弹命令行黑框。

打包成无控制台的 exe 后，每个 ffmpeg / powershell 子进程都会自己分配一个新控制台，
一节视频十几个片段就是一串黑框在任务栏上闪。CREATE_NO_WINDOW 让它直接在后台跑，
输出照旧走管道，功能不受影响。
"""
from __future__ import annotations

import subprocess
import sys

NO_WINDOW: dict = ({"creationflags": subprocess.CREATE_NO_WINDOW}
                   if sys.platform == "win32" else {})
