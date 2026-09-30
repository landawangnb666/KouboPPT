"""分步确认模式的 GUI 端到端测试（桩 LLM，需要桌面环境，会弹窗后自动点按钮）。

阶段零：分步确认 + 风格选「自动」→ AI 候选弹窗里挑第二套 → 风格生效并落盘 PPT风格.json。
阶段零之二：全自动 + 风格选「自动」→ 不弹窗，AI 直接定首推（日志写"自动选定"）。
阶段一：分步模式下点「继续」→ 全流程跑完（PPT + 笔记）。
阶段二：分步模式下点「就到这里」→ 停在第一步，不出笔记。
阶段三：弹窗开着时点主窗口「取消」→ 立即停止。
运行：python test_gui_step.py
"""
import shutil
import sys
import time
import traceback
import types
from pathlib import Path

import customtkinter as ctk

import kouboppt.gui as gui
from test_courseware import StubLLM, make_pdf

# 控制台是 GBK 时打印 ✔/⚠ 会 UnicodeEncodeError，导致测试整个失败
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

# 1) 干掉真实网络客户端与模态弹窗，配置写到临时文件
gui.LLMClient = lambda cfg, log=None: StubLLM()
gui.messagebox = types.SimpleNamespace(showinfo=lambda *a, **k: None,
                                       showwarning=lambda *a, **k: None)
_TMP_CFG = Path("test_out/_tmp_gui_config.json")
_TMP_CFG.parent.mkdir(parents=True, exist_ok=True)
gui.CONFIG_DIR = _TMP_CFG.parent
gui.CONFIG_FILE = _TMP_CFG
gui.App.AUTO_SELCHECK = False       # 首启编码自检由 test_encoder.py 专门覆盖


def find_buttons(widget):
    out = []
    for w in widget.winfo_children():
        if isinstance(w, ctk.CTkButton):
            out.append(w)
        out.extend(find_buttons(w))
    return out


def top_levels(app):
    return [w for w in app.winfo_children() if isinstance(w, ctk.CTkToplevel)]


def press(btn):
    btn._command()


def prep(out_dir: Path) -> Path:
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf = out_dir / "测试教材.pdf"
    make_pdf(pdf)
    return pdf


app = gui.App()
app.report_callback_exception = lambda *a: traceback.print_exception(*a)

state = {"phase": 0, "t0": time.time()}


def set_form(pdf: Path, out: Path):
    # 教材队列：直接灌进 app.pdfs 再刷新列表（等价于浏览/拖拽导入）
    app.pdfs[:] = [str(pdf)]
    app._refresh_pdf_list()
    for entry, val in ((app.cw_out, str(out)),
                       (app.ai_url, "https://example.invalid/v1"),
                       (app.ai_model, "stub-model"),
                       (app.ai_key, "sk-stub")):
        entry.delete(0, "end")
        entry.insert(0, val)
    app.cw_split_var.set("pages")
    for entry, val in ((app.cw_start, "1"), (app.cw_per, "1")):
        entry.delete(0, "end")
        entry.insert(0, val)
    app.cw_minutes.delete(0, "end")
    app.cw_minutes.insert(0, "35")
    app.cw_mode_var.set("step")
    for ck, on in ((app.ck_ppt, True), (app.ck_video, False),
                   (app.ck_notes, True), (app.ck_quiz, False), (app.ck_pdf, False)):
        (ck.select if on else ck.deselect)()


def check_workers_entry():
    """请求并发不设上限：挡位只是常用值，手输更大的数字也认；0/非数字有兜底。"""
    for val, want in (("24", 24), ("8", 8), ("1", 1), ("0", 1), ("abc", 4), (" 16 ", 16)):
        app.cw_workers.set(val)
        got = app._collect_courseware(app.pdfs[0]).workers
        assert got == want, f"并发输入 {val!r} → {got}，应为 {want}"
    app.cw_workers.set("4")
    print(" ✔ 并发数不设上限（24 等更大值可手输；0→1、非数字→4）", flush=True)


