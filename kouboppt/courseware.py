"""教材 → 课程 两阶段流水线编排。

第一步：识别教材原文 → 每节课 PPT 课件（公式转图片）+ 口播稿 → TTS 配音 → 口播视频
        （视频走 1 路后台队列：上一节的视频在合成时，下一节的课件已经在生成，两不耽误）
第二步：教材原文 → 学习笔记 + 题库（Word，可选 PDF）

每一步的产出直接作为下一步的输入（课件/视频/学习笔记/题库分别放在 PPT/、视频/、学习笔记/、
题库/ 文件夹，课后习题单独放 课后习题/，识别稿与口播稿放在课节目录），所以两步可以分开跑：
单跑第二步发现缺前置产物时，会自动补做第一步。
阶段之间可插入 gate 闸门：返回 False 即"就到这里"，跳过后续步骤。

切分方式：SPLIT_PAGES = 从起始页起每 N 页一节课（余数独立成最后一节）；
SPLIT_CHAPTERS = 按教材章节（书签/AI 目录/章节结构.json）切，章内节边界对齐
目录小节再凑成约 N 页一节，绝不跨章。两种模式都支持最短节合并（min_lesson_pages）。

PPT 风格 = 配色 + 版式骨架（见 slidegen.THEMES）。Options.theme 传具体风格 key；
传 "auto" 则按书名让 AI 选一次，结果落在 书目录/PPT风格.json，全书统一且重跑不再问 AI。
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import docgen, formula_fallback, pipeline, slidegen, textbook, xmlsafe
from .llm import LLMClient, LLMError, dump_raw, salvage_objects

MIN_PER_SLIDE = 1.15          # 一页内容页平均讲话分钟数（约 250 字稿）

SLIDES_PER_CALL = 3           # 每次请求生成几页内容页（块太大时接口容易把回复截断）
QUESTIONS_PER_CALL = 8        # 每次请求出几道题

SPLIT_PAGES = "pages"         # 从起始页起每 N 页一节课
SPLIT_CHAPTERS = "chapters"   # 按教材章节切，章内再按 N 页细分


class Cancelled(Exception):
    pass


@dataclass
class Options:
    pdf_path: Path
    out_dir: Path
    split_mode: str = SPLIT_PAGES     # pages / chapters
    start_page: int = 1               # 仅 pages 模式生效（跳过封面/目录）
    pages_per_lesson: int = textbook.DEFAULT_PAGES_PER_LESSON
    min_lesson_pages: int = 0         # 低于该页数的短节自动并入相邻节；0 = 不合并
    target_minutes: float = 35.0      # 每节课时长（30~40 推荐）
    theme: str = slidegen.DEFAULT_STYLE   # 风格 key；"auto" = 按书名让 AI 选（落在 书目录/PPT风格.json）
    gen_ppt: bool = True
    gen_video: bool = True
    gen_notes: bool = True
    gen_quiz: bool = True
    quiz_count: int = 15
    export_pdf: bool = False          # Word 再转 PDF（需本机装 Word）
    workers: int = 4                  # 同时发出的请求数（1 = 完全串行）
    video: pipeline.Settings = field(default_factory=pipeline.Settings)


def _fmt_dur(seconds: float) -> str:
    s = int(round(seconds))
    if s < 60:
        return f"{s} 秒"
    return f"{s // 60} 分 {s % 60} 秒"


def _safe_name(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", s).strip("_")[:48] or "章节"


def book_dir_for(out_dir: Path, pdf_path: Path) -> Path:
    """一本书的输出根目录（输出目录/书名）——风格缓存等按书存放的东西都放这里。"""
    return Path(out_dir) / _safe_name(Path(pdf_path).stem)


def _write_range(rng: Path, start: int, end: int) -> None:
    """把本节识别的页码范围登记到 .range.json，下次重跑用来判断缓存是否还有效。"""
    try:
        rng.write_text(json.dumps({"start": int(start), "end": int(end)},
                                   ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _read_range(rng: Path) -> tuple[int, int] | None:
    """读取 .range.json 登记的页码范围；文件缺失或损坏返回 None。"""
    if not rng.exists():
        return None
    try:
        d = json.loads(rng.read_text(encoding="utf-8"))
        a, b = int(d["start"]), int(d["end"])
        if a < 1 or b < a:
            return None
        return a, b
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _check(cancel):
    if cancel is not None and cancel.is_set():
        raise Cancelled()


class _AnyCancel:
    """把几个取消开关合成一个（只用 .is_set()，pipeline 与 _check 都认）。

    后台视频线程用它：出错退出时置位内部开关，让正在合成的那一节尽快停下，
    免得用户紧接着重跑时两个线程往同一份产物写文件。
    """

    def __init__(self, *events):
        self._events = [e for e in events if e is not None]
        self.own = threading.Event()

    def is_set(self) -> bool:
        return self.own.is_set() or any(e.is_set() for e in self._events)


# ---------------------------------------------------------------- AI 内容
_OUTLINE_SYS = "你是资深课程课件总编辑，擅长把教材拆成讲课大纲。"

# 模型在中文句子里写科学计数法/单位幂时最常忘掉 $ 定界符（结果 ^ 原样显示成尖号），
# 这里用具体例子把它说清楚；渲染层 wrap_bare_pow 还会再兜一层。
_MATH_HINT = (
    "数学表达式一律写成 LaTeX 并用 $...$ 包住：科学计数法写 $5.00\\times10^{22}$"
    "（不要写成 10^22、5.00×10^22 这类裸写法）、单位幂写 $\\mathrm{m}^{2}$、"
    "变量幂写 $v^{2}$；指数是多位数或带符号时必须加花括号，如 $x^{22}$、$10^{-3}$。"
    "纯数字、以及中文句子里的普通数字不要加 $。")

_SLIDE_RULES = """要求：
- 内容严格来自教材原文：只讲原文讲到的概念、结论、例子，不得补充原文之外的主题。
- 公式用标准 LaTeX：行内 $...$，独立公式 $$...$$ 单独成行（只用这两种定界符，不要 \\(...\\) 或 \\[...\\]）；只用常见命令（\\frac \\sqrt \\int \\sum 上下标、希腊字母、matrix/cases/aligned），不要 \\usepackage 类宏包；多行推导写成 $$\\begin{aligned} 第一行 \\\\ 第二行 \\end{aligned}$$（每行用 & 对齐，行末写 \\\\）。
- 忠实教材原文，覆盖原文讲到的定义、要点、公式、例题（例题把题目和解答步骤写进 lines）。
- 每个 slide 的 lines 为 3~6 条，每条不超过 45 个汉字；
- script 是该页的口播稿 200~280 字，口语化讲解；其中的公式一律写成中文读法（如"x 分之 dx"），不得出现 LaTeX 符号或 $。
""" + _MATH_HINT


def _doc_or_report(what: str, st_dir: Path, payload, build, log):
    """生成文档（Word/PPT）；失败时把源数据留档，方便定位是哪个字符出的问题。

    正常路径下 xmlsafe 已经清掉了 XML 非法字符，这里是兜底：真出别的问题时，
    日志里能直接看出事的数据——repr 会把 \\x0b 这类不可见字符显式打印出来，
    并额外列出命中的非法码位。

    另外汇总本次生成里**渲染失败的公式**：Word 侧过去只静默降级成纯文本，
    日志里一句提示都没有，用户根本不知道公式没画出来。
    """
    docgen.reset_failed()
    try:
        return build()
    except Exception as exc:                      # noqa: BLE001
        raw = payload if isinstance(payload, str) else repr(payload)
        bad = xmlsafe.illegal(raw)
        dump_raw(st_dir, f"{what}（生成失败 {type(exc).__name__}）", repr(raw)[:20000])
        log(f"  ⚠ {what} 生成失败：{type(exc).__name__}: {exc}")
        log("    源数据里的 XML 非法字符：" + ("、".join(bad) if bad else "未发现")
            + f"；原始数据已留档 {st_dir / 'AI原始回复.log'}")
        raise
    finally:
        fails = docgen.drain_failed()
        if fails:
            log(f"  ⚠ {what}有 {len(fails)} 处公式渲染失败，已降级为可读文字，"
                f"如：{fails[0][:120]}")
            _dump_failed_formulas(st_dir, fails, log)


def _dump_failed_formulas(st_dir: Path, items: list[str], log) -> None:
    """把渲染失败的公式原文单独留档——日志里只够显示一条，排查需要完整清单。"""
    try:
        st_dir.mkdir(parents=True, exist_ok=True)
        with (st_dir / "公式渲染失败.log").open("a", encoding="utf-8") as f:
            f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 共 {len(items)} 处 =====\n")
            f.write("\n".join(items) + "\n")
    except OSError:
        return
    log(f"    完整清单见 {st_dir / '公式渲染失败.log'}")


def _chat_json_tolerant(llm: LLMClient, system: str, user: str, max_tokens: int,
                        array_key: str, tag: str, raw_dir: Path | None = None) -> dict:
    """要 JSON；整段解析失败时，从（可能被截断的）原始回复里抢救完整的对象。"""
    try:
        return llm.chat_json(system, user, max_tokens=max_tokens,
                             raw_dir=raw_dir, tag=tag)
    except LLMError as exc:
        raw = str(getattr(exc, "raw_text", "") or "")
        dump_raw(raw_dir, f"{tag}（解析失败）", raw)
        objs = salvage_objects(raw, array_key)
        if not objs:
            raise
        llm.log(f"  ⚠ {tag}：回复不是合法 JSON（接口可能截断了输出），"
                f"已从中抢救出 {len(objs)} 条完整内容继续")
        return {array_key: objs}


def plan_outline(llm: LLMClient, chapter_md: str, minutes: float,
                 raw_dir: Path | None = None) -> list[dict]:
    n_slides = max(8, round(minutes / MIN_PER_SLIDE))
    data = _chat_json_tolerant(
        llm, _OUTLINE_SYS,
        f"下面是一节课对应的教材原文：\n{chapter_md}\n\n"
        f"请严格依据上面原文的知识脉络，规划一节约 {minutes:g} 分钟、共 {n_slides} 页内容页的课件大纲，"
        "分 3~6 个 section，slides 数之和等于总页数；小节标题用原文里的概念来命名，"
        "不要引入原文没有的主题。\n"
        '输出 JSON：{"sections":[{"title":"小节标题","focus":"本段讲什么","slides":页数}]}',
        2000, "sections", "课件大纲", raw_dir)
    sections = data.get("sections") if isinstance(data, dict) else data
    if not sections:
        raise ValueError("AI 大纲生成失败")
    return sections


def gen_section_slides(llm: LLMClient, chapter_md: str, sec: dict,
                       script_chars: int, raw_dir: Path | None = None) -> list[dict]:
    total = max(1, int(sec.get("slides") or 1))
    pages: list[dict] = []
    for a in range(1, total + 1, SLIDES_PER_CALL):
        b = min(total, a + SLIDES_PER_CALL - 1)
        part = ("" if total <= SLIDES_PER_CALL else
                f"（本小节共 {total} 页，本次只出第 {a}~{b} 页，共 {b - a + 1} 页）")
        prev = ""
        if pages:
            done = "、".join(str(p.get("title", ""))[:12] for p in pages[-6:])
            prev = f"本小节前面已生成：{done}；接着往下讲，不要重复。\n"
        data = _chat_json_tolerant(
            llm, _OUTLINE_SYS,
            f"教材原文：\n{chapter_md}\n\n"
            f"请为小节《{sec['title']}》（{sec.get('focus', '')}）生成 {b - a + 1} 页内容页{part}。\n"
            + prev
            + "内容全部取自上面的教材原文，小节标题只是切分提示，若与原文不符以原文为准。\n"
            + _SLIDE_RULES +
            f"\n每页 script 约 {script_chars} 字。\n"
            '输出 JSON：{"slides":[{"title":"...","lines":["...","$$...$$"],"script":"..."}]}',
            8000, "slides", f"小节《{sec['title']}》第{a}-{b}页", raw_dir)
        got = data.get("slides") if isinstance(data, dict) else data
        if not got:
            raise ValueError(f"小节《{sec['title']}》第 {a}~{b} 页内容生成失败")
        pages.extend(got)
    return pages


def gen_lesson(llm: LLMClient, chapter_md: str, title: str, book: str,
               minutes: float, theme: str, workers: int = 1,
               raw_dir: Path | None = None) -> slidegen.LessonSpec:
    sections_plan = plan_outline(llm, chapter_md, minutes, raw_dir)
    script_chars = int(MIN_PER_SLIDE * 230)

    def one(sec: dict) -> slidegen.SectionSpec:
        spec = slidegen.SectionSpec(title=str(sec.get("title", "内容")))
        for s in gen_section_slides(llm, chapter_md, sec, script_chars, raw_dir):
            lines = [str(x) for x in (s.get("lines") or []) if str(x).strip()]
            spec.slides.append(slidegen.SlideSpec(
                title=str(s.get("title", spec.title))[:40],
                lines=lines, script=str(s.get("script", "")).strip()))
        return spec

    n = max(1, min(int(workers or 1), len(sections_plan)))
    if n <= 1:
        specs = [one(sec) for sec in sections_plan]
    else:
        with ThreadPoolExecutor(n) as pool:
            futs = [pool.submit(one, sec) for sec in sections_plan]
            try:
                specs = [f.result() for f in futs]      # 按小节顺序取结果
            except BaseException:
                for f in futs:
                    f.cancel()
                raise

    lesson = slidegen.LessonSpec(title=title, subtitle=book, theme=theme)
    for spec in specs:
        if spec.slides:
            lesson.sections.append(spec)
    if not lesson.sections:
        raise ValueError("AI 未能生成任何幻灯片内容")
    return lesson


# 笔记分两段请求（一次要到 4000 字更容易被接口截断，且截断会悄悄丢掉结尾的例题/易错点）
_NOTE_PARTS = (
    ("上篇", "# 知识框架（本次课脉络）/ ## 核心概念与要点（逐条讲清）", 1400),
    ("下篇", "重要公式（若有：$$ 独立成行、行内 $...$）/ 典型例题（若有，含解答步骤）/ "
             "易错点总结（3~6 条）", 1600),
)


def gen_notes_md(llm: LLMClient, chapter_md: str,
                 raw_dir: Path | None = None) -> str:
    parts: list[str] = []
    for name, outline, chars in _NOTE_PARTS:
        hint = ""
        if parts:
            heads = [ln.strip() for ln in parts[-1].splitlines()
                     if ln.strip().startswith("#")]
            if heads:
                hint = ("前面已写好的部分（只写下面这部分，不要重复）："
                        + "；".join(h[:30] for h in heads[:12]) + "\n")
        parts.append(llm.chat(
            "你是学习笔记撰写专家。",
            f"根据教材原文写本次课学习笔记的{name}（Markdown，约 {chars} 字），内容以原文为准。\n"
            f"{_MATH_HINT}\n"
            f"{hint}本部分只写：{outline}。只输出 Markdown 本身。\n\n" + chapter_md,
            max_tokens=6000, raw_dir=raw_dir, tag=f"学习笔记{name}").strip())
    return "\n\n".join(parts)


def gen_questions(llm: LLMClient, chapter_md: str, count: int,
                  raw_dir: Path | None = None) -> list[dict]:
    count = max(1, int(count))
    qs: list[dict] = []
    for a in range(0, count, QUESTIONS_PER_CALL):
        n = min(QUESTIONS_PER_CALL, count - a)
        part = ("" if count <= QUESTIONS_PER_CALL else
                f"（本次是全套 {count} 道题里的第 {a + 1}~{a + n} 道，只出这 {n} 道）")
        prev = ""
        if qs:
            stems = "；".join(str(q.get("stem", ""))[:16] for q in qs[-n:])
            prev = f"前面已出过的题（不要重复）：{stems}\n"
        data = _chat_json_tolerant(
            llm, "你是命题老师。",
            f"教材原文：\n{chapter_md}\n\n"
            + prev
            + f"请严格依据上面的原文出 {n} 道题{part}（只考原文里的知识点）：约 40% 单选、"
            "20% 多选、20% 填空、20% 解答。覆盖本次课核心考点，难度贴合教材课后题水平。"
            f"{_MATH_HINT}\n"
            '输出 JSON：{"questions":[{"type":"single|multi|blank|short",'
            '"stem":"题干","options":["A. ...","B. ..."],"answer":"A 或 答案文字",'
            '"explanation":"解析"}]}（填空/解答题 options 给空列表）',
            8000, "questions", f"题库第{a + 1}-{a + n}题", raw_dir)
        got = data.get("questions") if isinstance(data, dict) else data
        if not got:
            raise ValueError(f"题库第 {a + 1}~{a + n} 题生成失败")
        qs.extend(got)
    return qs


# ---------------------------------------------------------------- 两阶段
STAGE_PPT = "ppt"        # 第一步：识别原文 → 课件 + 口播稿 → 口播视频（内部流水线并行）
STAGE_NOTES = "notes"    # 第二步：教材原文 → 学习笔记 + 题库

# gate(下一步名称, 本步完成说明, 输出目录) -> 是否继续
GateFn = Callable[[str, str, Path], bool]


def _noop_gate(next_name: str, done: str, out_dir: Path) -> bool:
    return True


def _short_list(items: list[str], head: int = 4) -> str:
    if len(items) <= head:
        return "、".join(items)
    return "、".join(items[:head]) + f" 等 {len(items)} 个"


@dataclass
class ChapterState:
    idx: int
    title: str
    start: int
    end: int
    dir: Path
    md: str = ""              # 教材原文（Markdown，含 LaTeX；已去掉课后习题部分）
    ex: str = ""              # 从识别稿里切出的课后习题/思考题（空 = 本节课没有）
    pptx: Path | None = None
    ticked_md: bool = False   # 进度单位只记一次，跨阶段复用产物不重复计数
    ticked_ppt: bool = False
    base: str = ""            # 产物文件名用的标题（同名课时加 _2 区分）
    sliced: bool = False      # 识别稿是否已切过课后习题（切一次就够）


def _chapter_dir(book_dir: Path, idx: int, title: str, log=print) -> Path:
    """按"序号_标题"精确匹配旧目录；序号相同但名字不同的旧目录不复用（防止换切分参数后误用缓存）。"""
    name = f"{idx:02d}_{_safe_name(title)}"
    olds = [p.name for p in sorted(book_dir.glob(f"{idx:02d}_*"))
            if p.is_dir() and p.name != name]
    if olds:
        log(f"  ⚠ 目录 {olds[0]} 与本次切分不一致（本次：{name}）——为避免误用旧缓存，"
            "本次用新目录重做；确认无用可手动删除旧目录")
    return book_dir / name


PPT_DIR = "PPT"       # 课件单独一个文件夹，文件名 = 课时标题 + "PPT"
VIDEO_DIR = "视频"    # 口播视频同理，文件名 = 课时标题 + "视频"
EX_DIR = "课后习题"   # 课后习题/思考题单独成文，文件名 = 课时标题 + "课后习题"
NOTE_DIR = "学习笔记"  # 学习笔记同理，文件名 = 课时标题 + "学习笔记"
QUIZ_DIR = "题库"     # 题库同理，文件名 = 课时标题 + "题库"


def _ppt_path(book_dir: Path, st: ChapterState) -> Path:
    return book_dir / PPT_DIR / f"{_safe_name(st.base)}PPT.pptx"


def _video_path(book_dir: Path, st: ChapterState) -> Path:
    return book_dir / VIDEO_DIR / f"{_safe_name(st.base)}视频.mp4"


def _ex_path(book_dir: Path, st: ChapterState) -> Path:
    return book_dir / EX_DIR / f"{_safe_name(st.base)}课后习题.docx"


def _note_path(book_dir: Path, st: ChapterState) -> Path:
    return book_dir / NOTE_DIR / f"{_safe_name(st.base)}学习笔记.docx"


def _quiz_path(book_dir: Path, st: ChapterState) -> Path:
    return book_dir / QUIZ_DIR / f"{_safe_name(st.base)}题库.docx"


def _adopt_legacy(book_dir: Path, states: list[ChapterState], log=print) -> None:
    """旧结构的课件/视频原来放在课节目录里：原地搬进新文件夹，搬完即算缓存，不重复花钱。"""
    moved = 0
    for st in states:
        olds = sorted(st.dir.glob("*_课件.pptx"))
        if not olds or not (st.dir / "口播稿.txt").exists():
            continue                      # 与旧缓存判据一致：课件和口播稿齐了才算数
        ppt, vid = _ppt_path(book_dir, st), _video_path(book_dir, st)
        old_vid = st.dir / f"{olds[0].stem}.mp4"
        for old, new in ((olds[0], ppt), (old_vid, vid)):
            if new.exists() or not old.exists():
                continue
            new.parent.mkdir(parents=True, exist_ok=True)
            old.replace(new)
            moved += 1
    if moved:
        log(f"  旧版产物已搬进 {PPT_DIR}/ 与 {VIDEO_DIR}/（共 {moved} 个，照样算缓存，不重复生成）")


def _adopt_legacy_docs(book_dir: Path, states: list[ChapterState], log=print) -> None:
    """旧版笔记/题库曾放在课节目录里（`01_学习笔记.docx` / `01_题库.docx`）：
    原地搬进 学习笔记/ 与 题库/，搬完即算缓存，不重复花钱。"""
    moved = 0
    for st in states:
        for old, new in ((st.dir / f"{st.idx:02d}_学习笔记.docx", _note_path(book_dir, st)),
                         (st.dir / f"{st.idx:02d}_题库.docx", _quiz_path(book_dir, st))):
            if new.exists() or not old.exists():
                continue
            new.parent.mkdir(parents=True, exist_ok=True)
            old.replace(new)
            moved += 1
            old_pdf = old.with_suffix(".pdf")
            if old_pdf.exists():
                old_pdf.replace(new.with_suffix(".pdf"))
    if moved:
        log(f"  旧版笔记/题库已搬进 {NOTE_DIR}/ 与 {QUIZ_DIR}/"
            f"（共 {moved} 个，照样算缓存，不重复生成）")


def stage_plan(opts: Options) -> list[str]:
    """这次要跑哪几步（每一步至少有一个勾选产出时才在里面）。

    课件与口播视频合并成第一步：视频在后台 1 路排队，上一节合成视频时下一节的
    课件已经在生成，不用等——所以只在"笔记题库"前停一次。
    """
    plan = []
    if opts.gen_ppt or opts.gen_video or opts.gen_notes or opts.gen_quiz:
        plan.append(STAGE_PPT)          # 识别原文是所有产出的前提
    if opts.gen_notes or opts.gen_quiz:
        plan.append(STAGE_NOTES)
    return plan


def _stage_label(key: str, need_ppt: bool, gen_video: bool = False) -> str:
    if key == STAGE_PPT:
        if not need_ppt:
            return "第一步 识别教材原文"
        return "第一步 生成课件 + 口播视频" if gen_video else "第一步 生成 PPT 课件 + 口播稿"
    return "第二步 生成学习笔记 + 题库"


def _stage_short(key: str, gen_video: bool = False) -> str:
    if key == STAGE_PPT:
        return "课件+视频" if gen_video else "课件"
    return "笔记题库"


# ---------------------------------------------------------------- 主流水线
def run(llm: LLMClient, opts: Options, log=print, progress=None, cancel=None,
        gate: GateFn | None = None) -> list[Path]:
    """跑完整流水线；gate=None 为全自动。第一个阶段的闸门在上面一层（GUI）。"""
    t_all = time.monotonic()
    auto = gate is None        # 全自动没有闸门，视频可以一路压到第二步之后再收尾
    if gate is None:
        gate = _noop_gate
    opts = Options(**{**vars(opts), "pdf_path": Path(opts.pdf_path),
                      "out_dir": Path(opts.out_dir)})
    book = opts.pdf_path.stem
    book_dir = book_dir_for(opts.out_dir, opts.pdf_path)
    book_dir.mkdir(parents=True, exist_ok=True)
    tmpdir = book_dir / ".pages"

    # ---- PPT 风格：手动指定就用指定值；"auto" 按书名让 AI 选一次（缓存，全书统一）
    theme = opts.theme
    if theme == slidegen.AUTO_STYLE and (opts.gen_ppt or opts.gen_video):
        theme = slidegen.resolve_style(llm, book, book_dir, log=log)
    elif theme not in slidegen.THEMES:
        theme = slidegen.DEFAULT_STYLE
    outputs: list[Path] = []
    vpool: ThreadPoolExecutor | None = None      # 视频后台队列，出错时也要在 finally 里停掉
    vcancel: _AnyCancel | None = None

    def add_out(*paths):
        for p in paths:
            p = Path(p)
            if p not in outputs:
                outputs.append(p)

    doc = textbook.open_pdf(opts.pdf_path)
    try:
        # ---- 切分：按每节页数 / 按教材章节
        total = textbook.page_count(doc)
        per = max(1, int(opts.pages_per_lesson or textbook.DEFAULT_PAGES_PER_LESSON))
        mode = (opts.split_mode or SPLIT_PAGES).lower()
        if mode == SPLIT_CHAPTERS:
            chapters, src = textbook.ensure_chapter_structure(llm, doc, book_dir, tmpdir, log)
            log(f"切分方式：按教材章节（来源 {src}）——{len(chapters)} 章，"
                f"节边界对齐目录小节、每节约 {per} 页、不跨章")
            lessons = textbook.split_chapters(chapters, per, min_pages=opts.min_lesson_pages)
        else:
            start = max(1, int(opts.start_page or 1))
            cached = textbook.load_chapter_structure(book_dir, total, log)
            chapters = cached[0] if cached else []
            if not chapters:
                log("  提示：输出目录里没有 章节结构.json，本次节标题不带章名"
                    "（想带章名先跑一次「按教材章节」，或手工编写该文件）")
            lessons = textbook.split_by_pages(total, start, per, chapters=chapters, log=log)
            if opts.min_lesson_pages > 1 and len(lessons) > 1:
                lessons = textbook._merge_short(lessons, int(opts.min_lesson_pages))
                log(f"  短节合并：不足 {opts.min_lesson_pages} 页的节并入相邻节"
                    f"（剩 {len(lessons)} 节）")
            log(f"切分方式：按每节 {per} 页 —— 第 {start}-{total} 页共 {len(lessons)} 节课")
        for c in lessons:
            log(f"  {c.title}：第 {c.start}-{c.end} 页")
        if not lessons:
            log("✘ 没有切出任何内容，请检查 PDF 页数与「起始页/每节页数」设置")
            return outputs

        states = []
        seen_titles: dict[str, int] = {}
        for ci, ch in enumerate(lessons, 1):
            st_dir = _chapter_dir(book_dir, ci, ch.title, log=log)
            st_dir.mkdir(parents=True, exist_ok=True)
            n = seen_titles[ch.title] = seen_titles.get(ch.title, 0) + 1
            states.append(ChapterState(ci, ch.title, ch.start, ch.end, st_dir,
                                       base=ch.title if n == 1 else f"{ch.title}_{n}"))

        # ---- 进度模型：识别每页 + 课件 12 + 视频 14（后台排队）+ 笔记/题库各 2
        need_ppt = opts.gen_ppt or opts.gen_video
        if need_ppt:
            _adopt_legacy(book_dir, states, log)
        per_ch = 12 if need_ppt else 0
        if opts.gen_video:
            per_ch += 14
        if opts.gen_notes:
            per_ch += 2
        if opts.gen_quiz:
            per_ch += 2
        total_units = float(sum(s.end - s.start + 1 for s in states) + len(states) * per_ch)
        done = [0.0]
        done_lock = threading.Lock()

        def tick(units: float, msg: str):
            with done_lock:
                done[0] += units
                frac = min(done[0] / total_units, 1.0) if total_units else 0.0
            if progress and total_units:
                progress(frac, msg)

        def skipped(st: ChapterState) -> bool:
            if st.md and len(st.md) < 200:
                return True
            if st.md:
                return False
            f = st.dir / "教材原文.md"
            if f.exists() and f.stat().st_size > 500:
                st.md = f.read_text(encoding="utf-8")
            if st.md and len(st.md) >= 200:
                return False
            log(f"  ✘ {st.title}：识别内容过少，跳过（可检查该页扫描件质量）")
            if not st.md:
                st.md = "\x00"       # 本次运行不再重试该节
            return True

        # ---- 产物就绪（缺则自动补做前面步骤，实现"环环相扣"）
        def ensure_md(st: ChapterState):
            if not st.md:
                target = st.dir / "教材原文.md"
                rng = st.dir / ".range.json"
                if target.exists() and target.stat().st_size > 500:
                    old = _read_range(rng)
                    if old is not None and old != (st.start, st.end):
                        # 切分方式/每节页数/章节结构改了导致页码范围变化：旧识别稿不可复用，重做
                        log(f"  {st.title}：页码范围已变（旧 {old[0]}-{old[1]} → 本次 "
                            f"{st.start}-{st.end}），重新识别教材原文")
                    else:
                        st.md = target.read_text(encoding="utf-8")
                        if old is None:
                            _write_range(rng, st.start, st.end)   # 旧版产物补登范围
                        log(f"  {st.title}：已有 教材原文.md（要重新识别就删掉它）")
                        if not st.ticked_md:
                            tick(st.end - st.start + 1, "跳过识别")
                if not st.md:
                    log(f"  {st.title}：识别第 {st.start}-{st.end} 页…")
                    try:
                        st.md = textbook.ocr_chapter(
                            llm, doc, st.start, st.end, tmpdir,
                            log=lambda s: (log(s), tick(1, s.strip())),
                            workers=opts.workers, cancel=cancel, raw_dir=st.dir)
                    except textbook.Cancelled:
                        raise Cancelled() from None
                    target.write_text(st.md, encoding="utf-8")
                    _write_range(rng, st.start, st.end)
                st.ticked_md = True
            if st.md and not st.sliced:
                st.sliced = True              # 只切一次；教材原文.md 保持全文不动
                if len(st.md) >= 200:
                    st.md, st.ex = textbook.split_exercises(st.md)
                    if st.ex:
                        log(f"  {st.title}：识别到课后习题/思考题（{len(st.ex)} 字），"
                            f"单独成文放 {EX_DIR}/，不进课件与笔记")
            return None if skipped(st) else st.md

        def _formula_fallback(st: ChapterState):
            """公式兜底：本地渲不出时先请 AI 改写重渲，再从教材原页裁图（出问题不影响流程）。"""
            try:
                return formula_fallback.make_fallback(llm, doc, (st.start, st.end),
                                                      st.dir / ".eq_cache", tmpdir, log=log)
            except Exception as exc:                # noqa: BLE001
                log(f"  ⚠ 公式兜底不可用（{type(exc).__name__}: {exc}），"
                    "渲不出的公式仍降级为可读文字")
                return None

        def ensure_ppt(st: ChapterState) -> Path | None:
            if ensure_md(st) is None:
                return None
            if st.pptx is not None:
                return st.pptx
            script = st.dir / "口播稿.txt"
            target = _ppt_path(book_dir, st)
            if target.exists() and script.exists():
                st.pptx = target
                add_out(target, script)
                log(f"  {st.title}：已有课件与口播稿（要重新生成就删掉它们）")
                if not st.ticked_ppt:
                    tick(12, "跳过课件")
                st.ticked_ppt = True
                return st.pptx
            log(f"  {st.title}：生成课件内容（目标 {opts.target_minutes:g} 分钟）…")
            tick(4, "生成课件内容")
            lesson = gen_lesson(llm, st.md, st.title, book,
                                opts.target_minutes, theme,
                                workers=opts.workers, raw_dir=st.dir)
            _check(cancel)
            log(f"  大纲：{'；'.join(s.title for s in lesson.sections)}")
            n_pages = sum(len(s.slides) for s in lesson.sections)
            est_min = n_pages * MIN_PER_SLIDE
            log(f"  共 {n_pages} 页内容页，预计讲课时长约 {est_min:.0f} 分钟")
            if not (opts.target_minutes * 0.7 <= est_min <= opts.target_minutes * 1.45):
                log(f"  ⚠ 与目标 {opts.target_minutes:g} 分钟偏差较大，"
                    "可在生成后手动增删幻灯片页")
            pptx_path = _ppt_path(book_dir, st)
            pptx_path.parent.mkdir(parents=True, exist_ok=True)
            engine = slidegen.BuiltinEngine(st.dir / ".eq_cache",
                                            formula_fallback=_formula_fallback(st))
            _doc_or_report("课件", st.dir, lesson,
                           lambda: engine.build(lesson, pptx_path), log)
            if engine.formula_rescued:
                log(f"  ✔ {len(engine.formula_rescued)} 处公式本地渲不出，"
                    "已用兜底图补上（AI 改写重渲或原书截图）")
            if engine.failed_formulas:
                log(f"  ⚠ {len(engine.failed_formulas)} 处公式渲染失败，已降级为可读文字，"
                    f"如：{engine.failed_formulas[0][:120]}")
                _dump_failed_formulas(st.dir, engine.failed_formulas, log)
            for w in engine.warnings:
                log(f"  ⚠ {w}")
            with script.open("w", encoding="utf-8") as f:
                for si, s in enumerate((s for sec in lesson.sections for s in sec.slides), 1):
                    f.write(f"[第{si}页] {s.title}\n{s.script}\n\n")
            add_out(pptx_path, script)
            log(f"  ✔ 课件：{pptx_path.name}")
            tick(8, "构建 PPT")
            st.pptx = pptx_path
            st.ticked_ppt = True
            return pptx_path

        def ensure_exercises(st: ChapterState) -> Path | None:
            """课后习题单独出 Word（内容来自识别稿，不额外花钱；主线程跑公式渲染）。"""
            if not st.ex:
                return None
            target = _ex_path(book_dir, st)
            if target.exists() and target.stat().st_size > 0:
                log(f"  {st.title}：已有课后习题（要重新生成就删掉它）")
                add_out(target)
                return target
            target.parent.mkdir(parents=True, exist_ok=True)
            _doc_or_report("课后习题", st.dir, st.ex,
                           lambda: docgen.markdown_to_docx(
                               st.ex, target, book, formula_dir=st.dir / ".eq_cache",
                               title=f"{st.title} 课后习题"), log)
            add_out(target)
            log(f"  ✔ 课后习题：{target.name}")
            return target

        # ---- 视频后台队列（1 路）：课件一就绪就排队合成，主线程继续做下一节
        # 全自动模式下不在第一步收尾：剩下的视频与第二步的笔记/题库并行跑，最后统一 drain。
        # 分步确认模式必须在闸门前收完，否则用户点"就到这里"时后台还在往输出目录写文件。
        # 并行是安全的：视频那条线只有 COM 导出子进程 + TTS 网络 + ffmpeg，不碰 matplotlib，
        # 而公式渲染（非线程安全）只发生在主线程建 pptx / docx 的时候。
        plan = stage_plan(opts)
        defer_video = auto and plan[:1] == [STAGE_PPT] and STAGE_NOTES in plan
        if opts.gen_video:
            vpool = ThreadPoolExecutor(1)
            vcancel = _AnyCancel(cancel)
        vfuts: list[tuple[ChapterState, object]] = []
        vids: list[Path] = []
        vdrained = False

        def drain_videos() -> float:
            """收尾视频队列，返回等待耗时（秒）。重复调用无副作用。"""
            nonlocal vdrained
            if vpool is None or vdrained:
                return 0.0
            vdrained = True
            t0 = time.monotonic()
            try:
                if vfuts and not defer_video:
                    log(f"  等待 {len(vfuts)} 个口播视频收尾…")
                for _st, fut in vfuts:
                    _check(cancel)
                    vids.extend(fut.result())
            finally:
                vpool.shutdown(wait=False, cancel_futures=True)
            return time.monotonic() - t0

        def video_for(st: ChapterState, pptx: Path) -> list[Path]:
            """后台 1 路：课件一就绪就排队合成视频，同时主线程继续做下一节的课件。"""
            t0 = time.monotonic()
            target = _video_path(book_dir, st)
            if target.exists() and target.stat().st_size > 0:
                log(f"  [视频·{st.idx}] 已有口播视频（要重新生成就删掉它）")
                tick(14, "跳过视频")
                add_out(target)
                return [target]
            log(f"  [视频·{st.idx}] 开始合成口播视频…")
            v_settings = pipeline.Settings(**{**vars(opts.video),
                                              "text_source": "备注",
                                              "workers": opts.workers})
            pl = pipeline.Pipeline(
                v_settings, log=lambda s: log(f"  [视频·{st.idx}] {s.strip()}"),
                progress=lambda frac, msg: tick(14 * frac, f"[视频·{st.idx}] {msg}"))
            got = pl.process_file(pptx, "", target.parent, cancel=vcancel,
                                  out_base=target.stem)
            add_out(*got)
            log(f"  [视频·{st.idx}] 完成（{_fmt_dur(time.monotonic() - t0)}）")
            return got

        def step_ppt() -> str:
            log(f"════ {_stage_label(STAGE_PPT, need_ppt, opts.gen_video)} ════")
            for st in states:
                _check(cancel)
                t0 = time.monotonic()
                log(f"  — {st.idx}/{len(states)} {st.title} —")
                if need_ppt:
                    pptx = ensure_ppt(st)
                else:
                    ensure_md(st)
                    pptx = None
                ensure_exercises(st)
                log(f"  本节用时 {_fmt_dur(time.monotonic() - t0)}")
                if vpool is not None:
                    if pptx is not None:
                        vfuts.append((st, vpool.submit(video_for, st, pptx)))
                    else:
                        log("  ✘ 无课件，跳过本节视频")
            if not defer_video:
                drain_videos()
            elif vfuts:
                log(f"  {len(vfuts)} 个口播视频继续在后台合成，同时开始第二步"
                    "（视频那条线不碰公式渲染，两边不打架）")
            names = [st.title for st in states if st.md]
            if need_ppt:
                ok = [st.title for st in states if st.pptx is not None]
                detail = (f"课件与口播稿就绪：{_short_list(ok)}" if ok
                          else "本批没有成功生成任何课件")
            else:
                detail = f"教材原文已识别：{_short_list(names)}"
            if opts.gen_video:
                if vids:
                    detail += f"；口播视频已生成 {len(vids)} 个"
                elif defer_video and vfuts:
                    detail += f"；{len(vfuts)} 个口播视频在后台合成中（收尾时汇总）"
                else:
                    detail += "；没有生成视频"
            log(f"  {detail}（输出目录：{book_dir}）")
            return detail

        def step_notes() -> str:
            log(f"════ {_stage_label(STAGE_NOTES, True, opts.gen_video)} ════")
            _adopt_legacy_docs(book_dir, states, log)
            made: list[str] = []
            for st in states:
                _check(cancel)
                t0 = time.monotonic()
                log(f"  — {st.idx}/{len(states)} {st.title} —")
                if ensure_md(st) is None:      # 缺教材原文时自动补识别
                    continue
                note = _note_path(book_dir, st)
                quiz = _quiz_path(book_dir, st)
                need_note = opts.gen_notes and not note.exists()
                need_quiz = opts.gen_quiz and not quiz.exists()

                # 两路 LLM 调用并发（docx 构建留主线程：matplotlib 公式渲染非线程安全）
                note_md = ""
                quiz_qs: list[dict] = []

                def fetch_note() -> str:
                    log("  生成学习笔记…")
                    return gen_notes_md(llm, st.md, st.dir)

                def fetch_quiz() -> list[dict]:
                    log(f"  生成题库（{opts.quiz_count} 题）…")
                    return gen_questions(llm, st.md, opts.quiz_count, raw_dir=st.dir)

                if need_note and need_quiz and (opts.workers or 1) > 1:
                    pool = ThreadPoolExecutor(2)
                    try:
                        f_note, f_quiz = pool.submit(fetch_note), pool.submit(fetch_quiz)
                        note_md, quiz_qs = f_note.result(), f_quiz.result()
                    finally:
                        pool.shutdown(wait=False, cancel_futures=True)
                else:
                    if need_note:
                        note_md = fetch_note()
                    if need_quiz:
                        quiz_qs = fetch_quiz()
                _check(cancel)

                if opts.gen_notes:
                    if note.exists():
                        log("  已有学习笔记（要重新生成就删掉它）")
                        tick(2, "跳过学习笔记")
                    else:
                        note.parent.mkdir(parents=True, exist_ok=True)
                        note = _doc_or_report("学习笔记", st.dir, note_md,
                                              lambda: docgen.markdown_to_docx(
                                                  note_md, note, book, st.title,
                                                  formula_dir=st.dir / ".eq_cache"), log)
                        tick(2, "学习笔记")
                    add_out(note)
                    if opts.export_pdf:
                        try:
                            add_out(docgen.docx_to_pdf(note))
                        except Exception as exc:            # noqa: BLE001
                            log(f"  ⚠ 笔记转 PDF 失败：{exc}")
                    made.append(note.name)
                if opts.gen_quiz:
                    if quiz.exists():
                        log("  已有题库（要重新生成就删掉它）")
                        tick(2, "跳过题库")
                    else:
                        quiz.parent.mkdir(parents=True, exist_ok=True)
                        quiz = _doc_or_report(
                            "题库", st.dir, quiz_qs,
                            lambda: docgen.questions_to_docx(
                                quiz_qs, quiz, book, st.title,
                                with_answers=True, formula_dir=st.dir / ".eq_cache"), log)
                        tick(2, "题库")
                    add_out(quiz)
                    if opts.export_pdf:
                        try:
                            add_out(docgen.docx_to_pdf(quiz))
                        except Exception as exc:            # noqa: BLE001
                            log(f"  ⚠ 题库转 PDF 失败：{exc}")
                    made.append(quiz.name)
                log(f"  本节用时 {_fmt_dur(time.monotonic() - t0)}")
            detail = (f"学习资料已生成 {len(made)} 份：{_short_list(made)}" if made
                      else "本批没有生成任何学习资料")
            log(f"  {detail}")
            return detail

        # ---- 依次执行 + 阶段间闸门（plan 已在视频队列那里算好）
        if not plan:
            log("没有勾选任何产出内容，未做任何事")
            return outputs
        steps = {STAGE_PPT: step_ppt, STAGE_NOTES: step_notes}
        last_stage = None
        stage_times: dict[str, float] = {}
        for i, key in enumerate(plan):
            t_stage = time.monotonic()
            detail = steps[key]()
            stage_times[key] = time.monotonic() - t_stage
            last_stage = key
            if i + 1 < len(plan) and not gate(
                    _stage_label(plan[i + 1], need_ppt, opts.gen_video),
                    f"「{_stage_label(key, need_ppt, opts.gen_video)}」已完成：{detail}", book_dir):
                log("⏸ 选择到此为止，先停在这里；以后重跑会复用已有产物继续，不会重复花钱。")
                break

        vwait = drain_videos()
        if defer_video and vfuts:
            log(f"口播视频收尾：共 {len(vids)} 个，其中 {_fmt_dur(vwait)} 是第二步之后才等出来的"
                "（其余与笔记/题库并行完成）")

        shutil.rmtree(tmpdir, ignore_errors=True)
        if need_ppt and not opts.gen_ppt:
            pptxs = sorted((book_dir / PPT_DIR).glob("*.pptx"))
            if pptxs:
                log("提示：想单独出视频，就在「教材 → 课程」页只勾选「口播视频」——"
                    f"已有的 {len(pptxs)} 个课件会自动复用，不会重复生成 PPT。")

        if progress:
            progress(1.0, "完成" if last_stage == plan[-1]
                     else f"已停在 {_stage_label(last_stage, need_ppt, opts.gen_video)}")
        if last_stage == plan[-1]:
            log(f"全部完成，输出目录：{book_dir}")
        else:
            log(f"已停在「{_stage_label(last_stage, need_ppt, opts.gen_video)}」，输出目录：{book_dir}")
        if stage_times:
            spent = "、".join(f"{_stage_short(k, opts.gen_video)} {_fmt_dur(v)}"
                              for k, v in stage_times.items())
            if defer_video and vfuts:
                spent += f"、视频收尾 {_fmt_dur(vwait)}"
            log(f"总用时 {_fmt_dur(time.monotonic() - t_all)}（{spent}；请求并发 {opts.workers} 路）")
        return outputs
    finally:
        if vpool is not None:      # 出错/取消时先叫停在跑的那一节，再丢掉排队中的
            if vcancel is not None:
                vcancel.own.set()
            vpool.shutdown(wait=False, cancel_futures=True)
        doc.close()
