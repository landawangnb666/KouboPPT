"""LaTeX 公式 → 高清 PNG（matplotlib mathtext，纯本地、免装 LaTeX）。

mathtext 不支持 matrix/cases/array 等环境（matplotlib 已知限制），
这类公式由本模块逐格渲染后用 PIL 网格拼装 + 大括号定界符合成。
渲染失败的公式返回 None，由调用方决定降级展示。
"""
from __future__ import annotations

import contextlib
import functools
import hashlib
import re
from pathlib import Path

import matplotlib

from . import xmlsafe

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

plt.rcParams["mathtext.fontset"] = "stix"
plt.rcParams["font.family"] = ["Microsoft YaHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

DPI = 300
# 渲染/归一化规则一变就 +1。公式图片的缓存 key 里带它，规则改进后旧图自动失效——
# 否则像 `$x^22$`（旧规则渲染成 x²2 却"成功"落盘）修复后仍会命中旧图，用户以为没修。
CACHE_VERSION = 2
# 公式里混中文时（`E_{总}`、`v_{平均}`）改用"中文字体也能进 math"的配置渲染。
# STIX 没有中文字形，mathtext 只会画个空心方块；换 custom + 中文字体虽然丢掉数学斜体，
# 但远比整行降级成纯文本好。纯英文公式仍走 STIX。
_CJK_PREFER = ("Microsoft YaHei", "SimHei", "Noto Sans SC", "Noto Sans CJK SC",
               "DengXian", "SimSun", "DejaVu Sans")


@functools.lru_cache(maxsize=1)
def _cjk_font() -> str:
    """挑一个系统里存在的中文字体名。"""
    have = {f.name for f in matplotlib.font_manager.fontManager.ttflist}
    for name in _CJK_PREFER:
        if name in have:
            return name
    return "DejaVu Sans"


@contextlib.contextmanager
def _cjk_math():
    """临时切到"中文字体做数学字体"（mathtext.fontset=custom）的配置，退出时还原。

    公式渲染全程在主线程串行执行（matplotlib 非线程安全），所以切换全局 rcParams 是安全的。
    """
    font = _cjk_font()
    keys = ["mathtext.fontset"] + [f"mathtext.{k}" for k in
                                   ("rm", "it", "bf", "cal", "sf", "tt")]
    old = {k: plt.rcParams.get(k) for k in keys}
    try:
        plt.rcParams["mathtext.fontset"] = "custom"
        for k in ("rm", "it", "bf", "cal", "sf", "tt"):
            plt.rcParams[f"mathtext.{k}"] = font
        yield
    finally:
        for k, v in old.items():
            plt.rcParams[k] = v
# re.S 让 $$...$$ 可跨行；行内 $..$ 显式排除换行，避免跨段误匹配
_MATH_SEG = re.compile(r"(\$\$.+?\$\$|\$[^$\n]+\$)", re.S)
_CJK = re.compile(r"[一-鿿]")
_TEXTCMD = re.compile(
    r"\\(?:text|textrm|textbf|textit|mbox|mathrm|mathbf|mathit|mathsf|mathtt|"
    r"operatorname)\{([^{}]*)\}")
_ENV_START = re.compile(
    r"\\begin\{(matrix|pmatrix|bmatrix|Bmatrix|vmatrix|Vmatrix|cases|"
    r"array|aligned|align|align\*|split|subarray|gathered)\}(?:\{[a-z|@{}* ]*\})?")
_ENV_DELIMS = {
    "matrix": ("", ""), "pmatrix": ("(", ")"), "bmatrix": ("[", "]"),
    "Bmatrix": ("{", "}"), "vmatrix": ("|", "|"), "Vmatrix": ("‖", "‖"),
    "cases": ("{", ""), "array": ("", ""), "aligned": ("", ""),
    "align": ("", ""), "split": ("", ""), "subarray": ("", ""),
    "gathered": ("", ""),
}


_INLINE_DELIM = re.compile(r"\\\((.+?)\\\)", re.S)
_DISPLAY_DELIM = re.compile(r"\\\[(.+?)\\\]", re.S)


def normalize_delims(text: str) -> str:
    """统一模型写的公式定界符，并把漏写 $ 的裸数学碎片包起来。

    ① \\(x\\) / \\[x\\] → $x$ / $$x$$；
    ② 5.00×10^22、\\mathrm{m}^2、v^2 这类模型忘写定界符的，自动包成 $...$ 走公式渲染——
       否则 ^ 会在 Word/PPT 里原样显示成一个尖号（实测题库里出现过）。
    """
    text = _DISPLAY_DELIM.sub(lambda m: "$$" + m.group(1) + "$$", text)
    text = _INLINE_DELIM.sub(lambda m: "$" + m.group(1) + "$", text)
    return wrap_bare_pow(text)


# 科学计数法 / 单位幂 / 变量幂：形态明确，也是模型最常漏写定界符的地方
_EXP_ARG = r"(?:\{[^{}]*\}|[+-]?\d+)"
_POW = re.compile(
    r"(?P<mant>\d+(?:\.\d+)?)\s*[×xX*]\s*10\s*\^\s*(?P<e1>" + _EXP_ARG + r")"
    r"|(?<![A-Za-z0-9.])10\s*\^\s*(?P<e2>" + _EXP_ARG + r")"
    r"|(?<![A-Za-z_])(?P<var>[A-Za-z]{1,4})\s*\^\s*(?P<e3>" + _EXP_ARG + r")")


def _pow_repl(m: re.Match) -> str:
    if m.group("mant") is not None:                 # 5.00×10^22 → $5.00\times10^{22}$
        return "$%s\\times10^{%s}$" % (m.group("mant"), _unwrap(m.group("e1")))
    if m.group("e2") is not None:                   # 10^-3 → $10^{-3}$
        return "$10^{%s}$" % _unwrap(m.group("e2"))
    var = m.group("var")                            # m^2 → $\mathrm{m}^{2}$；v^2 → $v^{2}$
    name = "\\mathrm{%s}" % var if len(var) > 1 else var
    return "$%s^{%s}$" % (name, _unwrap(m.group("e3")))


def wrap_bare_pow(text: str) -> str:
    """把漏写 $ 的"明显是数学"的碎片包成 $...$（如 5.00×10^22、m^2、v^2）。

    已经带 $ 的片段一律不动；只处理形态明确的几种，宁可漏也不误伤普通文字。
    """
    if "^" not in text:
        return text
    out: list[str] = []
    for seg in _MATH_SEG.split(text):
        if not seg:
            continue
        if seg.startswith("$") and seg.endswith("$") and len(seg) > 2:
            out.append(seg)                          # 已经是公式，别动
        else:
            out.append(_POW.sub(_pow_repl, seg))
    return "".join(out)


def has_math(text: str) -> bool:
    return bool(_MATH_SEG.search(normalize_delims(text)))


def split_math(text: str) -> list[tuple[str, bool]]:
    """"能量 $E=mc^2$ 守恒" → [("能量 ", False), ("$E=mc^2$", True), (" 守恒", False)]"""
    text = normalize_delims(text)
    out: list[tuple[str, bool]] = []
    for seg in _MATH_SEG.split(text):
        if seg:
            out.append((seg, seg.startswith("$") and seg.endswith("$") and len(seg) > 2))
    return out


# mathtext 支持的等价写法：左边是模型/OCR 常写但不被 mathtext 认的命令。
# 一律用 (?![A-Za-z]) 断言命令词边界，避免误伤长命令（\le 不碰 \left/\leq，
# \bm 不碰 \bmod，\text 不碰 \textbf）。
_ALIASES = (
    ("stackrel", "overset"),      # \stackrel{a}{=} -> \overset{a}{=}
    ("tfrac", "frac"),
    ("le", "leq"),                # \le 不被 mathtext 支持（只认 \leq）
    ("ge", "geq"),
    ("gets", "leftarrow"),        # \gets 不被 mathtext 支持
    ("implies", "Rightarrow"),
    ("iff", "Leftrightarrow"),
    ("land", "wedge"),
    ("lor", "vee"),
)
_TEXT_STYLE = (
    ("textbf", "mathbf"), ("textit", "mathit"), ("textrm", "mathrm"),
    ("text", "mathrm"), ("bm", "mathbf"),
)
_SIZE_CMD = re.compile(
    r"\\(?:displaystyle|textstyle|scriptstyle|scriptscriptstyle|limits|nolimits|"
    r"big|Big|bigg|Bigg|bigl|bigr|Bigl|Bigr|biggl|biggr|Biggl|Biggr|bigm|Bigm)"
    r"(?![A-Za-z])")
_BRACE_WRAP = re.compile(r"\\(?:underbrace|overbrace)\s*\{([^{}]*)\}")
_XARROW = re.compile(r"\\(xrightarrow|xleftarrow)\s*\{([^{}]*)\}")
_MODP = re.compile(r"\\pmod\s*\{([^{}]*)\}")
_BMOD = re.compile(r"\\bmod(?![A-Za-z])")
_VERT = re.compile(r"\\(?:lVert|rVert|lvert|rvert)(?![A-Za-z])")
# 分子/分母各自可以是"单个字符"或"{...}"；已带花括号的会被原样重写（幂等）
_FRAC_SHORT = re.compile(
    r"\\(frac|dfrac|tfrac)\s*([0-9A-Za-z]|\{[^{}]*\})\s*([0-9A-Za-z]|\{[^{}]*\})")
_SQRT_SHORT = re.compile(r"\\sqrt\s*(?![{\[])([0-9A-Za-z])")
# mathtext 里 ^ 只作用于后一个 token：x^22 会被渲染成 x²2，多位数/带符号的指数要补花括号
_POW_EXP = re.compile(r"\^\s*([+-]?\d{2,}|[+-]\d)")


def _unwrap(g: str) -> str:
    """把 \"{x}\" 还原成 \"x\"，避免补花括号时重复嵌套。"""
    return g[1:-1] if len(g) > 1 and g[0] == "{" and g[-1] == "}" else g


def sanitize_latex(latex: str) -> str:
    """把模型/OCR 常写、但 matplotlib mathtext 不支持的 LaTeX 归一到可渲染形态。

    只做字符串层面的等价替换，幂等，不改变数学含义：
    ① \\le/\\ge/\\land/\\lor/\\tfrac/\\stackrel 换成 mathtext 认的别名；
    ② \\textbf/\\textit/\\text/\\bm 等按词边界映射（不再误伤 \\textbf、\\bmod）；
    ③ \\frac1n、\\frac12、\\sqrt2 这类省花括号写法补全花括号；
    ④ 吃掉 \\displaystyle/\\limits/\\big 等纯排版命令；
    ⑤ \\bmod/\\pmod/\\underbrace/\\xrightarrow/\\lVert 降级为等价可渲染写法。
    """
    if not latex:
        return latex
    out = xmlsafe.clean(latex)      # 控制字符会让 mathtext 直接渲染失败，先剔除
    out = _SIZE_CMD.sub("", out)                                   # ④
    out = _BRACE_WRAP.sub(lambda m: m.group(1), out)               # \underbrace{x} -> x
    out = _XARROW.sub(
        lambda m: "\\overset{%s}{%s}" % (
            m.group(2), "\\to" if m.group(1) == "xrightarrow" else "\\leftarrow"), out)
    out = _MODP.sub(lambda m: "\\ (\\mathrm{mod}\\ %s)" % m.group(1), out)
    out = _BMOD.sub(lambda m: "\\ \\mathrm{mod}", out)
    out = _VERT.sub(lambda m: "\\|", out)
    for wrong, right in _ALIASES:                                  # ①
        out = re.sub(r"\\%s(?![A-Za-z])" % wrong,
                     lambda m, r=right: "\\" + r, out)
    for wrong, right in _TEXT_STYLE:                               # ②
        out = re.sub(r"\\%s(?![A-Za-z])" % wrong,
                     lambda m, r=right: "\\" + r, out)
    out = _FRAC_SHORT.sub(
        lambda m: "\\%s{%s}{%s}" % (
            m.group(1), _unwrap(m.group(2)), _unwrap(m.group(3))), out)
    out = _SQRT_SHORT.sub(lambda m: "\\sqrt{%s}" % m.group(1), out)  # ③
    out = _POW_EXP.sub(lambda m: "^{%s}" % m.group(1), out)          # ⑥ x^22 → x^{22}
    return out


# 兼容旧名
normalize_latex = sanitize_latex


def _balanced(s: str) -> bool:
    """花括号是否配平（跳过转义的 \\{ \\}）。"""
    depth = 0
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth < 0:
                return False
        i += 1
    return depth == 0


def _simplify(latex: str) -> str:
    """去掉 \\text/\\mathrm 等"文本外壳"，作为渲染失败后的第二次尝试。

    必要时牺牲粗斜体，换"能出图"：a_{\\text{Si}} → a_{Si}、\\frac{\\mathrm{d}y}{\\mathrm{d}x} → \\frac{dy}{dx}。
    """
    return re.sub(
        r"\\(?:text|textrm|textbf|textit|mbox|mathrm|mathbf|mathit|mathsf|mathtt|"
        r"operatorname)\s*\{([^{}]*)\}", r"\1", latex)


def _math_body(core: str) -> str | None:
    """纯 math 串 → matplotlib 可解析的混排串；没法安全处理时返回 None。

    \\text{中文} 这类命令里的非 ASCII 内容要提到 math 之外（STIX 没有中文字形），
    中文因此交给中文字体渲染、其余部分保住数学斜体。
    但如果命令嵌在 {{...}} 组里——`\\frac{{\\mathrm{{d}}y}}{{\\mathrm{{d}}x}}`、`a_{{\\text{{Si}}}}`——
    按命令切分会得到 `\\frac{{` 这种未闭合的残段，直接渲染必然失败。
    这种情况不再整行放弃，而是**整段留在 math 里**，由调用方切到中文字体集渲染（见 `_cjk_math`）：
    `E_{\\text{总}}` 因此能正常出图，而不是降级成纯文本 `E_总`。
    """
    core = sanitize_latex(core)
    parts: list[str] = []
    pos = 0
    for m in _TEXTCMD.finditer(core):
        head = core[pos:m.start()]
        if head and not _balanced(head):
            return f"${core}$" if core.strip() else None   # 嵌在组里：整段交中文字体渲染
        if head:
            parts.append(f"${head}$")
        inner = m.group(1)
        # 含中文的 {..} 提到 math 之外用中文字体渲染；纯 ASCII 保留命令本体（保住粗斜体）
        parts.append(inner if _CJK.search(inner) else f"${m.group(0)}$")
        pos = m.end()
    rest = core[pos:]
    if rest:
        parts.append(f"${rest}$")
    return "".join(parts) or "$ $"


def _render_simple(body: str, out_path: Path, fontsize: float, color: str,
                   cjk: bool = False) -> Path | None:
    """body 已是 matplotlib 可解析的 $..$ 混排串。

    cjk=True：用中文字体做数学字体渲染（含中文的公式只能走这条路）。
    """
    if not body:
        return None
    tmp = out_path.with_suffix(".part.png")
    fig = None
    try:
        with (_cjk_math() if cjk else contextlib.nullcontext()):
            fig = plt.figure(figsize=(14, 2))
            fig.text(0.01, 0.5, body, fontsize=fontsize, color=color)
            fig.savefig(tmp, dpi=DPI, bbox_inches="tight", pad_inches=0.06,
                        facecolor="white")
        tmp.replace(out_path)
        return out_path
    except Exception:                            # noqa: BLE001
        tmp.unlink(missing_ok=True)
        return None
    finally:
        if fig is not None:
            plt.close(fig)


def _render_once(latex: str, out_path: Path, fontsize: float,
                 color: str, display: bool) -> Path | None:
    """单次尝试：把 latex 转成 matplotlib 串并渲染。"""
    if display:
        core = sanitize_latex(latex.strip().strip("$").strip())
        env = _extract_env(core)
        if env:
            return _render_env(env, out_path, fontsize, color)
        body = _math_body(core)
    else:
        chunks: list[str] = []
        for seg, is_math in split_math(latex):
            if not is_math:
                chunks.append(seg)
                continue
            piece = _math_body(seg.strip("$"))
            if piece is None:
                return None
            chunks.append(piece)
        body = "".join(chunks)
    if body is None:
        return None
    # 行内混排里的普通文字段本来就由中文字体画；只有 math 段含中文才需要切字体集
    return _render_simple(body, out_path, fontsize, color, cjk=bool(_CJK.search(body)))


def render(latex: str, out_path: Path, fontsize: float = 20,
           color: str = "#1a1a1a", display: bool = False) -> Path | None:
    """渲染一行内容成 PNG。

    display=True：latex 为独立公式（可带 $$ 包裹），可含 matrix/cases 环境；
    display=False：可为 "文字 $公式$ 文字" 混排行。
    成功返回 PNG 路径；语法错误返回 None（不抛异常）。
    """
    # 混排行里的"普通文字"段会原样交给 matplotlib，控制字符会让它打出
    # "does not have a glyph ... substituting with a dummy symbol" 并把占位符画进图里
    latex = xmlsafe.clean(latex)
    out_path = Path(out_path)
    if out_path.exists() and out_path.stat().st_size > 0:
        return out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    got = _render_once(latex, out_path, fontsize, color, display)
    if got is not None:
        return got
    simple = _simplify(latex)              # ② 兜底：去掉文本外壳再试一次
    if simple != latex:
        got = _render_once(simple, out_path, fontsize, color, display)
    return got


# ---------------------------------------------------------------- 环境拼装
def _extract_env(core: str):
    m = _ENV_START.search(core)
    if not m:
        return None
    env = re.sub(r"\*$", "", m.group(1))
    tag_end = m.end()
    end_tag = f"\\end{{{m.group(1)}}}"
    j = core.find(end_tag, tag_end)
    if j < 0:
        for alt in (env, env + "*"):
            j = core.find(f"\\end{{{alt}}}", tag_end)
            if j >= 0:
                break
    if j < 0:
        return None
    block_end = core.find(end_tag, tag_end)
    block_end = block_end + len(end_tag) if block_end >= 0 else j
    prefix, body, suffix = core[:m.start()], core[tag_end:j], core[block_end:]
    dl, dr = _ENV_DELIMS.get(env, ("", ""))
    pm = re.search(r"\\left\s*([(\[{|.])\s*$", prefix)
    if pm:
        dl = "" if pm.group(1) == "." else pm.group(1)
        prefix = prefix[:pm.start()]
    sm = re.match(r"\s*\\right\s*([)\]}|.])", suffix)
    if sm:
        dr = "" if sm.group(1) == "." else sm.group(1)
        suffix = suffix[sm.end():]
    return prefix.strip(), env, dl, dr, body, suffix.strip()


def _parse_grid(body: str) -> list[list[str]]:
    body = body.replace(r"\&", "\x01")
    rows = [r for r in body.split(r"\\") if r.strip("& \n")]
    return [[c.replace("\x01", r"\&").strip() for c in r.split("&")] for r in rows]


_NIB_CHARS = "{}"          # 中间有"尖"的字符：切 5 段，只拉伸上下两段直笔画


def _stretch_v(im, target_h: int, nib: bool):
    """把字形竖直拉伸到 target_h：只拉伸中间的"直笔画"段，两端的弯钩/尖保持原样。

    直接按目标高度放大整个字形，笔画会一起被拉粗——长矩阵的大括号会变成一条肥弧线。
    切片拉伸让笔画粗细仍与公式字号一致。
    """
    from PIL import Image

    if target_h <= im.height:
        return im
    if nib:                                    # { }：顶钩 / 上笔画 / 中尖 / 下笔画 / 底钩
        cuts = ((0.00, 0.22), (0.22, 0.46), (0.46, 0.54), (0.54, 0.78), (0.78, 1.00))
        grow = (1, 3)
    else:                                      # ( ) [ ] | ‖：顶 / 中段 / 底
        cuts = ((0.00, 0.16), (0.16, 0.84), (0.84, 1.00))
        grow = (1,)
    h = im.height
    bands = [im.crop((0, int(h * a), im.width, max(int(h * b), int(h * a) + 1)))
             for a, b in cuts]
    extra = target_h - sum(b.height for b in bands)
    if extra <= 0:
        return im.resize((im.width, target_h))
    add = extra // len(grow)
    grow_set = set(grow)
    out = [b.resize((b.width, b.height + add)) if i in grow_set else b
           for i, b in enumerate(bands)]
    canvas = Image.new("RGBA", (im.width, sum(b.height for b in out)), (0, 0, 0, 0))
    y = 0
    for b in out:
        canvas.paste(b, (0, y), b)
        y += b.height
    return canvas


def _char_png(ch: str, height_px: int, color: str, out_path: Path,
              base_px: int | None = None) -> Path | None:
    """画一个纵向铺满 height_px 的定界符。

    字形先按 base_px（≈公式字号）渲染，保证笔画粗细正常，再切片拉伸到目标高度。
    """
    from PIL import Image, ImageDraw, ImageFont

    if not ch:
        return None
    font_path = matplotlib.font_manager.findfont("DejaVu Sans")
    size = max(int(base_px or height_px), 10)
    try:
        font = ImageFont.truetype(font_path, size)
    except Exception:                            # noqa: BLE001
        return None
    tmp = Image.new("RGBA", (size * 3, size * 3), (0, 0, 0, 0))
    d = ImageDraw.Draw(tmp)
    d.text((size, size), ch, font=font, fill=color)
    bbox = tmp.getbbox()
    if not bbox:
        return None
    glyph = tmp.crop(bbox)
    if glyph.height < height_px:
        glyph = _stretch_v(glyph, height_px, ch in _NIB_CHARS)
    glyph.save(out_path)
    return out_path


def _render_side(tex: str, cache: Path, tag: str, fontsize: float,
                 color: str) -> Path | None:
    """渲染环境两侧的零碎内容（`F =` 这类前缀/后缀）。文件名按内容哈希，免得互相覆盖。"""
    body = _math_body(tex)
    if body is None:
        return None
    key = hashlib.sha1(
        f"{CACHE_VERSION}|{body}|{fontsize}|{color}".encode()).hexdigest()[:16]
    return _render_simple(body, cache / f"{tag}_{key}.png", fontsize, color,
                          cjk=bool(_CJK.search(body)))


def _render_env(env_info, out_path: Path, fontsize: float, color: str) -> Path | None:
    from PIL import Image

    prefix, env, dl, dr, body, suffix = env_info
    rows = _parse_grid(body)
    if not rows:
        return None
    cache = out_path.parent / "_env_cells"
    cache.mkdir(exist_ok=True)
    cell_imgs: list[list[Image.Image | None]] = []
    for ri, row in enumerate(rows):
        line: list[Image.Image | None] = []
        for ci, cell in enumerate(row):
            tex = cell if cell.strip() else r"\ "   # 空格子留占位，别让整个矩阵降级
            key = hashlib.sha1(
                f"{CACHE_VERSION}|{tex}|{fontsize}|{color}".encode()).hexdigest()[:16]
            p = cache / f"c_{key}.png"
            got = render(tex, p, fontsize=fontsize, color=color, display=True)
            line.append(Image.open(got).convert("RGBA") if got else None)
        cell_imgs.append(line)
    if any(any(c is None for c in row) for row in cell_imgs):
        for row in cell_imgs:
            for c in row:
                if c:
                    c.close()
        return None

    ncols = max(len(r) for r in cell_imgs)
    x_gap = int(fontsize * DPI / 72 * 0.30)
    y_gap = int(fontsize * DPI / 72 * 0.18)
    col_w = [max((row[j].width for row in cell_imgs if j < len(row)), default=1)
             for j in range(ncols)]
    row_h = [max(c.height for c in cell_imgs[i] if c) for i in range(len(cell_imgs))]
    grid_w = sum(col_w) + x_gap * (ncols - 1)
    grid_h = sum(row_h) + y_gap * (len(cell_imgs) - 1)

    pad = int(fontsize * DPI / 72 * 0.12)
    grid = Image.new("RGBA", (grid_w + pad * 2, grid_h + pad * 2), (255, 255, 255, 255))
    y = pad
    for ri in range(len(cell_imgs)):
        x = pad
        for ci in range(ncols):
            c = cell_imgs[ri][ci] if ci < len(cell_imgs[ri]) else None
            if c:
                grid.paste(c, (x, y), c)
            x += col_w[ci] + x_gap
        y += row_h[ri] + y_gap

    # 定界符：高度取整个网格，垂直居中。缓存单独放 _delims/，不和成品图混在一层
    delim_dir = out_path.parent / "_delims"
    delim_dir.mkdir(exist_ok=True)
    base_px = int(fontsize * DPI / 72)          # 字形按公式字号渲染，笔画才不会被拉粗
    need_h = grid_h + pad * 2
    delim_imgs: list[Image.Image | None] = []
    for ch in (dl, dr):
        if not ch:
            delim_imgs.append(None)
            continue
        dp = delim_dir / (f"d{CACHE_VERSION}_"
                          f"{hashlib.md5(ch.encode()).hexdigest()[:6]}"
                          f"_{base_px}_{need_h}.png")
        if not (dp.exists() and dp.stat().st_size):
            _char_png(ch, need_h, color, dp, base_px=base_px)
        got = dp if dp.exists() and dp.stat().st_size else None
        delim_imgs.append(Image.open(got).convert("RGBA") if got else None)

    pieces: list[Image.Image] = []
    if prefix:
        p = _render_side(prefix, cache, "prefix", fontsize, color)
        if p:
            pieces.append(Image.open(p).convert("RGBA"))
    if delim_imgs[0]:
        pieces.append(delim_imgs[0])
    pieces.append(grid)
    if delim_imgs[1]:
        pieces.append(delim_imgs[1])
    if suffix:
        p = _render_side(suffix, cache, "suffix", fontsize, color)
        if p:
            pieces.append(Image.open(p).convert("RGBA"))

    gap = int(fontsize * DPI / 72 * 0.15)
    total_w = sum(im.width for im in pieces) + gap * (len(pieces) - 1)
    total_h = max(im.height for im in pieces)
    canvas = Image.new("RGB", (total_w, total_h), "white")
    x = 0
    for im in pieces:
        canvas.paste(im, (x, (total_h - im.height) // 2), im)
        x += im.width + gap
    canvas.save(out_path, dpi=(DPI, DPI))
    for im in pieces:
        im.close()
    return out_path


def render_dir_for(cache_dir: Path, latex: str, fontsize: float, color: str,
                   display: bool) -> Path:
    """公式图片的缓存路径。key 里带 CACHE_VERSION：渲染规则升级后旧图自动失效。"""
    key = hashlib.sha1(
        f"{CACHE_VERSION}|{latex}|{fontsize}|{color}|{display}".encode()).hexdigest()[:24]
    return cache_dir / f"eq_{key}.png"


# ---------------------------------------------------------------- 可读降级
# 公式渲染不了时，若把 LaTeX 源码原样写进 Word（`a_{\text{Si}} = 0.543102\ \text{nm}`）
# 用户根本没法看。这里把它转成接近原文的纯文本，作为最后的降级展示。
_GREEK = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
    "varepsilon": "ε", "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "θ",
    "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ",
    "pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ", "upsilon": "υ", "phi": "φ",
    "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ",
    "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
}
_SYMBOL = {
    "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥", "ne": "≠", "neq": "≠",
    "approx": "≈", "equiv": "≡", "simeq": "≃", "propto": "∝", "sim": "～",
    "times": "×", "cdot": "·", "pm": "±", "mp": "∓", "div": "÷",
    "to": "→", "rightarrow": "→", "leftarrow": "←", "gets": "←",
    "Rightarrow": "⇒", "Leftarrow": "⇐", "leftrightarrow": "↔",
    "infty": "∞", "partial": "∂", "nabla": "∇", "sum": "Σ", "prod": "∏",
    "int": "∫", "iint": "∬", "oint": "∮", "in": "∈", "notin": "∉",
    "subset": "⊂", "subseteq": "⊆", "supset": "⊃", "supseteq": "⊇",
    "cup": "∪", "cap": "∩", "emptyset": "∅", "varnothing": "∅",
    "forall": "∀", "exists": "∃", "neg": "¬", "angle": "∠", "perp": "⊥",
    "parallel": "∥", "circ": "∘", "degree": "°", "ldots": "…", "cdots": "…",
    "dots": "…", "vdots": "⋮", "prime": "′", "ast": "∗",
}
_SUP_MAP = str.maketrans("0123456789+-=()n", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿ")
_SUP_CHARS = set("⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿ")


def _sup(inner: str) -> str:
    """指数尽量转成 Unicode 上标（10^{22} → 10²²），转不了就退回 ^ 写法。"""
    mapped = inner.translate(_SUP_MAP)
    return mapped if mapped and set(mapped) <= _SUP_CHARS else "^" + inner


def to_readable(latex: str) -> str:
    """LaTeX → 接近原文的纯文本，用于公式渲染失败时的降级展示。

    只求"看得懂"，不追求严格排版：
    `a_{\\text{Si}} = 0.543102\\ \\text{nm}` → `a_Si = 0.543102 nm`
    `\\frac{\\mathrm{d}E_g}{\\mathrm{d}P}` → `(dE_g)/(dP)`
    """
    s = normalize_delims(latex or "")       # 先归一（顺带处理裸幂），再统一去掉定界符
    s = s.replace("$", "").strip()
    s = re.sub(r"\\(?:left|right|[bB]igg?)\s*", "", s)
    s = re.sub(r"\\(?:text|textrm|textbf|textit|mbox|mathrm|mathbf|mathit|mathsf|"
               r"mathtt|operatorname)\s*\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"\\(?:frac|dfrac|tfrac)\s*\{([^{}]*)\}\s*\{([^{}]*)\}", r"(\1)/(\2)", s)
    s = re.sub(r"\\sqrt\s*\{([^{}]*)\}", r"√(\1)", s)
    s = re.sub(r"\\sqrt\s*([0-9A-Za-z])", r"√\1", s)
    s = re.sub(r"\\[,;:! ]", " ", s)                    # 细空格与 \␣
    s = re.sub(r"\\{2,}", " ", s)                       # 矩阵换行符
    s = re.sub(r"_\{([^{}]*)\}", r"_\1", s)
    s = re.sub(r"\^\{([^{}]*)\}", lambda m: _sup(m.group(1)), s)
    s = re.sub(r"\^([0-9n])", lambda m: _sup(m.group(1)), s)
    for name, ch in _SYMBOL.items():                    # 长命令在前，靠词边界防误伤
        s = re.sub(r"\\" + name + r"(?![A-Za-z])", lambda m, c=ch: c, s)
    for name, ch in _GREEK.items():
        s = re.sub(r"\\" + name + r"(?![A-Za-z])", lambda m, c=ch: c, s)
    s = re.sub(r"\\([A-Za-z]+)", r"\1", s)              # 其余命令：去掉反斜杠留名字
    s = s.replace("{", "").replace("}", "")
    return re.sub(r"[ \t]{2,}", " ", s).strip()
