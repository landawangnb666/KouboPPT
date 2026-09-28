"""端到端测试：样例 PPT → 口播视频（不经过 GUI）。"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# 控制台是 GBK 时打印 ✔/⚠ 会 UnicodeEncodeError，导致测试整个失败
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

from kouboppt import pipeline, video
from kouboppt.pipeline import Pipeline, Settings

OUT = Path(__file__).parent / "test_out"


def main():
    pptx = OUT / "样例课程_第1课.pptx"
    assert pptx.exists(), "先运行 make_test_pptx.py"

    settings = Settings(text_source="正文")   # 走最常用的路径
    logs = []

    pl = Pipeline(
        settings,
        log=lambda s: (logs.append(s), print("  [日志]", s)),
        progress=lambda f, m: print(f"  [进度 {f*100:5.1f}%] {m}"),
    )

    t0 = time.time()
    outputs = pl.process_file(pptx, "1-3, 4-6", OUT)   # 拆成两个视频
    dt = time.time() - t0

    print(f"\n耗时 {dt:.1f}s，生成 {len(outputs)} 个视频：")
    for o in outputs:
        assert o.exists() and o.stat().st_size > 10000, f"输出异常: {o}"
        # 用 ffmpeg 验证视频时长与可解码性
        dur = _video_duration(o)
        print(f"  ✔ {o.name}  {o.stat().st_size/1024/1024:.2f} MB  时长 {dur:.1f}s")

    print("\n=== 单元自测 ===")
    hw_concat()
    hw_lesson(pptx, outputs[0])
    assert pipeline.parse_ranges("1-18, 19-36, 5", 40) == [(1, 18), (19, 36), (5, 5)]
    assert pipeline.parse_ranges("", 40) is None
    assert pipeline.parse_ranges("3-1", 40) == [(1, 3)]
    assert pipeline.output_name("课程", None) == "课程.mp4"
    assert pipeline.output_name("课程", (1, 18)) == "课程_p1-18.mp4"
    assert pipeline.output_name("课程", (5, 5)) == "课程_p5.mp4"
    one = pl.process_file(pptx, "5", OUT, out_base="课时标题视频")   # 教材→课程靠它命名"标题+视频"
    assert [p.name for p in one] == ["课时标题视频_p5.mp4"], one
    assert pipeline.compute_export_size(12192000, 6858000) == (1920, 1080)
    assert pipeline.compute_export_size(9144000, 6858000)[1] == 1080   # 4:3 → 1440x1080
    assert ppt_text_join() is None
    try:
        pipeline.parse_ranges("1-99", 40)
        raise AssertionError("越界应报错")
    except ValueError:
        pass
    print("所有断言通过 ✔")
    print(f"\n测试结论：{len(outputs)} 个视频生成成功" )


def hw_concat():
    """硬编片段必须能 -c copy 无损拼接，且拼完能完整解码——整条视频链的前提。"""
    import shutil
    import subprocess
    tmp = OUT / "_enc_hw"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    img = tmp / "slide.png"
    video._probe_image(img)          # 带文字细节的 1080p 静帧，和真实幻灯片同类
    tested = 0
    for codec in (video.SW_CODEC, *video.HW_CODECS):
        segs = [tmp / f"seg{i}_{codec}.mp4" for i in (1, 2)]
        try:
            for seg in segs:
                video.make_segment(img, None, 3.0, seg, 1920, 1080, codec=codec)
        except video.EncoderFallback as exc:
            print(f"  ⚠ {video.ENCODER_LABELS[codec]} 本机不可用，跳过"
                  f"（{str(exc).splitlines()[0][:70]}）")
            continue
        out = tmp / f"join_{codec}.mp4"
        video.concat_segments(segs, out)
        dur = _video_duration(out)
        assert 5.5 <= dur <= 6.5, (codec, dur)
        dec = subprocess.run([video.FFMPEG, "-v", "error", "-i", str(out), "-f", "null", "-"],
                             capture_output=True, text=True, errors="replace")
        assert dec.returncode == 0 and not dec.stderr.strip(), (codec, dec.stderr[-300:])
        print(f"  ✔ {video.ENCODER_LABELS[codec]}：2 段 × 3s → 无损拼接 {dur:.1f}s，"
              f"解码无报错（{out.stat().st_size / 1024:.0f} KB）")
        tested += 1
    shutil.rmtree(tmp, ignore_errors=True)
    assert tested >= 1, "连软编都没测通"


def hw_lesson(pptx: Path, sw_video: Path):
    """整节课改用硬编跑一遍：时长要和软编一致；硬编真失败也得能自动回退出片。"""
    import shutil
    out = OUT / "_enc_hw_run"
    shutil.rmtree(out, ignore_errors=True)
    logs: list[str] = []
    Pipeline(Settings(text_source="正文", codec="h264_qsv", workers=2),
             log=logs.append).process_file(pptx, "1-3", out)
    got = sorted(out.glob("*.mp4"))
    assert len(got) == 1 and got[0].stat().st_size > 10000, got
    hw_dur, sw_dur = _video_duration(got[0]), _video_duration(sw_video)
    assert abs(hw_dur - sw_dur) < 0.5, (hw_dur, sw_dur)
    fell_back = any("改回 CPU 软编" in m for m in logs)
    print(f"  ✔ 整节用 Quick Sync 实跑：{hw_dur:.1f}s（软编 {sw_dur:.1f}s）"
          + ("，硬编失败已自动回退软编" if fell_back else "，全程硬编未回退"))
    shutil.rmtree(out, ignore_errors=True)


def _video_duration(path: Path) -> float:
    import subprocess
    from kouboppt.video import FFMPEG
    proc = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)],
                          capture_output=True, text=True, errors="replace")
    import re
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr)
    if not m:
        raise RuntimeError(f"无法解析视频时长: {path}\n{proc.stderr[-400:]}")
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])


def ppt_text_join():
    from kouboppt import ppt_text
    t = ppt_text.clean_for_tts("• 标题一\n• 第二点，注意逗号。\n  第三点\n")
    assert t == "标题一，第二点，注意逗号。第三点", repr(t)
    return None


if __name__ == "__main__":
    main()
