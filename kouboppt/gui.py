"""KouboPPT 桌面界面（customtkinter）。

两个标签页：
- PPT→视频：原有功能
- 教材→课程：扫描 PDF → OCR → AI 生成课件/口播稿 → 视频 → 笔记/题库
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import sys
import threading
import traceback
import webbrowser
from datetime import datetime
from pathlib import Path

import customtkinter as ctk
from PIL import Image
from tkinter import filedialog, messagebox, Listbox, END, EXTENDED

from . import __version__, courseware, pipeline, slidegen, video
from . import theme as T
from .llm import LLMClient, LLMConfig
from .tts import get_provider, provider_names

CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "KouboPPT"
CONFIG_FILE = CONFIG_DIR / "config.json"

LOG_NAME = "运行日志.log"          # 落在输出目录，带时间戳与运行分段
LOG_SEP = "─" * 8                  # 界面上的运行分隔线（也是日志中的阶段样式）
LOG_H_SHORT, LOG_H_TALL = 120, 420  # 日志区折叠 / 展开高度
NAV_BTN_W = 152                    # 顶部两个标签按钮的等宽

RATE_RE = re.compile(r"^[+-]\d+%$")

# PPT 风格下拉：第一项"自动"（AI 按书名选，见 slidegen.resolve_style），其余来自风格库。
# key 直接写进配置，所以风格库的 key 只能新增、不能改名。
AUTO_STYLE_LABEL = "自动（AI 按书名选风格）"
THEME_LABELS = {slidegen.AUTO_STYLE: AUTO_STYLE_LABEL, **slidegen.style_labels()}

# 视频编码方式：界面上给的是"策略"，具体用哪个编码器由自检结果解析
ENC_MODE_LABELS = {
    "auto": "自动（用实测更快的）",
    "hw": "显卡硬件编码",
    "sw": "CPU 软件编码",
}

# 题库题量：常用挡位做成下拉，超出挡位选「自定义…」自己填（按每节课算）
QUIZ_PRESETS = ("5", "10", "15", "20", "30")
QUIZ_CUSTOM = "自定义…"


def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    except Exception:
        pass


def _asset(name: str) -> Path | None:
    """找打包进来的资源（图标等）：打包后在解包目录/exe 旁边，开发时在 build_assets。"""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        cands = [Path(meipass) / name if meipass else None,
                 Path(sys.executable).parent / name]
    else:
        cands = [Path(__file__).parent.parent / "build_assets" / name]
    return next((c for c in cands if c and c.exists()), None)


class App(ctk.CTk):
    AUTO_SELCHECK = True      # 自动化测试置 False：别让每次跑测试都真编一遍

    def __init__(self):
        super().__init__()
        # 固定浅色主题：商务蓝白在深浅色系统下表现一致，截图/讲解都稳定
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")

        self.title(f"口播PPT KouboPPT v{__version__}")
        self.geometry("1120x960")
        self.minsize(980, 780)
        self.configure(fg_color=T.BG)
        icon = _asset("icon.ico")
        if icon:
            try:
                self.iconbitmap(str(icon))
            except Exception:      # noqa: BLE001  个别环境不支持 ico，不影响使用
                pass

        self.cfg = load_config()
        self.files: list[str] = list(self.cfg.get("files", []))
        self.worker: threading.Thread | None = None
        self.cancel_event = threading.Event()
        self.msg_queue: queue.Queue = queue.Queue()
        self._overlay = None
        self._overlay_step = None
        self._overlay_bar = None
        self._overlay_done = 0
        self._ov_mode = ctk.StringVar(value="sw")
        self._enc_busy = False
        self._enc_cancel = threading.Event()
        self._log_path: Path | None = None      # 本次/上次运行的日志文件（供「打开日志」）
        self._log_active = False                # 运行中才往文件写，避免零散消息串到旧日志
        self._log_lock = threading.Lock()       # 工作线程也会写文件，必须加锁
        self._warn_count = 0
        self._log_tall = False
        video.set_selfcheck(self.cfg.get("enc_selfcheck"))

        self._build_header()
        self._build_ui()
        self._restore_cfg()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._poll)
        self._maybe_selfcheck()

    # ------------------------------------------------------------------ UI
    def _build_header(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(16, 6))

        png = _asset("icon_ui.png")
        if png:
            try:
                icon = ctk.CTkImage(Image.open(png), size=(40, 40))
                ctk.CTkLabel(header, image=icon, text="").pack(side="left", padx=(0, 12))
            except Exception:      # noqa: BLE001  图标损坏不影响启动
                pass

        titles = ctk.CTkFrame(header, fg_color="transparent")
        titles.pack(side="left")
        row = ctk.CTkFrame(titles, fg_color="transparent")
        row.pack(anchor="w")
        ctk.CTkLabel(row, text="口播PPT KouboPPT", font=T.FONT_H1,
                     text_color=T.TEXT).pack(side="left")
        ctk.CTkLabel(row, text=f"v{__version__}", font=(T.FONT, 11, "bold"),
                     text_color=T.PRIMARY, fg_color=T.PRIMARY_SOFT, corner_radius=6,
                     width=52, height=22).pack(side="left", padx=(10, 0))
        ctk.CTkLabel(titles, text="扫描教材 / 现成 PPT  →  课件 · 口播视频 · 学习笔记 · 题库",
                     font=T.FONT_SMALL, text_color=T.MUTED).pack(anchor="w", pady=(2, 0))

        T.button(header, "LDWAPI  ↗", kind="link", width=104, height=30,
                 command=self._open_api_site).pack(side="right")

    def _open_api_site(self):
        webbrowser.open("https://api.ldwnb666.xyz/")

    def _build_ui(self):
        # 分段导航：白色圆角容器里两个等宽按钮。选中项＝品牌蓝实心＋白字，
        # 未选中＝透明底＋灰字。（CTkTabview 的 text_color 是统一的，做不出这两种文字色）
        nav_wrap = ctk.CTkFrame(self, fg_color="transparent")
        nav_wrap.pack(fill="x", padx=16, pady=(8, 0))
        nav = ctk.CTkFrame(nav_wrap, fg_color=T.CARD, corner_radius=T.RADIUS,
                           border_width=1, border_color=T.CARD_BORDER)
        nav.pack(side="left")
        self.nav_btns: dict[str, ctk.CTkButton] = {}
        for key, label in (("video", "PPT → 视频"), ("book", "教材 → 课程")):
            btn = ctk.CTkButton(nav, text=label, width=NAV_BTN_W, height=34,
                                corner_radius=8, font=T.FONT_BODY_BOLD,
                                command=lambda k=key: self._switch_tab(k))
            btn.pack(side="left", padx=4, pady=4)
            self.nav_btns[key] = btn

        self.tab_body = ctk.CTkFrame(self, fg_color="transparent")
        self.tab_body.pack(fill="both", expand=True, padx=16, pady=(6, 0))
        self.tab_video = ctk.CTkFrame(self.tab_body, fg_color="transparent")
        self.tab_book = ctk.CTkFrame(self.tab_body, fg_color="transparent")
        self._build_video_tab(self.tab_video)
        self._build_book_tab(self.tab_book)
        self._build_bottom()
        self._switch_tab("video")

    def _switch_tab(self, key: str):
        """切换标签页：只显示选中的那个，并同步顶部按钮的选中外观。"""
        for k, frame in (("video", self.tab_video), ("book", self.tab_book)):
            if k == key:
                frame.pack(fill="both", expand=True)
            else:
                frame.pack_forget()
        for k, btn in self.nav_btns.items():
            on = k == key
            btn.configure(fg_color=T.PRIMARY if on else "transparent",
                          hover_color=T.PRIMARY_HOVER if on else T.SECONDARY_HOVER,
                          text_color="#FFFFFF" if on else T.MUTED)

    def _scroll_area(self, root) -> ctk.CTkScrollableFrame:
        area = ctk.CTkScrollableFrame(root, fg_color="transparent", corner_radius=0,
                                      scrollbar_button_color="#C9D4E4",
                                      scrollbar_button_hover_color=T.FAINT)
        area.pack(fill="both", expand=True)
        return area

    # ---------------- 标签页 1：原有 PPT→视频 ----------------
    def _build_video_tab(self, root):
        run_frame = ctk.CTkFrame(root, fg_color="transparent")
        run_frame.pack(side="bottom", fill="x", pady=(10, 2))
        self.start_btn = T.cta(run_frame, "开始生成视频", command=self._start)
        self.start_btn.pack(side="left")

        root = self._scroll_area(root)
        card1 = T.Card(root, "选择 PPT 文件", step=1)
        card1.pack(fill="x", pady=(10, 8))
        self.file_count_label = ctk.CTkLabel(card1.head, text="", font=T.FONT_SMALL,
                                             text_color=T.FAINT)
        self.file_count_label.pack(side="right")

        row = ctk.CTkFrame(card1.body, fg_color="transparent")
        row.pack(fill="x")
        list_wrap = ctk.CTkFrame(row, fg_color=T.SOFT, corner_radius=8,
                                 border_width=1, border_color=T.FIELD_BORDER)
        list_wrap.pack(side="left", fill="both", expand=True)
        self.file_list = Listbox(list_wrap, selectmode=EXTENDED, height=4,
                                 font=T.FONT_BODY, activestyle="none",
                                 bg=T.SOFT, fg=T.TEXT, bd=0, highlightthickness=0,
                                 selectbackground=T.PRIMARY_SOFT,
                                 selectforeground=T.PRIMARY)
        list_scroll = ctk.CTkScrollbar(list_wrap, command=self.file_list.yview,
                                       button_color="#C9D4E4",
                                       button_hover_color=T.FAINT)
        self.file_list.configure(yscrollcommand=list_scroll.set)
        self.file_list.pack(side="left", fill="both", expand=True, padx=8, pady=8)
        list_scroll.pack(side="right", fill="y", pady=8, padx=(0, 6))

        btns = ctk.CTkFrame(row, fg_color="transparent")
        btns.pack(side="left", fill="y", padx=(10, 0))
        T.button(btns, "添加文件", kind="primary", width=112,
                 command=self._add_files).pack(pady=(0, 6))
        T.button(btns, "移除所选", width=112, command=self._remove_files).pack(pady=(0, 6))
        T.button(btns, "清空列表", width=112, command=self._clear_files).pack()

        card2 = T.Card(root, "输出设置", step=2)
        card2.pack(fill="x")
        grid = card2.body
        grid.columnconfigure(1, weight=1)

        ctk.CTkLabel(grid, text="输出目录：", font=T.FONT_BODY).grid(row=0, column=0, sticky="w", pady=4)
        self.out_entry = T.entry(grid, placeholder_text="视频保存到哪里（留空 = 和 PPT 同目录）")
        self.out_entry.grid(row=0, column=1, sticky="ew", pady=4)
        T.button(grid, "浏览", width=72,
                 command=self._pick_out).grid(row=0, column=2, padx=(10, 0), pady=4)

        ctk.CTkLabel(grid, text="页码范围：", font=T.FONT_BODY).grid(row=1, column=0, sticky="w", pady=4)
        self.range_entry = T.entry(grid, placeholder_text="如 1-18,19-36 → 拆成 2 个视频；留空 = 整个 PPT 一个视频")
        self.range_entry.grid(row=1, column=1, columnspan=2, sticky="ew", pady=4)

        ctk.CTkLabel(grid, text="文稿来源：", font=T.FONT_BODY).grid(row=2, column=0, sticky="w", pady=4)
        src_row = ctk.CTkFrame(grid, fg_color="transparent")
        src_row.grid(row=2, column=1, columnspan=2, sticky="w", pady=4)
        self.src_var = ctk.StringVar(value="正文")
        for s in pipeline.ppt_text.TEXT_SOURCES:
            T.radio(src_row, s, variable=self.src_var, value=s).pack(side="left", padx=(0, 22))

        ctk.CTkLabel(grid, text="TTS 音色：", font=T.FONT_BODY).grid(row=3, column=0, sticky="w", pady=4)
        tts_row = ctk.CTkFrame(grid, fg_color="transparent")
        tts_row.grid(row=3, column=1, columnspan=2, sticky="ew", pady=4)
        tts_row.columnconfigure(1, weight=1)
        self.provider_combo = T.combo(tts_row, values=provider_names(), width=200,
                                      command=self._on_provider_change)
        self.provider_combo.grid(row=0, column=0, padx=(0, 8))
        self.voice_combo = T.combo(tts_row, values=[])
        self.voice_combo.grid(row=0, column=1, sticky="ew")
        ctk.CTkLabel(tts_row, text="语速", font=T.FONT_BODY).grid(row=0, column=2, padx=(12, 4))
        self.rate_entry = T.entry(tts_row, width=80, placeholder_text="+0%")
        self.rate_entry.grid(row=0, column=3)
        ctk.CTkLabel(tts_row, text="如 -10% 慢、+15% 快", font=T.FONT_SMALL,
                     text_color=T.FAINT).grid(row=0, column=4, padx=(8, 0))

        adv_row = ctk.CTkFrame(card2.body, fg_color="transparent")
        adv_row.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        ctk.CTkLabel(adv_row, text="无文字页停留", font=T.FONT_BODY).pack(side="left")
        self.minsec_entry = T.entry(adv_row, width=64)
        self.minsec_entry.pack(side="left", padx=(6, 2))
        ctk.CTkLabel(adv_row, text="秒", font=T.FONT_BODY).pack(side="left")
        ctk.CTkLabel(adv_row, text="清晰度（高度）", font=T.FONT_BODY).pack(side="left", padx=(24, 0))
        self.height_entry = T.entry(adv_row, width=72)
        self.height_entry.pack(side="left", padx=(6, 2))
        ctk.CTkLabel(adv_row, text="px，1080 推荐", font=T.FONT_SMALL,
                     text_color=T.FAINT).pack(side="left")

        fps_row = ctk.CTkFrame(card2.body, fg_color="transparent")
        fps_row.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ctk.CTkLabel(fps_row, text="帧率", font=T.FONT_BODY).pack(side="left")
        self.fps_combo = T.combo(fps_row, values=["10", "15", "30"], width=72)
        self.fps_combo.set("10")
        self.fps_combo.pack(side="left", padx=(6, 2))
        ctk.CTkLabel(fps_row, text="fps，画面是静止的幻灯片，10 足够；比 30 快约 5 倍",
                     font=T.FONT_SMALL, text_color=T.FAINT).pack(side="left")

        enc_row = ctk.CTkFrame(card2.body, fg_color="transparent")
        enc_row.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ctk.CTkLabel(enc_row, text="视频编码", font=T.FONT_BODY).pack(side="left")
        self.enc_combo = T.combo(enc_row, values=list(ENC_MODE_LABELS.values()),
                                 width=190, command=self._on_enc_mode_change)
        self.enc_combo.pack(side="left", padx=(6, 10))
        T.button(enc_row, "重新自检", width=88,
                 command=self._run_selfcheck).pack(side="left")

        self.enc_status = ctk.CTkLabel(card2.body, text="", font=T.FONT_SMALL,
                                       text_color=T.FAINT, anchor="w", justify="left",
                                       wraplength=900)
        self.enc_status.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(4, 0))

    # ---------------- 标签页 2：教材 → 课程 ----------------
    def _build_book_tab(self, root):
        run_frame = ctk.CTkFrame(root, fg_color="transparent")
        run_frame.pack(side="bottom", fill="x", pady=(10, 2))
        self.cw_start_btn = T.cta(run_frame, "开始生成课程", command=self._start_courseware)
        self.cw_start_btn.pack(side="left")

        root = self._scroll_area(root)
        card1 = T.Card(root, "AI 引擎", step=1)
        card1.pack(fill="x", pady=(10, 8))
        ctk.CTkLabel(card1.head, text="任意 OpenAI 兼容接口，教材识别需支持图片输入的多模态模型",
                     font=T.FONT_SMALL, text_color=T.FAINT).pack(side="right")

        g = card1.body
        g.columnconfigure(1, weight=1)
        g.columnconfigure(3, weight=1)
        ctk.CTkLabel(g, text="接口地址：", font=T.FONT_BODY).grid(row=0, column=0, sticky="w", pady=4)
        self.ai_url = T.entry(g, placeholder_text="https://api.deepseek.com/v1")
        self.ai_url.grid(row=0, column=1, sticky="ew", padx=(4, 14), pady=4)
        ctk.CTkLabel(g, text="模型：", font=T.FONT_BODY).grid(row=0, column=2, sticky="w", pady=4)
        self.ai_model = T.entry(g, placeholder_text="如 deepseek-chat / qwen-vl-max / glm-4v")
        self.ai_model.grid(row=0, column=3, sticky="ew", padx=(4, 14), pady=4)
        ctk.CTkLabel(g, text="API Key：", font=T.FONT_BODY).grid(row=1, column=0, sticky="w", pady=4)
        self.ai_key = T.entry(g, show="*")
        self.ai_key.grid(row=1, column=1, columnspan=3, sticky="ew", padx=(4, 14), pady=4)
        T.button(g, "测试连接", width=96,
                 command=self._test_ai).grid(row=1, column=4, pady=4)

        card2 = T.Card(root, "教材与产出设置", step=2)
        card2.pack(fill="both", expand=True)
        g2 = card2.body
        g2.columnconfigure(1, weight=1)

        ctk.CTkLabel(g2, text="扫描版 PDF：", font=T.FONT_BODY).grid(row=0, column=0, sticky="w", pady=4)
        self.pdf_entry = T.entry(g2, placeholder_text="选择教材 PDF（扫描版或文字版均可）")
        self.pdf_entry.grid(row=0, column=1, sticky="ew", pady=4)
        T.button(g2, "浏览", width=72, command=self._pick_pdf).grid(row=0, column=2, padx=(10, 0), pady=4)

        ctk.CTkLabel(g2, text="输出目录：", font=T.FONT_BODY).grid(row=1, column=0, sticky="w", pady=4)
        self.cw_out = T.entry(g2, placeholder_text="留空 = 和 PDF 同目录")
        self.cw_out.grid(row=1, column=1, sticky="ew", pady=4)
        T.button(g2, "浏览", width=72, command=self._pick_cw_out).grid(row=1, column=2, padx=(10, 0), pady=4)

        ctk.CTkLabel(g2, text="切分方式：", font=T.FONT_BODY).grid(row=2, column=0, sticky="w", pady=4)
        t2 = ctk.CTkFrame(g2, fg_color="transparent")
        t2.grid(row=2, column=1, columnspan=2, sticky="w", pady=4)
        self.cw_split_var = ctk.StringVar(value="pages")
        T.radio(t2, "按每节页数（推荐）", variable=self.cw_split_var, value="pages",
                command=self._on_split_mode_change).pack(side="left", padx=(0, 22))
        T.radio(t2, "按教材章节（自动识别目录/小节，节边界对齐小节）", variable=self.cw_split_var,
                value="chapters", command=self._on_split_mode_change).pack(side="left")

        ctk.CTkLabel(g2, text="起始页：", font=T.FONT_BODY).grid(row=3, column=0, sticky="w", pady=4)
        t3 = ctk.CTkFrame(g2, fg_color="transparent")
        t3.grid(row=3, column=1, columnspan=2, sticky="w", pady=4)
        self.cw_start = T.entry(t3, width=64)
        self.cw_start.pack(side="left")
        ctk.CTkLabel(t3, text="页", font=T.FONT_BODY).pack(side="left", padx=(6, 0))
        ctk.CTkLabel(t3, text="跳过封面/目录，仅「按每节页数」生效", font=T.FONT_SMALL,
                     text_color=T.FAINT).pack(side="left", padx=(6, 24))
        ctk.CTkLabel(t3, text="每节页数：", font=T.FONT_BODY).pack(side="left", padx=(0, 6))
        self.cw_per = T.entry(t3, width=64)
        self.cw_per.pack(side="left")
        ctk.CTkLabel(t3, text="页", font=T.FONT_BODY).pack(side="left", padx=(6, 0))
        ctk.CTkLabel(t3, text="如 15：300 页 → 20 节", font=T.FONT_SMALL,
                     text_color=T.FAINT).pack(side="left", padx=(6, 24))
        ctk.CTkLabel(t3, text="最短节：", font=T.FONT_BODY).pack(side="left", padx=(0, 6))
        self.cw_min_lesson = T.entry(t3, width=64)
        self.cw_min_lesson.pack(side="left")
        ctk.CTkLabel(t3, text="页", font=T.FONT_BODY).pack(side="left", padx=(6, 0))
        ctk.CTkLabel(t3, text="低于它的短节并入相邻节，0/空 = 不合并", font=T.FONT_SMALL,
                     text_color=T.FAINT).pack(side="left", padx=(6, 0))

        ctk.CTkLabel(g2, text="每节时长：", font=T.FONT_BODY).grid(row=4, column=0, sticky="w", pady=4)
        t4 = ctk.CTkFrame(g2, fg_color="transparent")
        t4.grid(row=4, column=1, columnspan=2, sticky="w", pady=4)
        self.cw_minutes = T.entry(t4, width=64)
        self.cw_minutes.pack(side="left")
        ctk.CTkLabel(t4, text="分钟", font=T.FONT_BODY).pack(side="left", padx=(6, 0))
        ctk.CTkLabel(t4, text="一节课 30~40 推荐", font=T.FONT_SMALL,
                     text_color=T.FAINT).pack(side="left", padx=(6, 24))
        ctk.CTkLabel(t4, text="PPT 风格", font=T.FONT_BODY).pack(side="left", padx=(0, 6))
        self.cw_theme = T.combo(t4, values=list(THEME_LABELS.values()), width=200)
        self.cw_theme.pack(side="left")

        ctk.CTkLabel(g2, text="产出内容：", font=T.FONT_BODY).grid(row=5, column=0, sticky="w", pady=4)
        t5 = ctk.CTkFrame(g2, fg_color="transparent")
        t5.grid(row=5, column=1, columnspan=2, sticky="w", pady=4)
        self.ck_ppt = T.checkbox(t5, "PPT 课件", onvalue=1, offvalue=0)
        self.ck_ppt.pack(side="left", padx=(0, 18))
        self.ck_video = T.checkbox(t5, "口播视频", onvalue=1, offvalue=0)
        self.ck_video.pack(side="left", padx=(0, 18))
        self.ck_notes = T.checkbox(t5, "学习笔记", onvalue=1, offvalue=0)
        self.ck_notes.pack(side="left", padx=(0, 18))
        self.ck_quiz = T.checkbox(t5, "题库", onvalue=1, offvalue=0)
        self.ck_quiz.pack(side="left", padx=(0, 8))
        ctk.CTkLabel(t5, text="题量", font=T.FONT_BODY).pack(side="left")
        self.cw_quizn = T.combo(t5, values=[*QUIZ_PRESETS, QUIZ_CUSTOM], width=104,
                                command=self._on_quizn_change)
        self.cw_quizn.set("15")
        self.cw_quizn.pack(side="left", padx=(6, 0))
        self.cw_quizn_custom = T.entry(t5, width=56, placeholder_text="3~100")
        self.quizn_hint = ctk.CTkLabel(t5, text="题/节", font=T.FONT_SMALL, text_color=T.FAINT)
        self.quizn_hint.pack(side="left", padx=(6, 18))
        self.ck_pdf = T.checkbox(t5, "Word 再转 PDF", onvalue=1, offvalue=0)
        self.ck_pdf.pack(side="left")
        ctk.CTkLabel(t5, text="需本机装 Word", font=T.FONT_SMALL,
                     text_color=T.FAINT).pack(side="left", padx=(6, 0))

        ctk.CTkLabel(g2, text="运行模式：", font=T.FONT_BODY).grid(row=6, column=0, sticky="w", pady=4)
        t6 = ctk.CTkFrame(g2, fg_color="transparent")
        t6.grid(row=6, column=1, columnspan=2, sticky="w", pady=4)
        self.cw_mode_var = ctk.StringVar(value="step")
        T.radio(t6, "分步确认（每步完成时问一次：继续 / 就到这里）",
                variable=self.cw_mode_var, value="step").pack(side="left", padx=(0, 22))
        T.radio(t6, "全自动（一口气跑完）",
                variable=self.cw_mode_var, value="auto").pack(side="left", padx=(0, 22))
        ctk.CTkLabel(t6, text="请求并发", font=T.FONT_BODY).pack(side="left", padx=(0, 6))
        self.cw_workers = T.combo(t6, values=["2", "4", "6", "8"], width=70)
        self.cw_workers.set("4")
        self.cw_workers.pack(side="left")
        ctk.CTkLabel(t6, text="路", font=T.FONT_BODY).pack(side="left", padx=(6, 0))
        ctk.CTkLabel(t6, text="识别/生成/配音同时发多个请求提速；被限流就调低，想更高可直接输入数字",
                     font=T.FONT_SMALL, text_color=T.FAINT).pack(side="left", padx=(8, 0))

        ctk.CTkLabel(g2, text="视频编码：", font=T.FONT_BODY).grid(row=7, column=0, sticky="w", pady=(8, 0))
        t7 = ctk.CTkFrame(g2, fg_color="transparent")
        t7.grid(row=7, column=1, columnspan=2, sticky="w", pady=(8, 0))
        self.enc_combo2 = T.combo(t7, values=list(ENC_MODE_LABELS.values()), width=190,
                                  command=self._on_enc_mode_change)
        self.enc_combo2.pack(side="left", padx=(0, 10))
        T.button(t7, "重新自检", width=88,
                 command=self._run_selfcheck).pack(side="left")
        ctk.CTkLabel(t7, text="和「PPT → 视频」页是同一个设置，改哪边都一样",
                     font=T.FONT_SMALL, text_color=T.FAINT).pack(side="left", padx=(10, 0))
        self.enc_status2 = ctk.CTkLabel(g2, text="", font=T.FONT_SMALL, text_color=T.FAINT,
                                        anchor="w", justify="left", wraplength=900)
        self.enc_status2.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(2, 0))

        tip = ("两步环环相扣：① 识别教材原文 → 每节课 PPT 课件（公式转图片）+ 口播稿 → 课件备注配音合成口播视频\n"
               "（上一节的视频在合成时，下一节的课件已经在生成，两不耽误）→ ② 教材原文生成学习笔记 + 题库\n"
               "（Word，可再转 PDF）。步骤之间可停下来，以后重跑会接着已有产物继续，不会重复花钱。「按教材章节」\n"
               "会先识别目录并把各章页码存成输出目录里的「章节结构.json」（可手工修改、删掉则重新识别）；\n"
               "识别稿里的课后习题/思考题会自动切出来，按课时单独出 Word 放到 课后习题/ 文件夹（不额外花钱）；\n"
               "产物有缓存：课件/视频/学习笔记/题库分别落在 PPT/、视频/、学习笔记/、题库/ 文件夹里，课后习题在 课后习题/；\n"
               "删掉对应文件才能强制重做那一步（旧结构的产物第一次重跑会自动搬进新文件夹）。")
        T.note_card(g2, tip).grid(row=9, column=0, columnspan=3, sticky="ew", pady=(10, 0))

    # ---------------- 底部共享：进度 + 日志 ----------------
    def _build_bottom(self):
        run_frame = ctk.CTkFrame(self, fg_color="transparent")
        run_frame.pack(fill="x", padx=20, pady=(12, 0))
        self.cancel_btn = T.button(run_frame, "取消", kind="danger", width=92, height=36,
                                   state="disabled", command=self._cancel)
        self.cancel_btn.pack(side="left")
        self.progress = ctk.CTkProgressBar(run_frame, height=8, corner_radius=4,
                                           fg_color="#E4EAF4", progress_color=T.PRIMARY)
        self.progress.pack(side="left", fill="x", expand=True, padx=16)
        self.progress.set(0)
        self.status_label = ctk.CTkLabel(run_frame, text="● 就绪", width=250, anchor="w",
                                         font=T.FONT_BODY, text_color=T.FAINT)
        self.status_label.pack(side="left")

        log_card = T.Card(self, "运行日志")
        log_card.pack(fill="x", padx=20, pady=(10, 16))
        head = log_card.head
        T.button(head, "清空", width=64, height=26, font=T.FONT_SMALL,
                 command=self._clear_log).pack(side="right")
        T.button(head, "导出日志", width=88, height=26, font=T.FONT_SMALL,
                 command=self._export_log).pack(side="right", padx=(0, 8))
        T.button(head, "打开日志", width=88, height=26, font=T.FONT_SMALL,
                 command=self._open_log).pack(side="right", padx=(0, 8))
        self.log_toggle_btn = T.button(head, "展开", width=64, height=26, font=T.FONT_SMALL,
                                       command=self._toggle_log_height)
        self.log_toggle_btn.pack(side="right", padx=(0, 8))
        self.log_box = ctk.CTkTextbox(log_card.body, font=(T.FONT, 11), wrap="word",
                                      height=LOG_H_SHORT, state="disabled", fg_color=T.SOFT,
                                      border_width=1, border_color=T.FIELD_BORDER,
                                      corner_radius=8, text_color=T.TEXT)
        self.log_box.pack(fill="both", expand=True)
        tb = self.log_box._textbox
        tb.tag_config("ok", foreground=T.SUCCESS)
        tb.tag_config("err", foreground=T.ERROR)
        tb.tag_config("warn", foreground=T.WARN)
        tb.tag_config("stage", foreground=T.PRIMARY, font=(T.FONT, 11, "bold"))

    def _toggle_log_height(self):
        """日志区在折叠/展开之间切换，方便翻看长日志。"""
        self._log_tall = not self._log_tall
        self.log_box.configure(height=LOG_H_TALL if self._log_tall else LOG_H_SHORT)
        self.log_toggle_btn.configure(text="收起" if self._log_tall else "展开")
        if self._log_tall:
            self.log_box.see("end")

    def _clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    # ---------------------------------------------------------------- 恢复
    def _restore_cfg(self):
        c = self.cfg
        self.out_entry.insert(0, c.get("out_dir", ""))
        self.range_entry.insert(0, c.get("ranges", ""))
        self.rate_entry.insert(0, c.get("rate", "+0%"))
        self.minsec_entry.insert(0, str(c.get("min_seconds", 3.0)))
        self.height_entry.insert(0, str(c.get("height", 1080)))
        self.fps_combo.set(str(c.get("fps", 10)))
        self._set_enc_mode_ui(c.get("enc_mode", "auto"))
        self._refresh_enc_status()
        self.src_var.set(c.get("text_source", "正文"))
        if c.get("provider"):
            self.provider_combo.set(c["provider"])
        self._on_provider_change()
        if c.get("voice"):
            self.voice_combo.set(c["voice"])
        for f in self.files:
            self.file_list.insert(END, f)
        self._update_file_count()

        self.ai_url.insert(0, c.get("ai_base_url", ""))
        self.ai_key.insert(0, c.get("ai_key", ""))
        self.ai_model.insert(0, c.get("ai_model", ""))
        self.cw_out.insert(0, c.get("cw_out", ""))
        self.cw_split_var.set(c.get("cw_split_mode", "pages"))
        self.cw_start.insert(0, str(c.get("cw_start_page", 1)))
        self.cw_per.insert(0, str(c.get("cw_pages_per_lesson", 15)))
        self.cw_min_lesson.insert(0, str(c.get("cw_min_lesson_pages", 0)))
        self.cw_minutes.insert(0, str(c.get("cw_minutes", 35)))
        try:
            n = int(c.get("cw_quiz_count", 15) or 15)
        except (TypeError, ValueError):
            n = 15
        if str(n) in QUIZ_PRESETS:
            self.cw_quizn.set(str(n))
        else:
            self.cw_quizn.set(QUIZ_CUSTOM)
            self.cw_quizn_custom.insert(0, str(n))
        self._on_quizn_change()
        self.cw_theme.set(THEME_LABELS.get(c.get("cw_theme", slidegen.DEFAULT_STYLE),
                                           THEME_LABELS[slidegen.DEFAULT_STYLE]))
        self.cw_mode_var.set(c.get("cw_mode", "step"))
        self.cw_workers.set(str(c.get("cw_workers", 4)))
        for ck, key, default in ((self.ck_ppt, "cw_ppt", 1), (self.ck_video, "cw_video", 1),
                                 (self.ck_notes, "cw_notes", 1), (self.ck_quiz, "cw_quiz", 1),
                                 (self.ck_pdf, "cw_pdf", 0)):
            v = int(c.get(key, default))
            (ck.select if v else ck.deselect)()
        self._on_split_mode_change()

    # ------------------------------------------------------------- actions
    def _add_files(self):
        paths = filedialog.askopenfilenames(
            title="选择 PPT 文件",
            filetypes=[("PowerPoint 演示文稿", "*.pptx *.ppt"), ("所有文件", "*.*")])
        for p in paths:
            if p not in self.files:
                self.files.append(p)
                self.file_list.insert(END, p)
        self._update_file_count()

    def _remove_files(self):
        for idx in reversed(self.file_list.curselection()):
            self.file_list.delete(idx)
            self.files.pop(idx)
        self._update_file_count()

    def _clear_files(self):
        self.file_list.delete(0, END)
        self.files.clear()
        self._update_file_count()

    def _update_file_count(self):
        n = len(self.files)
        self.file_count_label.configure(text=f"已选 {n} 个文件" if n else "尚未添加文件")

    def _pick_out(self):
        d = filedialog.askdirectory(title="选择输出目录")
        if d:
            self.out_entry.delete(0, END)
            self.out_entry.insert(0, d)

    def _pick_pdf(self):
        p = filedialog.askopenfilename(
            title="选择教材 PDF", filetypes=[("PDF 文档", "*.pdf"), ("所有文件", "*.*")])
        if p:
            self.pdf_entry.delete(0, END)
            self.pdf_entry.insert(0, p)

    def _pick_cw_out(self):
        d = filedialog.askdirectory(title="选择输出目录")
        if d:
            self.cw_out.delete(0, END)
            self.cw_out.insert(0, d)

    def _on_split_mode_change(self, *_):
        pages = self.cw_split_var.get() != "chapters"
        self.cw_start.configure(state="normal" if pages else "disabled")
        self.cw_min_lesson.configure(state="normal")

    def _on_quizn_change(self, value: str | None = None, *_):
        """题量下拉：选「自定义…」时把输入框放出来，选挡位就收回去。"""
        label = (value or self.cw_quizn.get()).strip()
        if label == QUIZ_CUSTOM:
            if self.cw_quizn_custom.winfo_manager() != "pack":
                self.cw_quizn_custom.pack(side="left", padx=(6, 0), before=self.quizn_hint)
                self.cw_quizn_custom.focus_set()
        elif self.cw_quizn_custom.winfo_manager() == "pack":
            self.cw_quizn_custom.pack_forget()

    def _quiz_count(self) -> int:
        label = self.cw_quizn.get().strip()
        if label == QUIZ_CUSTOM:
            label = self.cw_quizn_custom.get().strip()
        try:
            return int(label)
        except ValueError:
            raise ValueError("题量请填数字（3~100）") from None

    def _on_provider_change(self, *_):
        try:
            provider = get_provider(self.provider_combo.get())
            self.voice_combo.configure(values=provider.list_voices())
            self.voice_combo.set(provider.list_voices()[0])
        except Exception:
            self.voice_combo.configure(values=[])

    # ------------------------------------------------------------- 设置收集
    def _collect_settings(self) -> pipeline.Settings:
        rate = self.rate_entry.get().strip() or "+0%"
        if not RATE_RE.match(rate):
            raise ValueError("语速格式应为 +0% / -10% / +15% 这样")
        try:
            min_sec = float(self.minsec_entry.get().strip() or 3.0)
            height = int(self.height_entry.get().strip() or 1080)
            fps = int(float(self.fps_combo.get().strip() or 10))
        except ValueError:
            raise ValueError("停留秒数/清晰度/帧率请填数字") from None
        if not (1 <= min_sec <= 60):
            raise ValueError("无文字页停留时间请在 1~60 秒之间")
        if height < 480 or height > 2160:
            raise ValueError("清晰度高度请在 480~2160 之间")
        if not (5 <= fps <= 60):
            raise ValueError("帧率请在 5~60 之间")
        s = pipeline.Settings(
            text_source=self.src_var.get(),
            tts_provider=self.provider_combo.get(),
            tts_voice=self.voice_combo.get().strip(),
            tts_rate=rate,
            target_height=height,
            fps=fps,
            codec=video.resolve_codec(self._enc_mode()),
            min_slide_seconds=min_sec,
        )
        if not s.tts_voice:
            raise ValueError("请填写 TTS 音色")
        return s

    def _collect_llm_config(self) -> LLMConfig:
        url = self.ai_url.get().strip()
        model = self.ai_model.get().strip()
        if not url or not model:
            raise ValueError("请填写 AI 接口地址和模型名")
        return LLMConfig(base_url=url, api_key=self.ai_key.get().strip(), model=model)

    def _collect_courseware(self) -> courseware.Options:
        pdf = self.pdf_entry.get().strip()
        if not pdf or not Path(pdf).is_file():
            raise ValueError("请选择教材 PDF 文件")
        try:
            minutes = float(self.cw_minutes.get().strip() or 35)
            start = int(self.cw_start.get().strip() or 1)
            per = int(self.cw_per.get().strip() or 15)
            min_les = int(self.cw_min_lesson.get().strip() or 0)
        except ValueError:
            raise ValueError("起始页/每节页数/每节时长/最短节请填数字") from None
        if not (0 <= min_les <= 200):
            raise ValueError("最短节请在 0~200 之间（0 = 不合并）")
        quizn = self._quiz_count()
        mode = self.cw_split_var.get()
        if mode not in ("pages", "chapters"):
            mode = "pages"
        if mode == "pages" and start < 1:
            raise ValueError("起始页请从 1 开始")
        if not (1 <= per <= 200):
            raise ValueError("每节页数请在 1~200 之间（建议 10~30）")
        if not (10 <= minutes <= 120):
            raise ValueError("每节时长请在 10~120 分钟之间")
        theme = next((k for k, v in THEME_LABELS.items()
                      if v == self.cw_theme.get()), slidegen.DEFAULT_STYLE)
        try:
            workers = int(float(self.cw_workers.get().strip() or 4))
        except ValueError:
            workers = 4
        workers = max(1, workers)          # 不设上限：挡位只是常用值，想更高自己填
        out = self.cw_out.get().strip() or str(Path(pdf).parent)
        return courseware.Options(
            pdf_path=Path(pdf), out_dir=Path(out),
            split_mode=mode, start_page=start, pages_per_lesson=per,
            min_lesson_pages=min_les,
            target_minutes=minutes, theme=theme,
            gen_ppt=bool(self.ck_ppt.get()), gen_video=bool(self.ck_video.get()),
            gen_notes=bool(self.ck_notes.get()), gen_quiz=bool(self.ck_quiz.get()),
            quiz_count=max(3, min(100, quizn)), export_pdf=bool(self.ck_pdf.get()),
            workers=workers,
            video=self._collect_settings(),
        )

    # ------------------------------------------------------------- 运行
    def _busy(self):
        if self.worker and self.worker.is_alive():
            return True
        return False

    def _begin_run(self, title: str = "", out_dir=None):
        self.cancel_event.clear()
        self.start_btn.configure(state="disabled")
        self.cw_start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.progress.set(0)
        self.status_label.configure(text="● 运行中", text_color=T.PRIMARY)
        self._warn_count = 0
        self._start_log_file(title, out_dir)      # 定好本次的日志文件并写会话头
        self._append_log(
            f"{LOG_SEP} {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            f" 开始运行{(' · ' + title) if title else ''} {LOG_SEP}",
            tag="stage", count_warn=False, to_file=False)   # 界面上按次分段，文件里有会话头
        if self._log_path is not None:
            # 让用户一眼知道完整日志落在哪，方便跑完/出错后去取
            self._append_log(f"日志文件：{self._log_path}", to_file=False, count_warn=False)
        else:
            self._append_log("⚠ 日志写不进输出目录，本次只有界面日志（可用「导出日志」保存）",
                             tag="warn", count_warn=False)

    def _end_run(self):
        self.start_btn.configure(state="normal")
        self.cw_start_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self.status_label.configure(text="● 就绪", text_color=T.FAINT)

    def _start(self):
        if self._busy():
            return
        if not self.files:
            messagebox.showwarning("提示", "请先添加 PPT 文件")
            return
        try:
            settings = self._collect_settings()
        except ValueError as exc:
            messagebox.showwarning("参数有误", str(exc))
            return
        out_dir = self.out_entry.get().strip()
        if out_dir and not Path(out_dir).is_dir():
            messagebox.showwarning("提示", "输出目录不存在，请重新选择")
            return
        ranges = self.range_entry.get().strip()
        files = list(self.files)
        self._save_now(settings)

        self._begin_run("PPT → 视频",
                        out_dir or (Path(files[0]).parent if files else None))

        def work():
            ok, fail = 0, 0
            try:
                for i, f in enumerate(files, 1):
                    self.msg_queue.put(("status", f"({i}/{len(files)}) {Path(f).name}"))
                    self.msg_queue.put(("log", f"———— 文件 {i}/{len(files)}：{Path(f).name} ————"))
                    try:
                        outputs = self._run_pipeline(settings, f, ranges, out_dir)
                        ok += len(outputs)
                    except pipeline.Cancelled:
                        self.msg_queue.put(("log", "⛔ 已取消"))
                        raise
                    except Exception as exc:
                        fail += 1
                        self.msg_queue.put(("log", self._fail_detail(exc)))
            except pipeline.Cancelled:
                pass
            finally:
                self.msg_queue.put(("done", (ok, fail)))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _run_pipeline(self, settings, file_path, ranges, out_dir):
        target = Path(out_dir) if out_dir else Path(file_path).parent
        pl = pipeline.Pipeline(
            settings,
            log=lambda s: self.msg_queue.put(("log", s)),
            progress=lambda frac, msg: self.msg_queue.put(("progress", frac, msg)),
        )
        return pl.process_file(Path(file_path), ranges, target, cancel=self.cancel_event)

    def _start_courseware(self):
        if self._busy():
            return
        try:
            opts = self._collect_courseware()
            llm_cfg = self._collect_llm_config()
        except Exception as exc:
            messagebox.showwarning("参数有误", str(exc))
            return
        if not (opts.gen_ppt or opts.gen_video or opts.gen_notes or opts.gen_quiz):
            messagebox.showwarning("提示", "请至少勾选一种产出内容")
            return
        plan = courseware.stage_plan(opts)
        chosen_step = self.cw_mode_var.get() == "step"
        step_mode = chosen_step and len(plan) > 1
        if chosen_step and len(plan) <= 1:
            self.msg_queue.put(("log", "本次只有 1 个步骤，无需中途确认，直接跑完。"))
        # 风格询问也按用户选的模式走：全自动零打扰（AI 直接定首推），分步模式才弹候选窗
        ask_style = chosen_step
        self._save_now(None)
        llm = LLMClient(llm_cfg, log=lambda s: self.msg_queue.put(("log", s)))

        self._begin_run("教材 → 课程", opts.out_dir)

        def gate(next_name, done, out_dir):
            """工作线程侧：把确认请求交给主线程弹窗，然后阻塞等用户选择。"""
            if self.cancel_event.is_set():
                raise courseware.Cancelled()
            ev = threading.Event()
            holder = {"ok": False, "answered": False}
            self.msg_queue.put(("ask", next_name, done, str(out_dir), ev, holder))
            while not ev.wait(0.2):
                if self.cancel_event.is_set():
                    break                          # 取消：弹窗会被 watch 自动关掉
            if self.cancel_event.is_set():
                raise courseware.Cancelled()       # 走统一的"已取消"路径
            return bool(holder["ok"])

        def work():
            try:
                if opts.theme == slidegen.AUTO_STYLE and (opts.gen_ppt or opts.gen_video):
                    self._cw_pick_style(llm, opts, ask_style)
                outputs = courseware.run(
                    llm, opts,
                    log=lambda s: self.msg_queue.put(("log", s)),
                    progress=lambda frac, msg: self.msg_queue.put(("progress", frac, msg)),
                    cancel=self.cancel_event,
                    gate=gate if step_mode else None)
                self.msg_queue.put(("done", (len(outputs), 0)))
            except (courseware.Cancelled, pipeline.Cancelled):
                self.msg_queue.put(("log", "⛔ 已取消"))
                self.msg_queue.put(("done", (0, 0)))
            except Exception as exc:
                self.msg_queue.put(("log", self._fail_detail(exc)))
                self.msg_queue.put(("done", (0, 1)))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _test_ai(self):
        if self._busy():
            return
        try:
            cfg = self._collect_llm_config()
        except ValueError as exc:
            messagebox.showwarning("参数有误", str(exc))
            return
        self._append_log(f"测试连接 {cfg.base_url} / {cfg.model} …")

        def work():
            llm = LLMClient(cfg, log=lambda s: self.msg_queue.put(("log", s)))
            llm.cfg.max_retries = 1
            try:
                reply = llm.chat("", "请只回复两个字：正常", max_tokens=20)
                self.msg_queue.put(("log", f"✔ AI 连接成功，回复：{reply.strip()[:60]}"))
            except Exception as exc:
                self.msg_queue.put(("log", f"✘ AI 连接失败：{exc}"))

        threading.Thread(target=work, daemon=True).start()

    def _cancel(self):
        self.cancel_event.set()
        self.cancel_btn.configure(state="disabled")

    # -------------------------------------------------- 视频编码：自检与选择
    def _enc_mode(self) -> str:
        label = self.enc_combo.get()
        return next((k for k, v in ENC_MODE_LABELS.items() if v == label), "auto")

    def _set_enc_mode_ui(self, mode: str):
        """两个页签各有一个编码下拉，永远保持同值。"""
        label = ENC_MODE_LABELS.get(mode, ENC_MODE_LABELS["auto"])
        for combo in (self.enc_combo, self.enc_combo2):
            if combo.get() != label:
                combo.set(label)

    def _on_enc_mode_change(self, value: str | None = None, *_):
        # CTkComboBox 会把新选中的文字传进来，据此知道是哪边改的
        mode = next((k for k, v in ENC_MODE_LABELS.items() if v == value), self._enc_mode())
        self._set_enc_mode_ui(mode)
        self._refresh_enc_status()

    def _refresh_enc_status(self):
        codec = video.resolve_codec(self._enc_mode())
        text = (video.selfcheck_summary(self.cfg.get("enc_selfcheck"))
                + f"　→　本次将用：{video.ENCODER_LABELS.get(codec, codec)}")
        self.enc_status.configure(text=text)
        self.enc_status2.configure(text=text)

    def _maybe_selfcheck(self):
        """只在从没自检过的机器上自动跑一次；上次没测完（null）就再问一次。"""
        if not self.AUTO_SELCHECK or self.cfg.get("enc_selfcheck"):
            return
        self._run_selfcheck()

    def _run_selfcheck(self, *_):
        if self._enc_busy or self._busy():     # 跑任务时别抢 CPU，测出来也不准
            return
        self._enc_busy = True
        self._enc_cancel = threading.Event()
        self._overlay_progress()
        self._append_log("本机视频编码自检开始（约 5~15 秒）——自检过程会记入本日志，"
                         "关闭自检页即可回看", to_file=False, count_warn=False)

        def work():
            try:
                res = video.self_check(
                    progress=lambda s: self.msg_queue.put(("enc_progress", s)),
                    cancel=self._enc_cancel.is_set)
            except Exception as exc:      # noqa: BLE001  自检出问题不该拦住主程序
                res = {"skipped": True, "timings": {},
                       "errors": {"自检异常": str(exc)[:160]}}
            self.msg_queue.put(("enc_done", res))

        threading.Thread(target=work, daemon=True).start()

    def _skip_selfcheck(self):
        # 当前那一段探测还会跑完（不杀 ffmpeg 进程），但不会再弹结果页
        self._enc_cancel.set()
        prev = self.cfg.get("enc_selfcheck")
        if not prev or not prev.get("timings"):
            self._apply_selfcheck({"skipped": True, "timings": {}, "errors": {}}, ask=False)
        self._close_overlay()

    def _apply_selfcheck(self, res: dict, ask: bool):
        prev = self.cfg.get("enc_selfcheck") or {}
        if res.get("cancelled") and prev.get("timings"):
            res = {**prev, "cancelled": True}   # 中途跳过：别把上次的有效结论冲掉
        video.set_selfcheck(res)
        self.cfg["enc_selfcheck"] = res
        save_config(self._collect_config())
        self._refresh_enc_status()
        if ask and not self._enc_cancel.is_set():
            self._overlay_result(res)
        else:
            self._close_overlay()

    def _confirm_selfcheck(self):
        mode = self._ov_mode.get()
        if mode == "hw" and not (self.cfg.get("enc_selfcheck") or {}).get("hw_available"):
            mode = "sw"
        self._set_enc_mode_ui(mode)
        save_config(self._collect_config())
        self._refresh_enc_status()
        self._close_overlay()
        self._append_log(f"视频编码方式：{ENC_MODE_LABELS[mode]}"
                         f"（{video.ENCODER_LABELS.get(video.resolve_codec(mode), '')}）")

    # ---- 自检浮层：盖在主窗口上，不另开窗口（CTkToplevel 在本项目有销毁崩溃史）
    def _open_overlay(self):
        self._close_overlay()
        ov = ctk.CTkFrame(self, fg_color=T.BG, corner_radius=0)
        ov.place(relx=0, rely=0, relwidth=1, relheight=1)
        wrap = ctk.CTkFrame(ov, fg_color="transparent")
        wrap.place(relx=0.5, rely=0.44, anchor="center")
        card = ctk.CTkFrame(wrap, fg_color=T.CARD, corner_radius=T.CARD_RADIUS,
                            border_width=1, border_color=T.CARD_BORDER)
        card.pack()
        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=30, pady=26)
        self._overlay = ov
        ov.lift()
        return body

    def _close_overlay(self):
        ov, self._overlay = self._overlay, None
        self._overlay_step = self._overlay_bar = None
        if ov is not None:
            ov.place_forget()
            ov.destroy()

    def _overlay_progress(self):
        body = self._open_overlay()
        ctk.CTkLabel(body, text="首次启动 · 本机视频编码自检", font=T.FONT_H1,
                     text_color=T.TEXT).pack(anchor="w")
        ctk.CTkLabel(body, text="正在用一段 30 秒的测试画面，实测这台机器上 CPU 软编和\n"
                                "显卡硬编谁更快。只测这一次，结果会记住，大约 5~15 秒。",
                     font=T.FONT_SMALL, text_color=T.MUTED, wraplength=560,
                     justify="left", anchor="w").pack(anchor="w", pady=(10, 18))
        self._overlay_bar = ctk.CTkProgressBar(body, width=560, height=8,
                                               progress_color=T.PRIMARY,
                                               fg_color=T.SECONDARY)
        self._overlay_bar.set(0)
        self._overlay_bar.pack(anchor="w")
        self._overlay_step = ctk.CTkLabel(body, text="准备中 …", font=T.FONT_BODY,
                                          text_color=T.PRIMARY, wraplength=560,
                                          justify="left", anchor="w")
        self._overlay_step.pack(anchor="w", pady=(12, 0))
        self._overlay_done = 0
        row = ctk.CTkFrame(body, fg_color="transparent")
        row.pack(anchor="w", pady=(24, 0))
        T.button(row, "跳过自检，先用 CPU 软编",
                 command=self._skip_selfcheck).pack(side="left")
        ctk.CTkLabel(body, text="自检过程同时记入主窗口「运行日志」，关闭本页即可回看。",
                     font=T.FONT_SMALL, text_color=T.FAINT).pack(anchor="w", pady=(18, 0))

    def _overlay_advance(self, text: str):
        if self._overlay_step is not None:
            self._overlay_step.configure(text=text)
        if self._overlay_bar is not None:
            self._overlay_done += 1
            self._overlay_bar.set(min(1.0, self._overlay_done / 4))

    def _overlay_result(self, res: dict):
        body = self._open_overlay()
        timings = res.get("timings") or {}
        hws = [c for c in video.HW_CODECS if c in timings]
        rec = res.get("recommended") or video.SW_CODEC
        _sum = self._selfcheck_lines(res).strip().splitlines()
        if _sum:
            self._append_log(f"  [自检] {_sum[0][:200]}", to_file=False, count_warn=False)
        ctk.CTkLabel(body, text="自检完成 · 请选择视频编码方式", font=T.FONT_H1,
                     text_color=T.TEXT).pack(anchor="w")
        ctk.CTkLabel(body, text=self._selfcheck_lines(res), font=T.FONT_BODY,
                     text_color=T.MUTED, wraplength=560, justify="left",
                     anchor="w").pack(anchor="w", pady=(12, 16))

        self._ov_mode.set("hw" if video.is_hw(rec) else "sw")
        sw_text = "CPU 软件编码" + ("　← 实测更快，推荐" if rec == video.SW_CODEC else "")
        T.radio(body, sw_text, variable=self._ov_mode, value="sw").pack(anchor="w")
        if hws:
            name = video.ENCODER_LABELS[hws[0]]
            hw_text = f"显卡硬件编码 · {name}" + ("　← 实测更快，推荐" if rec == hws[0] else "")
            T.radio(body, hw_text, variable=self._ov_mode, value="hw").pack(anchor="w", pady=(8, 0))
        else:
            ctk.CTkLabel(body, text="显卡硬件编码 · 本机不可用（没有对应的显卡或驱动）",
                         font=T.FONT_BODY, text_color=T.FAINT).pack(anchor="w", pady=(8, 0))

        ctk.CTkLabel(body, text="硬编把活儿交给显卡，CPU 空出来、机器更安静；软编在本机实测更快。\n"
                                "选择会记住，随时能在「PPT → 视频」页改。硬编中途失败会自动\n"
                                "改回软编重做本节，不会产出坏文件。",
                     font=T.FONT_SMALL, text_color=T.FAINT, wraplength=560,
                     justify="left", anchor="w").pack(anchor="w", pady=(14, 0))
        row = ctk.CTkFrame(body, fg_color="transparent")
        row.pack(anchor="w", pady=(22, 0))
        T.button(row, "确定，记住这个选择", kind="primary", width=180,
                 command=self._confirm_selfcheck).pack(side="left")
        T.button(row, "重新自检", width=100,
                 command=self._run_selfcheck).pack(side="left", padx=(10, 0))

    def _selfcheck_lines(self, res: dict) -> str:
        timings = res.get("timings") or {}
        if not timings:
            err = next(iter((res.get("errors") or {}).values()), "")
            return f"没能测出可用的编码器{('：' + err) if err else ''}，将使用 CPU 软件编码。"
        secs = res.get("seconds", 30)
        lines = [f"实测（同一段 {secs:g} 秒幻灯片画面，耗时越短越快）："]
        lines += [f"　{video.ENCODER_LABELS.get(k, k)}　{v:.2f} 秒"
                  for k, v in timings.items()]
        rec = video.ENCODER_LABELS.get(res.get("recommended") or video.SW_CODEC, "")
        lines.append(f"→ 本机推荐：{rec}")
        return "\n".join(lines)

    # -------------------------------------------------------------- helpers
    def _collect_config(self) -> dict:
        theme = next((k for k, v in THEME_LABELS.items()
                      if v == self.cw_theme.get()), slidegen.DEFAULT_STYLE)
        try:
            quizn = self._quiz_count()      # 存配置可能发生在起点按钮之外（如关窗），不因为填错东西炸掉
        except ValueError:
            quizn = 15
        return {
            "files": self.files,
            "out_dir": self.out_entry.get().strip(),
            "ranges": self.range_entry.get().strip(),
            "rate": self.rate_entry.get().strip(),
            "min_seconds": self.minsec_entry.get().strip() or 3.0,
            "height": self.height_entry.get().strip() or 1080,
            "fps": self.fps_combo.get().strip() or "10",
            "enc_mode": self._enc_mode(),
            "enc_selfcheck": self.cfg.get("enc_selfcheck"),
            "text_source": self.src_var.get(),
            "provider": self.provider_combo.get(),
            "voice": self.voice_combo.get().strip(),
            "ai_base_url": self.ai_url.get().strip(),
            "ai_key": self.ai_key.get().strip(),
            "ai_model": self.ai_model.get().strip(),
            "cw_out": self.cw_out.get().strip(),
            "cw_split_mode": self.cw_split_var.get(),
            "cw_start_page": self.cw_start.get().strip() or 1,
            "cw_pages_per_lesson": self.cw_per.get().strip() or 15,
            "cw_min_lesson_pages": self.cw_min_lesson.get().strip() or 0,
            "cw_minutes": self.cw_minutes.get().strip() or 35,
            "cw_quiz_count": quizn,
            "cw_theme": theme,
            "cw_ppt": int(self.ck_ppt.get()),
            "cw_video": int(self.ck_video.get()),
            "cw_notes": int(self.ck_notes.get()),
            "cw_quiz": int(self.ck_quiz.get()),
            "cw_pdf": int(self.ck_pdf.get()),
            "cw_mode": self.cw_mode_var.get(),
            "cw_workers": self.cw_workers.get().strip() or "4",
        }

    def _save_now(self, settings):
        save_config(self._collect_config())

    def _poll(self):
        try:
            while True:
                kind, *payload = self.msg_queue.get_nowait()
                if kind == "log":
                    self._append_log(str(payload[0]))
                elif kind == "progress":
                    self.progress.set(payload[0])
                    self.status_label.configure(text=payload[1])
                elif kind == "status":
                    self.status_label.configure(text=str(payload[0]))
                elif kind == "ask":
                    self._ask_continue(*payload)
                elif kind == "style":
                    self._ask_style(*payload)
                elif kind == "enc_progress":
                    self._overlay_advance(str(payload[0]))
                    self._append_log(f"  [自检] {payload[0]}",
                                     to_file=False, count_warn=False)
                elif kind == "enc_done":
                    self._enc_busy = False
                    self._apply_selfcheck(payload[0], ask=True)
                elif kind == "done":
                    ok, fail = payload[0]
                    self._end_run()
                    if self.cancel_event.is_set():
                        self.status_label.configure(text="● 已取消", text_color=T.WARN)
                        self._append_log(f"已取消（产出 {ok} 个文件）")
                    elif fail:
                        self.status_label.configure(text="● 有失败项", text_color=T.ERROR)
                    else:
                        self.status_label.configure(text="● 已完成", text_color=T.SUCCESS)
                    if not self.cancel_event.is_set():
                        self._append_log(f"全部完成：产出 {ok} 个文件" + (f"，失败 {fail} 项" if fail else ""),
                                         count_warn=False)
                        if fail:
                            messagebox.showwarning("完成（有失败）", f"产出 {ok} 个文件，{fail} 项失败，详见日志。")
                        elif ok:
                            messagebox.showinfo("完成", f"完成，共产出 {ok} 个文件！")
                    self._summarize_warnings()
                    if self._log_path is not None:
                        self._append_log(f"日志已保存：{self._log_path}",
                                         to_file=False, count_warn=False)
                    self._finish_log_file(ok, fail)
        except queue.Empty:
            pass
        self.after(80, self._poll)

    # ---------------------------------------------------- PPT 风格（自动模式）
    def _cw_pick_style(self, llm, opts, ask: bool = True):
        """工作线程侧：AI 按书名出风格候选，写回 opts.theme。

        分步确认（ask=True）：主线程弹窗让用户点选；全自动（ask=False）：直接取 AI 首推，零打扰。
        这本书已经选过（书目录里有 PPT风格.json）就直接沿用，两种模式都不再问。
        """
        book = Path(opts.pdf_path).stem
        book_dir = courseware.book_dir_for(opts.out_dir, opts.pdf_path)
        cached = slidegen.load_style_choice(book_dir)
        if cached:
            label = slidegen.THEMES[cached["key"]].get("label", cached["key"])
            self.msg_queue.put(("log", f"  PPT 风格：沿用已选「{label}」"
                                       "（想重选就删掉书目录下的 PPT风格.json）"))
            opts.theme = cached["key"]
            return
        self.msg_queue.put(("log", "  AI 正在按书名挑选 PPT 风格…"))
        try:
            cands = slidegen.pick_style_candidates(llm, book)
        except Exception as exc:                   # noqa: BLE001  选风格失败不该拦下整本书
            self.msg_queue.put(("log", f"  ⚠ AI 选风格失败（{exc}），改用默认风格"))
            cands = []
        if not cands:
            opts.theme = slidegen.DEFAULT_STYLE
            return
        if not ask:                                # 全自动：AI 定首推就行，日志里写明理由
            pick = cands[0]
            label = slidegen.THEMES[pick["key"]].get("label", pick["key"])
            slidegen.save_style_choice(book_dir, pick["key"],
                                       (pick["reason"] + "（AI 自动选定）").strip())
            self.msg_queue.put(("log", "  PPT 风格：AI 按书名自动选定「%s」%s"
                                % (label, ("——" + pick["reason"]) if pick["reason"] else "")))
            opts.theme = pick["key"]
            return
        ev = threading.Event()
        holder = {"key": cands[0]["key"], "answered": False}
        self.msg_queue.put(("style", book, cands, ev, holder))
        while not ev.wait(0.2):
            if self.cancel_event.is_set():
                break                              # 取消：弹窗会被 watch 自动关掉
        key = holder["key"] if holder["key"] in slidegen.THEMES else cands[0]["key"]
        reason = next((c["reason"] for c in cands if c["key"] == key), "")
        slidegen.save_style_choice(book_dir, key, (reason + "（用户选定）").strip())
        self.msg_queue.put(("log", "  PPT 风格：「%s」已用于本节课件"
                            % slidegen.THEMES[key].get("label", key)))
        opts.theme = key

    def _ask_style(self, book, cands, ev, holder):
        """主线程侧：风格候选弹窗（每个候选一张离线预览图），选完唤醒工作线程。"""
        dlg = ctk.CTkToplevel(self)
        dlg.title("选择 PPT 风格")
        dlg.geometry("920x430")
        dlg.resizable(False, False)
        dlg.configure(fg_color=T.BG)
        try:
            dlg.transient(self)
            dlg.lift()
            dlg.focus_force()
        except Exception:
            pass

        def answer(key: str):
            if holder["answered"]:
                return
            holder["answered"] = True
            holder["key"] = key
            ev.set()
            try:
                # 同 _ask_continue：先隐藏、稍后再销毁，避免 CTkToplevel 的排队回调炸掉
                dlg.withdraw()
                dlg.after(400, dlg.destroy)
            except Exception:
                pass

        def watch():
            try:
                if holder["answered"] or not dlg.winfo_exists():
                    return
            except Exception:
                return
            if self.cancel_event.is_set():
                self._append_log("⛔ 已取消，风格用 AI 首推的一套")
                answer(cands[0]["key"])
                return
            dlg.after(200, watch)

        card = T.Card(dlg, "AI 推荐的 PPT 风格")
        card.pack(fill="both", expand=True, padx=18, pady=(18, 0))
        ctk.CTkLabel(card.body, text=f"按《{book}》判断，下面几套最合适——点「选这个」即可",
                     font=T.FONT_BODY_BOLD, text_color=T.TEXT).pack(anchor="w")
        row = ctk.CTkFrame(card.body, fg_color="transparent")
        row.pack(fill="both", expand=True, pady=(10, 0))
        for i, cand in enumerate(cands):
            key = cand["key"]
            label = slidegen.THEMES[key].get("label", key)
            bd = ctk.CTkFrame(row, fg_color=T.CARD, corner_radius=10, border_width=1,
                              border_color=T.CARD_BORDER)
            bd.pack(side="left", padx=(0, 12), fill="y")
            try:
                img = slidegen.style_preview_image(key, 250, 148)
                pic = ctk.CTkImage(light_image=img, dark_image=img, size=(250, 148))
                bd._pic = pic                      # 必须保引用，否则被回收后变空白
                ctk.CTkLabel(bd, image=pic, text="").pack(padx=10, pady=(10, 6))
            except Exception:                      # 预览画不出来不影响选择
                pass
            ctk.CTkLabel(bd, text=("★ " if i == 0 else "") + label,
                         font=(T.FONT, 13, "bold"),
                         text_color=T.PRIMARY if i == 0 else T.TEXT).pack(anchor="w", padx=10)
            ctk.CTkLabel(bd, text=cand.get("reason", ""), wraplength=226, justify="left",
                         font=T.FONT_SMALL, text_color=T.MUTED).pack(anchor="w", padx=10, pady=(2, 8))
            T.button(bd, "选这个", kind="primary" if i == 0 else "secondary", height=34,
                     command=lambda k=key: answer(k)).pack(fill="x", padx=10, pady=(0, 12))

        btns = ctk.CTkFrame(dlg, fg_color="transparent")
        btns.pack(side="bottom", fill="x", padx=18, pady=14)
        ctk.CTkLabel(btns, text="选中的风格会记进书目录的 PPT风格.json，下次自动沿用",
                     font=T.FONT_SMALL, text_color=T.FAINT).pack(side="left")
        T.button(btns, "用 AI 首推的风格", kind="primary", height=38, width=200,
                 command=lambda: answer(cands[0]["key"])).pack(side="right")

        dlg.protocol("WM_DELETE_WINDOW", lambda: answer(cands[0]["key"]))
        self._append_log(f"⏸ 等待选择 PPT 风格（AI 推荐 {len(cands)} 套，默认第一套）")
        dlg.after(200, watch)

    # ---------------------------------------------------- 分步确认弹窗
    def _ask_continue(self, next_name, done, out_dir, ev, holder):
        """主线程侧：非阻塞弹窗，用户选择后唤醒工作线程。"""
        dlg = ctk.CTkToplevel(self)
        dlg.title("这一步完成了")
        dlg.geometry("680x330")
        dlg.resizable(False, False)
        dlg.configure(fg_color=T.BG)
        try:
            dlg.transient(self)
            dlg.lift()
            dlg.focus_force()
        except Exception:
            pass

        card = T.Card(dlg, "本步已完成")
        card.pack(fill="both", expand=True, padx=18, pady=(18, 0))
        ctk.CTkLabel(card.body, text=done, wraplength=600, justify="left",
                     font=T.FONT_BODY_BOLD, text_color=T.SUCCESS).pack(anchor="w")

        nxt = ctk.CTkFrame(card.body, fg_color=T.PRIMARY_SOFT, corner_radius=10)
        nxt.pack(fill="x", pady=(12, 0))
        ctk.CTkLabel(nxt, text="接着做：" + next_name, wraplength=570, justify="left",
                     font=(T.FONT, 13, "bold"), text_color=T.PRIMARY).pack(anchor="w", padx=14, pady=9)
        ctk.CTkLabel(card.body, text=f"输出目录：{out_dir}", wraplength=600, justify="left",
                     text_color=T.FAINT, font=T.FONT_SMALL).pack(anchor="w", pady=(10, 0))

        def answer(ok: bool):
            if holder["answered"]:
                return
            holder["answered"] = True
            holder["ok"] = ok
            ev.set()
            try:
                # 先隐藏、稍后再销毁：CTkToplevel 内部还有排队的 after 回调，
                # 立刻 destroy 会让它拿到已销毁的窗口名而报 TclError
                dlg.withdraw()
                dlg.after(400, dlg.destroy)
            except Exception:
                pass

        def watch():
            try:
                if holder["answered"] or not dlg.winfo_exists():
                    return
            except Exception:
                return
            if self.cancel_event.is_set():
                self._append_log("⛔ 已取消，自动关闭确认框")
                answer(False)
                return
            dlg.after(200, watch)

        btns = ctk.CTkFrame(dlg, fg_color="transparent")
        btns.pack(side="bottom", fill="x", padx=18, pady=14)
        T.button(btns, f"继续 · {next_name}", kind="primary", height=38, width=180,
                 command=lambda: answer(True)).pack(side="right")
        T.button(btns, "就到这里", height=38, width=110,
                 command=lambda: answer(False)).pack(side="right", padx=(0, 10))
        T.button(btns, "打开输出目录", height=38, width=130,
                 command=lambda: self._open_dir(out_dir)).pack(side="left")

        dlg.protocol("WM_DELETE_WINDOW", lambda: answer(False))
        self._append_log(f"⏸ 等待确认：{done}；是否继续「{next_name}」？")
        dlg.after(200, watch)

    def _open_dir(self, path: str):
        try:
            p = Path(path)
            if p.is_file():
                p = p.parent
            if p.is_dir():
                os.startfile(str(p))            # noqa: S606
        except Exception as exc:
            self._append_log(f"⚠ 打不开目录：{exc}")

    # ------------------------------------------------------------ 日志落盘
    def _resolve_log_path(self, out_dir) -> Path | None:
        """日志写到输出目录；目录建不出来时兜底写 APPDATA 配置目录。"""
        for d in (out_dir, CONFIG_DIR):
            if not str(d or "").strip():
                continue
            try:
                p = Path(d)
                p.mkdir(parents=True, exist_ok=True)
                return p / LOG_NAME
            except OSError:
                continue
        return None

    def _write_log_file(self, text: str, stamp: bool = True) -> None:
        """追加一行到日志文件。线程安全；任何失败都静默，绝不影响主流程。"""
        p = self._log_path
        if p is None or not self._log_active:
            return
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {text}" if stamp else text
        try:
            with self._log_lock:
                with p.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except OSError:
            pass

    def _start_log_file(self, title: str, out_dir) -> None:
        """开一次运行的日志段：定好文件、写会话头（带时间戳，便于分段回看）。"""
        self._log_path = self._resolve_log_path(out_dir)
        self._log_active = self._log_path is not None
        if self._log_path is None:
            return
        sep = "=" * 60
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        head = (f"\n{sep}\n{ts}  开始运行{('  ·  ' + title) if title else ''}\n"
                f"输出目录：{out_dir or '—'}\n{sep}")
        try:
            with self._log_lock:
                with self._log_path.open("a", encoding="utf-8") as f:
                    f.write(head + "\n")
        except OSError:
            self._log_path = None
            self._log_active = False

    def _finish_log_file(self, ok: int, fail: int) -> None:
        """收尾：写结束行（含产出/失败/警告数），并停用文件写入。"""
        if self._log_active:
            self._write_log_file(
                f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  结束：产出 {ok} 个文件，"
                f"失败 {fail} 项，警告 {self._warn_count} 条", stamp=False)
        self._log_active = False

    def _fail_detail(self, exc: BaseException, prefix: str = "✘ 失败") -> str:
        """完整堆栈写日志文件，返回给界面的只有一行摘要。

        工作线程可直接调用：写文件带锁，返回的字符串再由调用方丢进 msg_queue。
        """
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        self._write_log_file(f"{prefix}：{exc}\n{tb.rstrip()}")
        lines = str(exc).strip().splitlines()
        head = lines[0] if lines else ""
        msg = f"{type(exc).__name__}: {head}" if head else type(exc).__name__
        note = "（详细堆栈见日志文件）" if self._log_active else ""
        return f"{prefix}：{msg[:300]}{note}"

    def _summarize_warnings(self) -> None:
        """结束时给一句汇总，免得用户不知道上面有没有漏看警告。"""
        if self._warn_count:
            self._append_log(f"⚠ 共 {self._warn_count} 条警告"
                             "（可点「展开」上翻或「打开日志」看完整记录）",
                             tag="warn", count_warn=False)

    # ------------------------------------------------------------ 日志按钮
    def _open_log(self):
        p = self._log_path
        if p is not None and p.exists():
            target = p
        elif p is not None and p.parent.is_dir():
            target = p.parent                 # 还没生成文件：退而打开所在目录
        else:
            messagebox.showinfo("还没有日志", "本次还没跑过任务；运行结束后会自动写出"
                                              f"「{LOG_NAME}」，日志区按钮即可打开。")
            return
        try:
            os.startfile(str(target))         # noqa: S606  Windows 专用
        except (OSError, AttributeError) as exc:
            messagebox.showwarning("打不开", f"{exc}\n\n日志位置：{target}")

    def _export_log(self):
        """导出日志：优先复制含完整堆栈的日志文件，没有再退回界面文本。"""
        src = self._log_path
        use_file = src is not None and src.exists() and src.stat().st_size > 0
        ui_text = self.log_box.get("1.0", "end").strip()
        if not use_file and not ui_text:
            messagebox.showinfo("没有内容", "日志还是空的，先跑一次任务吧。")
            return
        kwargs = {"title": "导出运行日志", "defaultextension": ".log",
                  "initialfile": f"运行日志_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log",
                  "filetypes": [("日志文件", "*.log"), ("文本文件", "*.txt"),
                                ("所有文件", "*.*")]}
        if use_file and src.parent.is_dir():
            kwargs["initialdir"] = str(src.parent)       # 保存框默认落在输出目录
        path = filedialog.asksaveasfilename(**kwargs)
        if not path:
            return
        dst = Path(path)
        if use_file:
            try:
                with self._log_lock:      # 复制期间不让工作线程追加，避免复制到半行
                    shutil.copyfile(src, dst)
            except OSError as exc:
                use_file = False          # 复制不了就退回界面文本，别让用户白跑一趟
                self._append_log(f"⚠ 日志文件复制失败（{exc}），改为导出界面内容",
                                 to_file=False, count_warn=False)
        if not use_file:
            try:
                dst.write_text(ui_text + "\n", encoding="utf-8")
            except OSError as exc:
                messagebox.showwarning("导出失败", str(exc))
                return
        self._append_log("✔ 日志已导出" + ("（含完整堆栈）" if use_file else "（界面内容）")
                         + f"：{dst}", to_file=False, count_warn=False)

    # ------------------------------------------------------------ 日志写入
    def _append_log(self, text: str, tag: str | None = None, count_warn: bool = True,
                    to_file: bool = True):
        """tag 不传时按首字符判断颜色；同时把这一行追加进日志文件。"""
        if tag is None:
            if text.startswith("✔") or text.startswith("全部完成"):
                tag = "ok"
            elif text.startswith(("✘", "⛔")):
                tag = "err"
            elif text.startswith(("⚠", "⏸", "已取消")):
                tag = "warn"
            elif text.startswith("════") or text.startswith(LOG_SEP):
                tag = "stage"
            else:
                tag = ""
        if count_warn and tag == "warn":
            self._warn_count += 1
        if to_file:
            self._write_log_file(text)
        self.log_box.configure(state="normal")
        if tag:
            self.log_box.insert("end", text + "\n", tag)
        else:
            self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _on_close(self):
        self.cancel_event.set()
        save_config(self._collect_config())
        self.destroy()


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
