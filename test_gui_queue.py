"""多教材队列的 GUI 测试：顺序执行、失败不中断、调序、去重、配置往返。

不起完整流水线（那要真跑 OCR+TTS 太慢），而是把 courseware.run 换成桩：
桩按调用顺序记录处理了哪本书，并可按书名"故意失败"，用来验证单本失败不影响后续。
需要桌面环境（会创建一个随即隐藏的 Tk 窗口，且会真实加载 tkdnd）。

运行：python test_gui_queue.py
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import kouboppt.gui as gui
from kouboppt import courseware

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

_TMP_CFG = Path("test_out/_tmp_queue_config.json")
_TMP_CFG.parent.mkdir(parents=True, exist_ok=True)
gui.CONFIG_DIR = _TMP_CFG.parent
gui.CONFIG_FILE = _TMP_CFG
gui.App.AUTO_SELCHECK = False
gui.messagebox = types.SimpleNamespace(showinfo=lambda *a, **k: None,
                                       showwarning=lambda *a, **k: None,
                                       showerror=lambda *a, **k: None)
gui.LLMClient = lambda cfg, log=None: types.SimpleNamespace()

TRACE: list[str] = []          # 桩记录：实际处理的顺序（按书名 stem）
FAIL_ON: set[str] = set()      # 这些书名"跑失败"


def _stub_run(llm, opts, log=None, progress=None, cancel=None, gate=None):
    name = Path(opts.pdf_path).stem
    TRACE.append(name)
    if log:
        log(f"[桩] 处理 {name} → {opts.out_dir}")
    if name in FAIL_ON:
        raise RuntimeError(f"故意让《{name}》失败")
    if progress:
        progress(0.5, f"桩进度 {name}")
        progress(1.0, f"桩完成 {name}")
    return [Path(opts.out_dir) / f"{name}_产出.pptx"]


courseware.run = _stub_run
gui.courseware.run = _stub_run


app = gui.App()

PDFS = [Path("test_out/_q/教材一.pdf"), Path("test_out/_q/教材二.pdf"),
        Path("test_out/_q/教材三.pdf")]
for p in PDFS:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"%PDF-1.4\n%stub\n")


def fill_common():
    app.cw_out.delete(0, "end")
    app.cw_out.insert(0, "test_out/_q_out")
    app.ai_url.delete(0, "end")
    app.ai_url.insert(0, "https://example.invalid/v1")
    app.ai_model.delete(0, "end")
    app.ai_model.insert(0, "stub")
    app.ai_key.delete(0, "end")
    app.ai_key.insert(0, "sk-x")
    app.cw_mode_var.set("auto")          # 多本强制全自动，这里直接选 auto 更贴近实际
    for ck, on in ((app.ck_ppt, True), (app.ck_video, False),
                   (app.ck_notes, False), (app.ck_quiz, False), (app.ck_pdf, False)):
        (ck.select if on else ck.deselect)()


def wait_done(timeout=30.0):
    """跑一轮直到 _handle_done 被调用。

    不能自己去 queue.get_nowait()——那会把消息从 _poll 手里抢走，done 永远轮不到
    _poll 处理。改成拦 _handle_done 拿结果，同时 update() 驱动 after 回调。
    """
    import time
    holder = {"payload": None}
    orig = app._handle_done

    def spy(result):
        holder["payload"] = result
        orig(result)

    app._handle_done = spy
    t0 = time.time()
    try:
        while time.time() - t0 < timeout:
            app.update()
            if holder["payload"] is not None:
                break
            time.sleep(0.02)
    finally:
        app._handle_done = orig
    return holder["payload"]


def check_add_remove_dedup():
    app.pdfs.clear()
    app._refresh_pdf_list()
    n = app._add_pdfs([str(PDFS[0]), str(PDFS[1]), str(PDFS[0])])   # 第三个是重复
    assert n == 2, f"去重后应加入 2 本，实际 {n}"
    assert app.pdfs == [str(PDFS[0]), str(PDFS[1])], app.pdfs
    app._add_pdfs([str(PDFS[2]), "not_a_pdf.txt"])                  # 混入非 PDF
    assert len(app.pdfs) == 3, app.pdfs
    assert app.pdf_list.size() == 3, app.pdf_list.size()
    assert app.pdf_list.get(0).startswith("1. "), app.pdf_list.get(0)
    print(" ✔ 追加导入：去重、忽略非 PDF、列表带序号", flush=True)


def check_reorder():
    app.pdfs[:] = [str(PDFS[0]), str(PDFS[1]), str(PDFS[2])]
    app._refresh_pdf_list()
    app.pdf_list.selection_clear(0, "end")
    app.pdf_list.selection_set(1)          # 选中第二本
    app._move_pdf(-1)                      # 上移
    assert app.pdfs == [str(PDFS[1]), str(PDFS[0]), str(PDFS[2])], app.pdfs
    assert app._selected_pdf_indices() == [0], app._selected_pdf_indices()
    app._move_pdf(1)                       # 再下移回去
    assert app.pdfs == [str(PDFS[0]), str(PDFS[1]), str(PDFS[2])], app.pdfs
    # 边界：第一本不能再上移
    app.pdf_list.selection_clear(0, "end")
    app.pdf_list.selection_set(0)
    app._move_pdf(-1)
    assert app.pdfs[0] == str(PDFS[0]), app.pdfs
    print(" ✔ 调序：上移/下移改执行顺序；到顶不越界", flush=True)


def check_remove_selected():
    app.pdfs[:] = [str(PDFS[0]), str(PDFS[1]), str(PDFS[2])]
    app._refresh_pdf_list()
    app.pdf_list.selection_clear(0, "end")
    app.pdf_list.selection_set(0)
    app.pdf_list.selection_set(2)          # 同时选中首尾
    app._remove_pdfs()
    assert app.pdfs == [str(PDFS[1])], app.pdfs
    app._clear_pdfs()
    assert app.pdfs == [] and app.pdf_list.size() == 0
    print(" ✔ 移除所选 / 清空列表", flush=True)


def check_sequential_run():
    TRACE.clear()
    FAIL_ON.clear()
    app.pdfs[:] = [str(PDFS[0]), str(PDFS[1]), str(PDFS[2])]
    app._refresh_pdf_list()
    fill_common()
    app._start_courseware()
    done = wait_done()
    assert done is not None, "队列没有在超时内跑完"
    ok, results, cancelled = done
    assert TRACE == ["教材一", "教材二", "教材三"], f"执行顺序不对：{TRACE}"
    assert not cancelled, cancelled
    assert len(results) == 3 and all(not r for _, _, r in results), results
    assert ok == 3, ok
    print(f" ✔ 顺序执行：按列表顺序逐本跑完（{TRACE}），产出 {ok} 个", flush=True)


def check_failure_isolation():
    TRACE.clear()
    FAIL_ON.clear()
    FAIL_ON.add("教材二")                  # 让中间那本失败
    app.pdfs[:] = [str(PDFS[0]), str(PDFS[1]), str(PDFS[2])]
    app._refresh_pdf_list()
    fill_common()
    app._start_courseware()
    done = wait_done()
    assert done is not None, "队列没有在超时内跑完"
    ok, results, cancelled = done
    assert TRACE == ["教材一", "教材二", "教材三"], f"失败后没有继续：{TRACE}"
    assert not cancelled, cancelled
    failed = [(n, r) for n, _, r in results if r]
    assert len(failed) == 1 and failed[0][0] == "教材二.pdf", results
    assert "故意让《教材二》失败" in failed[0][1], failed[0][1]
    assert ok == 2, ok
    print(f" ✔ 失败隔离：中间一本失败后继续跑完，成功 2 / 失败 1，原因已记录", flush=True)


def check_cancel_stops_queue():
    """取消要能立刻停在当前这本，后面的不再跑（真机上是运行中点「取消」）。

    _begin_run 会清一次取消位，所以必须"跑起来之后再取消"才符合真实路径。
    """
    TRACE.clear()
    FAIL_ON.clear()
    app.pdfs[:] = [str(PDFS[0]), str(PDFS[1]), str(PDFS[2])]
    app._refresh_pdf_list()
    fill_common()

    orig = _stub_run
    def cancel_after_first(llm, opts, log=None, progress=None, cancel=None, gate=None):
        out = orig(llm, opts, log=log, progress=progress, cancel=cancel, gate=gate)
        app.cancel_event.set()          # 第一本一跑完就按取消
        return out
    gui.courseware.run = cancel_after_first

    try:
        app._start_courseware()
        done = wait_done()
    finally:
        gui.courseware.run = orig
        app.cancel_event.clear()

    assert done is not None, "取消后没有收尾"
    ok, results, cancelled = done
    assert cancelled is True, done
    assert TRACE == ["教材一"], f"取消后不该继续下一本，实际跑了：{TRACE}"
    print(" ✔ 取消：停在当前这本，后面的不再跑", flush=True)


def check_config_roundtrip():
    app.pdfs[:] = [str(PDFS[0]), str(PDFS[2])]
    app._refresh_pdf_list()
    cfg = app._collect_config()
    assert cfg["cw_pdfs"] == [str(PDFS[0]), str(PDFS[2])], cfg["cw_pdfs"]
    back = gui._load_pdf_queue(cfg)
    assert back == [str(PDFS[0]), str(PDFS[2])], back
    # 老配置迁移
    assert gui._load_pdf_queue({"cw_pdf": "old.pdf"}) == ["old.pdf"]
    print(" ✔ 配置往返：写 cw_pdfs 列表、读回一致；旧 cw_pdf 自动迁移", flush=True)


def check_out_dir_rules():
    app.cw_out.delete(0, "end")            # 留空
    app.pdfs[:] = [str(PDFS[0])]
    assert app._courseware_out_for(str(PDFS[0])) == str(PDFS[0].parent), "单本应落 PDF 同目录"
    app.pdfs[:] = [str(PDFS[0]), str(PDFS[1])]
    got = app._courseware_out_for(str(PDFS[0]))
    assert got == str(PDFS[0].parent / "教材课程"), got
    app.cw_out.delete(0, "end")
    app.cw_out.insert(0, "test_out/_q_explicit")
    assert app._courseware_out_for(str(PDFS[0])) == "test_out/_q_explicit"
    print(" ✔ 输出目录：留空时单本 → PDF 同目录，多本 → 统一收进「教材课程」", flush=True)


def check_precheck_missing():
    app.pdfs[:] = ["test_out/_q/不存在.pdf"]
    fill_common()
    calls = []
    gui.messagebox.showwarning = lambda t, m, *a, **k: calls.append((t, m))
    app._start_courseware()
    assert calls and "不存在" in calls[0][1], calls
    assert TRACE == [], "预检失败不该真的开始跑"
    print(" ✔ 预检：路径不存在时直接拦住，不进入队列", flush=True)


def main():
    print("== 多教材队列测试 ==", flush=True)
    check_add_remove_dedup()
    check_reorder()
    check_remove_selected()
    check_out_dir_rules()
    check_config_roundtrip()
    check_precheck_missing()
    check_sequential_run()
    check_failure_isolation()
    check_cancel_stops_queue()
    print("GUI 队列 OK", flush=True)


if __name__ == "__main__":
    try:
        main()
    finally:
        try:
            app.destroy()
        except Exception:
            pass
