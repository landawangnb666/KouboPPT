"""GUI 日志功能测试：落盘 / 时间戳与分段 / 异常堆栈 / 高度切换 / 警告汇总。

不启动完整 App（那会触发首启编码自检），而是把 App 的日志方法绑到一个最小替身上跑。
需要桌面环境（会创建一个随即隐藏的 Tk 窗口）。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import types
from pathlib import Path

import customtkinter as ctk

from kouboppt.gui import LOG_H_SHORT, LOG_H_TALL, LOG_NAME, App

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")


class FakeWidget:
    """够用的控件替身：记录 configure 收到的参数。"""

    def __init__(self):
        self.kw: dict = {}

    def configure(self, **kw):
        self.kw.update(kw)

    def set(self, v):
        self.value = v


_LOG_METHODS = ("_resolve_log_path", "_write_log_file", "_start_log_file",
                "_finish_log_file", "_fail_detail", "_summarize_warnings",
                "_toggle_log_height", "_append_log", "_export_log")


def make_stub(box):
    """最小替身：属性齐备，并把 App 的日志方法绑到它上面（等价于自绑定）。"""
    st = types.SimpleNamespace(
        log_box=box,
        log_toggle_btn=FakeWidget(),
        start_btn=FakeWidget(), cw_start_btn=FakeWidget(), cancel_btn=FakeWidget(),
        progress=FakeWidget(), status_label=FakeWidget(),
        cancel_event=FakeEvent(),
        _log_lock=threading.Lock(), _log_active=False, _log_path=None,
        _warn_count=0, _log_tall=False,
    )
    for name in _LOG_METHODS:
        setattr(st, name, types.MethodType(getattr(App, name), st))
    return st


class FakeEvent:
    def __init__(self):
        self.cleared = 0

    def clear(self):
        self.cleared += 1


class FakeDialog:
    """替身：记录 asksaveasfilename 的调用次数与参数。"""

    def __init__(self, ret):
        self.ret, self.calls, self.kw = ret, 0, {}

    def asksaveasfilename(self, **kw):
        self.calls += 1
        self.kw = kw
        return self.ret


class FakeMessagebox:
    def __init__(self):
        self.info, self.warn = [], []

    def showinfo(self, *a, **k):
        self.info.append(a)

    def showwarning(self, *a, **k):
        self.warn.append(a)


def main():
    root = ctk.CTk()
    root.withdraw()
    box = ctk.CTkTextbox(root, state="disabled")
    box.pack()
    for tag, color in (("ok", "green"), ("err", "red"), ("warn", "orange"), ("stage", "blue")):
        box._textbox.tag_config(tag, foreground=color)

    tmp = Path(tempfile.mkdtemp(prefix="koubo_log_"))
    try:
        st = make_stub(box)

        # ---- 落盘：输出目录不存在也会自动建；会话头带时间戳与运行标题
        out_dir = tmp / "输出目录"
        App._begin_run(st, "单元测试", out_dir)
        logf = out_dir / LOG_NAME
        assert logf.exists(), f"日志没落盘：{list(out_dir.rglob('*'))}"
        head = logf.read_text(encoding="utf-8")
        assert "开始运行" in head and "单元测试" in head, head
        assert "====" in head and "输出目录：" in head, head
        assert st._log_active and st._warn_count == 0, vars(st)
        assert f"日志文件：{logf}" in box.get("1.0", "end"), "运行开始该提示日志落盘位置"

        # ---- 普通行落盘并带 [HH:MM:SS] 时间戳；警告计数
        App._append_log(st, "  识别第 1/10 页")
        App._append_log(st, "⚠ 公式渲染失败，已降级")
        App._append_log(st, "⚠ 又有警告")
        assert st._warn_count == 2, st._warn_count
        body = logf.read_text(encoding="utf-8")
        assert "[0" in body or "[1" in body or "[2" in body, body      # 时间戳前缀
        assert "识别第 1/10 页" in body, body

        # ---- 异常：完整堆栈进文件，界面只留一行
        try:
            raise ValueError("章节结构读不出来\n第二行不该出现在界面")
        except ValueError as exc:
            short = App._fail_detail(st, exc)
        assert "\n" not in short, short
        assert "ValueError" in short and "详细堆栈见日志文件" in short, short
        App._append_log(st, short)          # 主线程从队列取到摘要后才落到界面
        body = logf.read_text(encoding="utf-8")
        assert "Traceback" in body and "ValueError" in body, "堆栈没写进日志文件"

        # ---- 警告汇总 + 结束行
        App._summarize_warnings(st)
        App._finish_log_file(st, 7, 1)
        body = logf.read_text(encoding="utf-8")
        assert "共 2 条警告" in body, body[-600:]
        assert "结束：产出 7 个文件，失败 1 项，警告 2 条" in body, body[-600:]
        assert st._log_active is False, "收尾后应停用文件写入"

        # ---- 收尾之后的消息不再写进上次的日志（避免串档）
        before = logf.read_text(encoding="utf-8")
        App._append_log(st, "运行之后的消息")
        assert logf.read_text(encoding="utf-8") == before, "收尾后仍写入了日志文件"

        # ---- 界面文本：分段线、警告汇总都在
        ui = box.get("1.0", "end")
        assert "开始运行" in ui, ui[-400:]
        assert "共 2 条警告" in ui, ui[-400:]
        assert "✘ 失败：ValueError: 章节结构读不出来" in ui, ui[-400:]
        assert "\n第二行不该出现在界面" not in ui, "界面不该出现堆栈后续行"

        # ---- 高度切换
        App._toggle_log_height(st)
        assert st._log_tall is True and st.log_toggle_btn.kw.get("text") == "收起", st.log_toggle_btn.kw
        assert int(box.cget("height")) == LOG_H_TALL, box.cget("height")
        App._toggle_log_height(st)
        assert st._log_tall is False and st.log_toggle_btn.kw.get("text") == "展开", st.log_toggle_btn.kw
        assert int(box.cget("height")) == LOG_H_SHORT, box.cget("height")

        # ---- 按钮/接口在位
        for name in ("_open_log", "_export_log", "_clear_log", "_resolve_log_path"):
            assert callable(getattr(App, name)), name

        # ---- 输出目录不可用时兜底到配置目录（不抛异常）
        bad = tmp / "占位文件"
        bad.write_text("x", encoding="utf-8")
        got = App._resolve_log_path(st, bad)          # 传一个"文件"当目录
        assert got is None or got.name == LOG_NAME, got

        # ---- 导出日志：优先复制含完整堆栈的日志文件（逐字节一致）
        import kouboppt.gui as G
        old_dlg, old_mb = G.filedialog, G.messagebox
        try:
            dst = tmp / "导出_完整.log"
            dlg, mb = FakeDialog(str(dst)), FakeMessagebox()
            G.filedialog, G.messagebox = dlg, mb
            App._export_log(st)
            assert dlg.calls == 1, dlg.calls
            assert dlg.kw.get("initialdir") == str(logf.parent), dlg.kw
            assert dst.read_bytes() == logf.read_bytes(), "导出应逐字节复制日志文件"
            assert b"Traceback" in dst.read_bytes(), "导出的文件应含完整堆栈"
            assert "含完整堆栈" in box.get("1.0", "end"), "该提示来源应是文件"

            # ---- 没有日志文件时退回界面文本
            dst2 = tmp / "导出_界面.log"
            dlg = FakeDialog(str(dst2))
            G.filedialog = dlg
            st._log_path = None
            App._export_log(st)
            assert dlg.calls == 1, dlg.calls
            assert "章节结构读不出来" in dst2.read_text(encoding="utf-8"), "应退回界面文本"
            assert "（界面内容）" in box.get("1.0", "end"), "该提示来源应是界面"

            # ---- 用户取消保存：不写文件、不报错
            dlg3 = FakeDialog("")
            G.filedialog = dlg3
            App._export_log(st)
            assert dlg3.calls == 1 and not dlg3.ret

            # ---- 两者皆空：不弹保存框，只给提示
            box.configure(state="normal")
            box.delete("1.0", "end")
            box.configure(state="disabled")
            dlg4 = FakeDialog(None)
            G.filedialog = dlg4
            n_info = len(mb.info)
            App._export_log(st)
            assert dlg4.calls == 0, "没内容时不该弹保存框"
            assert len(mb.info) == n_info + 1, "应给出「没有内容」提示"
        finally:
            G.filedialog, G.messagebox = old_dlg, old_mb
            st._log_path = logf

        print("GUI 日志 OK（落盘/时间戳/堆栈/分段/高度切换/警告汇总/导出取源）")
    finally:
        root.destroy()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
