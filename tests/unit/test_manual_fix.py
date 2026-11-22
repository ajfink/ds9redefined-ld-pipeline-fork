import pytest

from fstk.core.errors import ArtifactError
from fstk.media import manual_fix as mf

BASE = "import vapoursynth as vs\ncore = vs.core\nout = core.std.BlankClip(length=100)\nout.set_output()\n"
NOTE = "dropout band across the face"


def test_validation_rules():
    ok = [mf.Fix(10, 12, mf.INTERPOLATE, note=NOTE), mf.Fix(20, 60, mf.DVD, note=NOTE),
          mf.Fix(70, 70, mf.LD_PATCH, (100, 40, 64, 20), -1, NOTE), mf.Fix(80, 80, mf.LEAVE)]
    assert mf.validate(ok, frames=100) == []
    bad = mf.validate([mf.Fix(10, 14, mf.INTERPOLATE, note=NOTE)], frames=100)
    assert "limited to 3" in bad[0]
    assert "overlap" in mf.validate([mf.Fix(10, 12, mf.DVD, note=NOTE), mf.Fix(13, 14, mf.DVD, note=NOTE)], frames=100)[0]
    assert "both sides" in mf.validate([mf.Fix(0, 0, mf.DVD, note=NOTE)], frames=100)[0]
    assert "even" in mf.validate([mf.Fix(5, 5, mf.LD_PATCH, (1, 2, 4, 4), 1, NOTE)], frames=100)[0]
    assert "leaves" in mf.validate([mf.Fix(5, 5, mf.DVD, (700, 2, 40, 4), None, NOTE)], frames=100,
                                   width=720, height=480)[0]
    assert "8 characters" in mf.validate([mf.Fix(5, 5, mf.DVD)], frames=100)[0]


def test_round_trip():
    f = mf.Fix(70, 72, mf.LD_PATCH, (100, 40, 64, 20), 1, NOTE)
    assert mf.Fix.from_dict(f.as_dict()) == f


def test_script_with_no_fixes_is_the_base_plus_guard():
    t = mf.manualfix_script(BASE, fixes=[mf.Fix(5, 5, mf.LEAVE)], expected_frames=100)
    assert mf.manualfix_matches_base(t, BASE) and "MANUAL_FIXES" not in t
    compile(t, "m.vpy", "exec")


def test_script_with_every_action_compiles_and_needs_dvd_payload():
    fixes = [mf.Fix(10, 11, mf.INTERPOLATE, note=NOTE), mf.Fix(20, 22, mf.DVD, note=NOTE),
             mf.Fix(30, 30, mf.DVD, (10, 10, 20, 20), None, NOTE), mf.Fix(40, 44, mf.LD_PATCH, (0, 0, 8, 8), 1, NOTE)]
    with pytest.raises(ArtifactError):
        mf.manualfix_script(BASE, fixes=fixes, expected_frames=100)
    t = mf.manualfix_script(BASE, fixes=fixes, expected_frames=100, payload="p.mkv",
                            slots={20: 0, 21: 1, 22: 2, 30: 3})
    compile(t, "m.vpy", "exec")
    assert "FlowInter" in t and "MaskedMerge" in t and "DVD_SLOTS = {20: 0, 21: 1, 22: 2, 30: 3}" in t
    assert t.rstrip().endswith("out.set_output()") and t.count("out.set_output()") == 1
