"""界面主题：商务蓝白配色的设计令牌 + 常用控件工厂。

所有界面颜色、字号、圆角都从这里取，改一处全局生效。
"""
from __future__ import annotations

import customtkinter as ctk

FONT = "Microsoft YaHei UI"

# ---- 颜色 ----------------------------------------------------------------
BG = "#F3F5FA"                 # 页面底色
CARD = "#FFFFFF"               # 卡片
CARD_BORDER = "#E2E7F0"
FIELD = "#FFFFFF"              # 输入框
FIELD_BORDER = "#DCE3EE"
SOFT = "#F7F9FC"               # 列表、日志底色

PRIMARY = "#2160E0"            # 品牌蓝
PRIMARY_HOVER = "#1A50BE"
PRIMARY_SOFT = "#EAF1FE"       # 浅蓝底
PRIMARY_SOFT_BORDER = "#CBDCFB"

SECONDARY = "#EDF1F8"          # 次按钮
SECONDARY_HOVER = "#DFE7F3"
SECONDARY_TEXT = "#33415C"

TEXT = "#1B2430"
MUTED = "#6B7A90"
FAINT = "#9AA7B8"

SUCCESS = "#12915F"
WARN = "#B4690E"
ERROR = "#C93A3F"
DANGER_SOFT = "#FDEDED"
DANGER_SOFT_HOVER = "#F9DCDC"

# ---- 字号 ----------------------------------------------------------------
FONT_H1 = (FONT, 19, "bold")
FONT_H2 = (FONT, 14, "bold")
FONT_BODY = (FONT, 12)
FONT_BODY_BOLD = (FONT, 12, "bold")
FONT_SMALL = (FONT, 11)
FONT_BUTTON = (FONT, 13, "bold")
FONT_CTA = (FONT, 15, "bold")

RADIUS = 10
CARD_RADIUS = 14


# ---- 控件工厂 ------------------------------------------------------------
class Card(ctk.CTkFrame):
    """白色圆角卡片：标题行（可选编号徽章）+ 内容区 body。"""

    def __init__(self, parent, title: str, step: int | None = None):
        super().__init__(parent, fg_color=CARD, corner_radius=CARD_RADIUS,
                         border_width=1, border_color=CARD_BORDER)
        self.head = ctk.CTkFrame(self, fg_color="transparent")
        self.head.pack(fill="x", padx=16, pady=(13, 0))
        if step is not None:
            ctk.CTkLabel(self.head, text=str(step), width=24, height=24,
                         corner_radius=7, fg_color=PRIMARY_SOFT,
                         text_color=PRIMARY, font=(FONT, 12, "bold")).pack(side="left", padx=(0, 8))
        ctk.CTkLabel(self.head, text=title, font=FONT_H2,
                     text_color=TEXT).pack(side="left")
        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.pack(fill="both", expand=True, padx=16, pady=(10, 14))


def entry(parent, **kw) -> ctk.CTkEntry:
    kw.setdefault("height", 34)
    kw.setdefault("corner_radius", 8)
    kw.setdefault("fg_color", FIELD)
    kw.setdefault("border_color", FIELD_BORDER)
    kw.setdefault("border_width", 1)
    kw.setdefault("text_color", TEXT)
    kw.setdefault("placeholder_text_color", FAINT)
    kw.setdefault("font", FONT_BODY)
    return ctk.CTkEntry(parent, **kw)


def combo(parent, **kw) -> ctk.CTkComboBox:
    kw.setdefault("height", 34)
    kw.setdefault("corner_radius", 8)
    kw.setdefault("fg_color", FIELD)
    kw.setdefault("border_color", FIELD_BORDER)
    kw.setdefault("text_color", TEXT)
    kw.setdefault("font", FONT_BODY)
    kw.setdefault("button_color", SECONDARY)
    kw.setdefault("button_hover_color", SECONDARY_HOVER)
    kw.setdefault("dropdown_fg_color", CARD)
    kw.setdefault("dropdown_text_color", TEXT)
    return ctk.CTkComboBox(parent, **kw)