def check_fps_combo():
    """帧率下拉：默认 10fps（静态幻灯片实测比 30fps 快约 5.9 倍），可切 15/30，越界报错。"""
    assert app.fps_combo.get() == "10", app.fps_combo.get()
    assert app._collect_settings().fps == 10, app._collect_settings().fps
    for val, want in (("15", 15), ("30", 30), (" 10 ", 10)):
        app.fps_combo.set(val)
        got = app._collect_settings().fps
        assert got == want, f"帧率 {val!r} → {got}，应为 {want}"
    app.fps_combo.set("3")
    try:
        app._collect_settings()
        raise AssertionError("帧率 3 超出 5~60 应报错")
    except ValueError:
        pass
    app.fps_combo.set("10")
    print(" ✔ 帧率可调（默认 10fps，可切 15/30；越界报错）", flush=True)


def check_quiz_count():
    """题量：下拉挡位 + 「自定义…」才放出输入框；非法输入报错，存配置有兜底。"""
    assert app.cw_quizn.get() == "15", app.cw_quizn.get()
    for val in ("5", "30"):
        app.cw_quizn.set(val)
        app._on_quizn_change()
        assert app.cw_quizn_custom.winfo_manager() != "pack", "选挡位时不该有自定义输入框"
        assert app._collect_courseware(app.pdfs[0]).quiz_count == int(val)

    app.cw_quizn.set(gui.QUIZ_CUSTOM)
    app._on_quizn_change()
    assert app.cw_quizn_custom.winfo_manager() == "pack", "选「自定义…」应放出输入框"
    app.cw_quizn_custom.delete(0, "end")
    app.cw_quizn_custom.insert(0, "42")
    assert app._collect_courseware(app.pdfs[0]).quiz_count == 42
    assert app._collect_config()["cw_quiz_count"] == 42

    app.cw_quizn_custom.delete(0, "end")
    app.cw_quizn_custom.insert(0, "abc")
    try:
        app._collect_courseware(app.pdfs[0])
        raise AssertionError("题量填 abc 应报错")
    except ValueError as exc:
        assert "题量" in str(exc), exc
    assert app._collect_config()["cw_quiz_count"] == 15, "存配置遇到非法题量要兜底"

    app.cw_quizn.set("15")
    app._on_quizn_change()
    app.cw_quizn_custom.delete(0, "end")
    assert app.cw_quizn_custom.winfo_manager() != "pack", "切回挡位应收回输入框"
    assert app._collect_courseware(app.pdfs[0]).quiz_count == 15
    print(" ✔ 题量下拉（5/10/15/20/30 挡位；选「自定义…」出现输入框，填 42 生效；"
          "非法输入报错、存配置兜底 15）", flush=True)


def start_phase_style():
    """阶段零：分步确认 + 风格选「自动」，等 AI 候选弹窗出现后挑第二套。"""
    out = Path("test_out/gui_e2e_style")
    pdf = prep(out)
    set_form(pdf, out)
    app.ck_notes.deselect()                     # 只留 PPT：不起分步闸门，专验风格弹窗
    app.cw_theme.set(gui.AUTO_STYLE_LABEL)
    state["phase"] = "style"
    state["t0"] = time.time()
    app._start_courseware()


def start_phase_auto():
    """阶段零之二：全自动 + 风格选「自动」，不该弹窗，AI 直接定首推。"""
    out = Path("test_out/gui_e2e_auto")
    pdf = prep(out)
    set_form(pdf, out)
    app.ck_notes.deselect()
    app.cw_mode_var.set("auto")
    app.cw_theme.set(gui.AUTO_STYLE_LABEL)
    state["phase"] = "auto"
    state["t0"] = time.time()
    app._start_courseware()


def start_phase_1():
    out = Path("test_out/gui_e2e_a")
    pdf = prep(out)
    set_form(pdf, out)
    check_workers_entry()
    check_fps_combo()
    check_quiz_count()
    state["phase"] = 1
    state["t0"] = time.time()
    app._start_courseware()


def start_phase_2():
    out = Path("test_out/gui_e2e_b")
    pdf = prep(out)
    set_form(pdf, out)
    state["phase"] = 2
    state["t0"] = time.time()
    app._start_courseware()


def start_phase_3():
    out = Path("test_out/gui_e2e_c")
    pdf = prep(out)
    set_form(pdf, out)
    state["phase"] = 3
    state["cancelled"] = False
    state["t0"] = time.time()
    app._start_courseware()


def finish(ok: bool, msg: str):
    print(msg, flush=True)
    try:
        app.destroy()
    except Exception:
        pass
    sys.stdout.flush()
    sys.stderr.flush()
    sys.exit(0 if ok else 1)


