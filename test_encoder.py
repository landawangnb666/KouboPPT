"""视频编码自检与编码方式选择的界面测试（桩 self_check，需要桌面环境，不碰真配置）。

覆盖：首启自动弹自检页 → 进度条 → 实测对比 → 预选推荐项 → 确定后写进配置；
     编码下拉三种策略解析到正确编码器；重新自检时可跳过且不冲掉旧结论；
     没有硬编的机器只给软编；硬编更快的机器预选硬编；二次启动不再自检。
运行：python test_encoder.py
"""
import json
import os
import shutil
import subprocess
import sys
import time
import types
from pathlib import Path

import customtkinter as ctk

import kouboppt.gui as gui

# 控制台是 GBK 时打印 ✔/⚠ 会 UnicodeEncodeError，导致测试整个失败
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

SECOND = "--second" in sys.argv       # 子进程：模拟"第二次启动"
NULLCFG = "--null" in sys.argv        # 子进程：上次没测完（null）应重新自检
CFG_DIR = Path("test_out/_enc_ui")
if not (SECOND or NULLCFG) and CFG_DIR.exists():
    shutil.rmtree(CFG_DIR)
CFG_DIR.mkdir(parents=True, exist_ok=True)
gui.CONFIG_DIR = CFG_DIR
gui.CONFIG_FILE = CFG_DIR / "config.json"
gui.messagebox = types.SimpleNamespace(showinfo=lambda *a, **k: None,
                                       showwarning=lambda *a, **k: None)

# 本机实测过的三种局面：软编更快 / 没有硬编 / 硬编更快
FAKE = {"seconds": 30.0, "fps": 10,
        "timings": {"libx264": 1.12, "h264_qsv": 1.98},
        "errors": {"h264_nvenc": "Cannot load nvcuda.dll",
                   "h264_amf": "DLL amfrt64.dll failed to open"},
        "hw_available": ["h264_qsv"], "recommended": "libx264",
        "elapsed": 4.5, "cancelled": False}
NOHW = {"seconds": 30.0, "fps": 10, "timings": {"libx264": 1.4},
        "errors": {"h264_nvenc": "no nvidia", "h264_qsv": "no qsv", "h264_amf": "no amf"},
        "hw_available": [], "recommended": "libx264", "elapsed": 2.0, "cancelled": False}
NEXT: list[dict] = [dict(FAKE)]


def fake_check(seconds=30.0, fps=10, timeout=15.0, deadline=45.0, progress=None, cancel=None):
    for name in gui.video.ENCODER_LABELS.values():
        if cancel and cancel():
            return {"cancelled": True, "timings": {}, "errors": {}}
        if progress:
            progress(f"正在测试 {name} …")
        time.sleep(0.35)          # 慢一点，好让进度页被观察到
    return NEXT.pop(0) if NEXT else dict(FAKE)


gui.video.self_check = fake_check


def find(widget, cls):
    out = []
    for w in widget.winfo_children():
        if isinstance(w, cls):
            out.append(w)
        out.extend(find(w, cls))
    return out


def pump(app, until, limit=8.0):
    t0 = time.time()
    while time.time() - t0 < limit:
        app.update()
        if until():
            return True
        time.sleep(0.01)
    return False


def radios(app):
    return find(app._overlay, ctk.CTkRadioButton) if app._overlay else []


def labels(app):
    return [w.cget("text") for w in find(app._overlay, ctk.CTkLabel)] if app._overlay else []


def overlay_buttons(app):
    return find(app._overlay, ctk.CTkButton) if app._overlay else []


def press(btn):
    btn._command()


def saved_cfg() -> dict:
    return json.loads((CFG_DIR / "config.json").read_text(encoding="utf-8"))


def click(app, text):
    btn = next(b for b in find(app, ctk.CTkButton) if text in b.cget("text"))
    press(btn)


