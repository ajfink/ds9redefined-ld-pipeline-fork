from __future__ import annotations

from fstk.tools.progress_lines import LineProgress


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


def run(lines, **kw):
    got, clk = [], Clock()
    lp = LineProgress(lambda f, m: got.append((f, m)), clock=clk, **kw)
    for stream, line in lines:
        clk.t += 2
        lp(stream, line)
    return got


def test_vspipe_progress_needs_no_total():
    got = run([("vspipe#0", "Frame: 500/1000 (18.45 fps)")], label="encoding")
    assert got == [(0.5, "encoding frame 500/1000 · 18.45 fps")]


def test_ffmpeg_frame_and_time_and_out_time_us():
    got = run([("ffmpeg", "frame= 250 fps=25 q=-1.0 size=N/A time=00:00:10.00 bitrate=N/A")], total_frames=1000)
    assert got[0][0] == 0.25 and "frame 250/1000" in got[0][1]
    got = run([("ffmpeg", "size=N/A time=00:01:00.00 bitrate=N/A")], total_seconds=120)
    assert got[0][0] == 0.5 and got[0][1].startswith("0:01:00 of 0:02:00")
    got = run([("stdout", "out_time_us=30000000")], total_seconds=120)
    assert got[0][0] == 0.25
    assert run([("ffmpeg", "frame= 250 fps=25")]) == []            # no total: nothing invented


def test_percent_span_base_throttle_and_never_backwards():
    got = run([("stderr", "render 10%"), ("stderr", "render 40 %"), ("stderr", "nothing here"),
               ("stderr", "render 30%")], base=0.1, span=0.8)
    assert [round(f, 3) for f, _ in got] == [0.18, 0.42, 0.42]     # 30% after 40% does not move the bar back
    got2 = []
    clk = Clock()
    lp = LineProgress(lambda f, m: got2.append(f), clock=clk, min_interval=5)
    for p in (10, 20, 30):
        clk.t += 1
        lp("e", f"{p}%")
    assert len(got2) == 1                                           # throttled
    assert run([("e", "version 1.2.3 build 100")]) == []            # not a percentage