def button(parent, text: str, kind: str = "secondary", **kw) -> ctk.CTkButton:
    """kind: primary（主操作）/ secondary（次操作）/ danger（停止类）/ link（外链入口）。"""
    kw.setdefault("height", 34)
    kw.setdefault("corner_radius", 8)
    kw.setdefault("font", FONT_BODY_BOLD)
    if kind == "primary":
        kw.setdefault("fg_color", PRIMARY)
        kw.setdefault("hover_color", PRIMARY_HOVER)
        kw.setdefault("text_color", "#FFFFFF")
    elif kind == "link":
        kw.setdefault("fg_color", PRIMARY_SOFT)
        kw.setdefault("hover_color", PRIMARY_SOFT_BORDER)
        kw.setdefault("text_color", PRIMARY)
    elif kind == "danger":
        kw.setdefault("fg_color", DANGER_SOFT)
        kw.setdefault("hover_color", DANGER_SOFT_HOVER)
        kw.setdefault("text_color", ERROR)
    else:
        kw.setdefault("fg_color", SECONDARY)
        kw.setdefault("hover_color", SECONDARY_HOVER)
        kw.setdefault("text_color", SECONDARY_TEXT)
    return ctk.CTkButton(parent, text=text, **kw)


def cta(parent, text: str, **kw) -> ctk.CTkButton:
    """页面主 CTA：大尺寸品牌蓝按钮。"""
    kw.setdefault("height", 42)
    kw.setdefault("corner_radius", 10)
    kw.setdefault("width", 200)
    kw.setdefault("font", FONT_CTA)
    kw.setdefault("fg_color", PRIMARY)
    kw.setdefault("hover_color", PRIMARY_HOVER)
    kw.setdefault("text_color", "#FFFFFF")
    return ctk.CTkButton(parent, text=text, **kw)


def checkbox(parent, text: str, **kw) -> ctk.CTkCheckBox:
    kw.setdefault("font", FONT_BODY)
    kw.setdefault("text_color", TEXT)
    kw.setdefault("fg_color", PRIMARY)
    kw.setdefault("hover_color", PRIMARY_HOVER)
    kw.setdefault("border_color", "#C3CEE0")
    kw.setdefault("checkmark_color", "#FFFFFF")
    kw.setdefault("corner_radius", 5)
    kw.setdefault("checkbox_width", 20)
    kw.setdefault("checkbox_height", 20)
    return ctk.CTkCheckBox(parent, text=text, **kw)


def radio(parent, text: str, **kw) -> ctk.CTkRadioButton:
    kw.setdefault("font", FONT_BODY)
    kw.setdefault("text_color", TEXT)
    kw.setdefault("fg_color", PRIMARY)
    kw.setdefault("hover_color", PRIMARY_HOVER)
    kw.setdefault("border_color", "#C3CEE0")
    kw.setdefault("radiobutton_width", 20)
    kw.setdefault("radiobutton_height", 20)
    return ctk.CTkRadioButton(parent, text=text, **kw)


def note_card(parent, text: str) -> ctk.CTkFrame:
    """浅蓝提示卡：放流程说明这类补充信息。"""
    box = ctk.CTkFrame(parent, fg_color=PRIMARY_SOFT, corner_radius=10,
                       border_width=1, border_color=PRIMARY_SOFT_BORDER)
    ctk.CTkLabel(box, text="使用提示", font=(FONT, 11, "bold"),
                 text_color=PRIMARY).pack(anchor="w", padx=12, pady=(8, 0))
    ctk.CTkLabel(box, text=text, justify="left", wraplength=760,
                 font=FONT_SMALL, text_color="#3C5A93").pack(anchor="w", padx=12, pady=(2, 9))
    return box