# ------------------------------------------------ 0) 二次启动（由父进程派生）
if SECOND:
    app = gui.App()
    for _ in range(40):
        app.update()
        time.sleep(0.02)
    assert app._overlay is None, "已自检过的机器又弹了一次自检"
    assert app.enc_combo.get() == gui.ENC_MODE_LABELS["hw"], app.enc_combo.get()
    assert app.enc_combo2.get() == gui.ENC_MODE_LABELS["hw"], app.enc_combo2.get()
    assert app._collect_settings().codec == "h264_nvenc", app._collect_settings().codec
    assert "推荐" in app.enc_status.cget("text"), app.enc_status.cget("text")
    print(" ✔ 二次启动：不再自检，恢复上次的编码选择", flush=True)
    app.destroy()
    sys.exit(0)

# ------------------------------------------- 0b) 上次没测完（null）→ 重新自检
if NULLCFG:
    cfg = json.loads((CFG_DIR / "config.json").read_text(encoding="utf-8"))
    cfg["enc_selfcheck"] = None          # 模拟"自检没做完就关了窗口"
    (CFG_DIR / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
    app = gui.App()
    assert pump(app, lambda: app._overlay is not None, 6), "上次没测完，这次应重新自检"
    app._skip_selfcheck()
    app.update()
    assert app._overlay is None, "跳过后浮层没关"
    print(" ✔ 上次没测完（配置里是 null）：这次会重新自检", flush=True)
    app.destroy()
    sys.exit(0)

# ------------------------------------------------ 1) 首启自动自检 → 结果页
app = gui.App()
assert pump(app, lambda: app._overlay is not None, 3), "首启没有弹出自检浮层"
assert pump(app, lambda: (app._overlay_bar is not None and app._overlay_bar.get() > 0)
                         or radios(app), 5), "进度条没走"
assert pump(app, lambda: radios(app), 5), "自检结束后没出现选择页"
assert len(radios(app)) == 2, f"应有软编/硬编两个选项，实际 {len(radios(app))}"
assert app._ov_mode.get() == "sw", f"应预选实测更快的软编，实际 {app._ov_mode.get()}"
text = "\n".join(labels(app))
assert "1.12" in text and "1.98" in text, f"结果页没写实测耗时：{text}"
assert "推荐" in text and "Quick Sync" in text, f"结果页文案缺推荐/硬编名：{text}"
assert any("确定" in b.cget("text") for b in overlay_buttons(app)), "缺确定按钮"
assert any("重新自检" in b.cget("text") for b in overlay_buttons(app)), "缺重新自检按钮"
assert any("跳过" in b.cget("text") for b in find(app, ctk.CTkButton)) is False, "结果页不该再有跳过"
print(" ✔ 首启自检页：进度条 + 实测对比 + 预选推荐项", flush=True)

# ------------------------------------------------ 2) 确定 → 记住选择
click(app, "确定")
app.update()
assert app._overlay is None, "点确定后浮层没关"
saved = saved_cfg()
assert saved["enc_mode"] == "sw", saved.get("enc_mode")
assert saved["enc_selfcheck"]["timings"]["h264_qsv"] == 1.98, saved["enc_selfcheck"]
assert app._collect_settings().codec == "libx264", app._collect_settings().codec
assert "本次将用：CPU 软件编码" in app.enc_status.cget("text"), app.enc_status.cget("text")
assert app.enc_combo2.get() == gui.ENC_MODE_LABELS["sw"], "确定后教材页下拉没同步"
print(" ✔ 确定：写进配置，本次用 CPU 软编，状态行同步", flush=True)

# ------------------------------------------------ 3) 下拉切硬编/自动
app.enc_combo.set(gui.ENC_MODE_LABELS["hw"])
app._on_enc_mode_change()
assert app._collect_settings().codec == "h264_qsv", app._collect_settings().codec
assert "Quick Sync" in app.enc_status.cget("text"), app.enc_status.cget("text")
app.enc_combo.set(gui.ENC_MODE_LABELS["auto"])
app._on_enc_mode_change()
assert app._collect_settings().codec == "libx264", "自动应挑实测最快的"
app.enc_combo.set(gui.ENC_MODE_LABELS["sw"])
app._on_enc_mode_change()
assert app._enc_mode() == "sw" and app._collect_settings().codec == "libx264"

# 教材页的下拉是同一设置的第二个入口：改哪边，另一边和两行状态都要跟着变
app.enc_combo2.set(gui.ENC_MODE_LABELS["hw"])
app._on_enc_mode_change(gui.ENC_MODE_LABELS["hw"])
assert app.enc_combo.get() == gui.ENC_MODE_LABELS["hw"], "教材页改编码，视频页没跟着变"
assert app._collect_settings().codec == "h264_qsv", app._collect_settings().codec
assert app.enc_status2.cget("text") == app.enc_status.cget("text"), "两行状态不一致"
app.enc_combo.set(gui.ENC_MODE_LABELS["sw"])
app._on_enc_mode_change(gui.ENC_MODE_LABELS["sw"])
assert app.enc_combo2.get() == gui.ENC_MODE_LABELS["sw"], "视频页改编码，教材页没跟着变"
print(" ✔ 编码下拉：自动/硬编/软编解析正确，两个页签互相同步", flush=True)

# ------------------------------------------------ 4) 重新自检 → 跳过
click(app, "重新自检")
assert pump(app, lambda: app._overlay is not None, 3), "重新自检没弹浮层"
assert pump(app, lambda: any("跳过" in b.cget("text") for b in overlay_buttons(app)), 3), \
    "自检进行中该有跳过按钮"
app._skip_selfcheck()
app.update()
assert app._overlay is None, "跳过后浮层没关"
assert pump(app, lambda: not app._enc_busy, 6), "跳过后自检线程没收工"
assert saved_cfg()["enc_selfcheck"]["timings"].get("h264_qsv") == 1.98, "跳过把已有结论冲掉了"
print(" ✔ 跳过自检：不弹结果页，已有结论不被冲掉", flush=True)

# ------------------------------------------------ 5) 没有硬编的机器
NEXT.append(dict(NOHW))
click(app, "重新自检")
assert pump(app, lambda: radios(app), 6), "无硬编场景没出结果页"
assert len(radios(app)) == 1, f"无硬编时只该有软编一个可选，实际 {len(radios(app))}"
assert any("本机不可用" in t for t in labels(app)), "没说明硬编不可用"
click(app, "确定")
app.update()
assert app._collect_settings().codec == "libx264"
assert saved_cfg()["enc_selfcheck"]["hw_available"] == []
print(" ✔ 无硬编机器：只给软编，并说明原因", flush=True)

# ------------------------------------------------ 6) 硬编更快的机器
NEXT.append({**FAKE, "timings": {"libx264": 3.4, "h264_nvenc": 1.1},
             "hw_available": ["h264_nvenc"], "recommended": "h264_nvenc", "errors": {}})
click(app, "重新自检")
assert pump(app, lambda: radios(app), 6), "硬编更快场景没出结果页"
assert app._ov_mode.get() == "hw", f"硬编更快时应预选硬编，实际 {app._ov_mode.get()}"
click(app, "确定")
app.update()
assert app._collect_settings().codec == "h264_nvenc", app._collect_settings().codec
assert saved_cfg()["enc_mode"] == "hw"
print(" ✔ 硬编更快的机器：预选硬编，确定后真的用 NVENC", flush=True)

app.destroy()

# ------------------------------------------------ 7) 二次启动：另开一个进程读同一份配置
child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--second"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env={**os.environ, "PYTHONIOENCODING": "utf-8"})
print((child.stdout or "").strip(), flush=True)
assert child.returncode == 0, f"二次启动失败：\n{child.stdout}\n{child.stderr}"

child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--null"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env={**os.environ, "PYTHONIOENCODING": "utf-8"})
print((child.stdout or "").strip(), flush=True)
assert child.returncode == 0, f"null 配置失败：\n{child.stdout}\n{child.stderr}"

shutil.rmtree(CFG_DIR, ignore_errors=True)
print("ENCODER UI OK")
