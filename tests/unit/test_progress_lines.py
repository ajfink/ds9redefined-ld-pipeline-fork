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


WARN = ("[orc-cli] [2026-11-23 13:27:34.198] [core] [warning] StackerStage: frames 500-999: the noise model does not "
        "explain the inputs (pair 2-8 misfits by 41%, 15% rms over 66 pair(s)), so they are stacked with equal weights")
REPORT = ('[orc-cli] [2026-11-23 13:40:47.106] [core] [info] StackerStage report: {"v":1,"kind":"weights",'
          '"frames":[3001,3500],"inputs":[{"input":1,"sigma":2.66,"share":9.3}]}')


def test_a_percentage_inside_a_log_message_is_not_progress():
    # Seen on the Windows run: this Orc warning put the render bar at "41%" an hour into a 7-hour render.
    assert run([("stderr-orc-cli", WARN)], label="Orc render") == []
    got = run([("stderr-orc-cli", "[orc-cli] [1/13] field_mapping on 'Frame Map P6':  20% Running disc mapper analysis...")])
    assert got[0][0] == 0.2


def test_orc_stacker_blocks_give_render_progress_when_the_total_is_known():
    got = run([("stderr-orc-cli", WARN), ("stderr-orc-cli", REPORT)], total_frames=83335, label="Orc render")
    assert len(got) == 1 and abs(got[0][0] - 3500 / 83335) < 1e-9
    assert got[0][1] == "Orc render frame 3500/83335"
    assert run([("stderr-orc-cli", REPORT)]) == []                      # without a total there is nothing to scale to


def test_a_restarted_percentage_is_followed_small_dips_are_not():
    from fstk.tools.progress_lines import LineProgress

    seen = []
    t = iter(range(100))
    lp = LineProgress(lambda f, m: seen.append(round(f, 2)), min_interval=0.0, clock=lambda: next(t))
    for pct in (41, 5, 65, 10, 15, 12, 60, 100):          # PC-A's Orc render, 2026-11-23
        lp("stderr", f"Processing: {pct}%")
    assert seen == [0.41, 0.05, 0.65, 0.10, 0.15, 0.15, 0.60, 1.0]