def tick():
    try:
        body()
    except SystemExit:
        raise
    except BaseException:
        traceback.print_exc()
        finish(False, "GUI E2E FAILED")
        return
    app.after(300, tick)


def body():
    if state["phase"] == 0:
        return

    for d in top_levels(app):
        try:
            if not d.winfo_viewable():          # 已答完、正在隐藏的弹窗不再去点它
                continue
        except Exception:
            continue
        # 窗口刚创建、控件还没挂上时可能被 Tk 事件循环观察到，跳过等下一次
        btns = find_buttons(d)
        if not btns:
            continue
        labels = [b.cget("text") for b in btns]
        print(f"  [第{state['phase']}阶段] 弹窗按钮：{labels}", flush=True)
        if state["phase"] == "style":
            press(btns[1])                      # 挑第二个候选（★ 之外的那套）
            continue
        if state["phase"] == "auto":
            if "选这个" in labels:
                raise AssertionError(f"全自动模式不该弹风格窗，却出现了：{labels}")
            continue
        if state["phase"] == 1:
            press(btns[0])                      # 继续
        elif state["phase"] == 2:
            press(btns[1])                      # 就到这里
        elif not state["cancelled"]:
            state["cancelled"] = True
            app._cancel()                       # 弹窗开着时点「取消」

    if app.worker and not app.worker.is_alive():
        time.sleep(0.5)
        app.update()

        if state["phase"] == "style":
            out = Path("test_out/gui_e2e_style")
            assert list(out.rglob("*PPT.pptx")), "自动风格下也该出课件"
            files = list(out.rglob(gui.slidegen.STYLE_FILE))
            assert files, "风格决定该落盘到书目录"
            key = gui.slidegen.load_style_choice(files[0].parent)["key"]
            assert key == "medical_teal", f"候选里选的第二套该生效，实际：{key}"
            print(" ✔ e2e：分步模式弹候选，选第二套 → 风格生效并落盘", flush=True)
            start_phase_auto()
        elif state["phase"] == "auto":
            out = Path("test_out/gui_e2e_auto")
            assert list(out.rglob("*PPT.pptx")), "全自动下也该出课件"
            files = list(out.rglob(gui.slidegen.STYLE_FILE))
            assert files, "全自动也该把风格决定落盘"
            key = gui.slidegen.load_style_choice(files[0].parent)["key"]
            assert key == "science_green", f"全自动该直接取 AI 首推，实际：{key}"
            assert "自动选定" in app.log_box.get("1.0", "end"), "日志该写明是 AI 自动选的"
            print(" ✔ e2e：全自动不弹窗，AI 直接定首推并写进日志", flush=True)
            start_phase_1()
        elif state["phase"] == 1:
            out = Path("test_out/gui_e2e_a")
            assert list(out.rglob("*PPT.pptx")), "缺课件"
            assert list(out.rglob("*学习笔记.docx")), "点「继续」后该有笔记"
            print(" ✔ e2e：点「继续」→ PPT + 笔记都出来了", flush=True)
            start_phase_2()
        elif state["phase"] == 2:
            out = Path("test_out/gui_e2e_b")
            assert list(out.rglob("*PPT.pptx")), "缺课件"
            assert not list(out.rglob("*学习笔记.docx")), "点「就到这里」不该出笔记"
            print(" ✔ e2e：点「就到这里」→ 停在第一步，无笔记", flush=True)
            assert app.cw_start_btn.cget("state") == "normal", "结束后按钮该恢复可用"
            assert app.cancel_btn.cget("state") == "disabled"
            start_phase_3()
        else:
            out = Path("test_out/gui_e2e_c")
            assert state["cancelled"], "没走到取消分支"
            log_text = app.log_box.get("1.0", "end")
            assert "已取消" in log_text, log_text[-400:]
            assert not list(out.rglob("*学习笔记.docx")), "取消后不该继续出笔记"
            print(" ✔ e2e：弹窗开着点「取消」→ 立即停止", flush=True)
            finish(True, "GUI E2E OK")

    if time.time() - state["t0"] > 120:
        finish(False, "TIMEOUT")


app.after(400, start_phase_style)
app.after(500, tick)
app.mainloop()
