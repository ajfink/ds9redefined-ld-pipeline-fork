from fstk.quality import report


def _video(resid=0):
    return {"levels": {"clipping": [{"check": "crushed blacks (codes <= 1)", "value": 0.001, "limit": 0.02, "status": "PASS"}]},
            "dropouts": {"found": 5, "concealed": 4, "residual_count": resid,
                         "residual": [{"frame": 240, "reason": "too tall"}] * resid},
            "proof": {"issues": []}, "chroma_decision": {"measured_chroma_std": 1.2, "trigger": 2.5, "applied": None},
            "hybrid": {"status": "NOT NEEDED", "reason": "all film cadence"}}


def test_clean_episode_is_clean():
    rep = report.build("DS9:S01E20", "t", video=_video(), audio={"ld": {"events_found": 0, "repaired": 0}},
                       qc={"summary": {"pass": 9, "warn": 0, "fail": 0}}, fixes=[], duration_s=100.0, frames=2400)
    assert rep["verdict"] == "CLEAN"
    assert "Verdict: CLEAN" in report.render_markdown(rep)


def test_missing_stage_or_leftover_dropout_asks_for_review_and_failed_proof_fails():
    rep = report.build("e", "", video=_video(resid=1), audio=None, qc={"summary": {"fail": 0}}, fixes=[{"frame": 480}],
                       duration_s=100.0, frames=2400)
    assert rep["verdict"] == "REVIEW"
    assert [m["time_s"] for m in rep["moments"]] == [10.0, 20.0]          # 240/24 s residual, 480 fix
    bad = _video()
    bad["proof"]["issues"] = ["luma changed"]
    assert report.build("e", "", video=bad, audio=None, qc=None, fixes=[], duration_s=1, frames=24)["verdict"] == "FAIL"


def test_moments_are_spread_out_and_ranked():
    fixes = [{"frame": f} for f in (24, 30, 36, 240)]                    # 1.0 s, 1.25 s, 1.5 s, 10 s
    ms = report.pick_moments(None, None, None, fixes, duration_s=100.0, frames=2400)
    assert [m["time_s"] for m in ms] == [1.0, 10.0]


def test_operator_fixes_are_listed_and_smoothed_ones_flagged():
    ops = [{"first": 10, "last": 11, "action": "interpolate", "note": "dropout", "grain_ratio_vs_neighbour": 0.6,
            "grain_flag": True},
           {"first": 50, "last": 70, "action": "dvd", "rect": [0, 0, 64, 20], "note": "rot band"}]
    rep = report.build("e", "", video=_video(), audio={"ld": {}}, qc={"summary": {}}, fixes=[], duration_s=10,
                       frames=240, operator_fixes=ops)
    rows = [r for r in rep["rows"] if r["area"] == "Operator fixes"]
    assert [r["status"] for r in rows] == ["FLAG", "NOTE"] and "DVD" in rows[1]["detail"] and "64x20" in rows[1]["detail"]
