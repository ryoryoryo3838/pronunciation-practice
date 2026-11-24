"""Self-check of the two-pass analysis on CC BY 4.0 speechocean762 clips (see fixtures/ATTRIBUTION.md)."""

import os
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

os.environ["PRON_DATA_DIR"] = tempfile.mkdtemp()  # model readings are cached under the data dir
sys.path.insert(0, str(Path(__file__).parent.parent))
import analyze  # noqa: E402
import reference  # noqa: E402

F = Path(__file__).parent / "fixtures"
SCRIPT = "# slide notes are ignored\nIt was a beautiful day for a float.\nFollow the link below to register.\nIt was good for me.\n"

tmp = Path(tempfile.mkdtemp())
clips = [analyze.load_audio(str(F / f"{n}.mp3")) for n in ("en-learner-1", "en-learner-2", "en-clean")]
sf.write(tmp / "talk.wav", np.concatenate(clips), analyze.SR)
sf.write(tmp / "noise.wav", np.random.default_rng(0).normal(0, 0.003, analyze.SR * 2).astype(np.float32), analyze.SR)
sf.write(tmp / "partial.wav", clips[0], analyze.SR)

r = analyze.analyze(str(tmp / "talk.wav"), SCRIPT)
assert [s["line"] for s in r["lines"]] == [1, 2, 3]
words = [w for s in r["lines"] for w in s["words"]]
assert [w["word_index"] for w in words] == list(range(len(words)))
assert words[0]["start_ms"] < words[-1]["start_ms"] <= r["duration_ms"]
# Line 2 starts after clip 1, so its timestamps must be offset into the full recording.
assert r["lines"][1]["words"][0]["start_ms"] > len(clips[0]) / analyze.SR * 1000 - 200
reg = next(w for w in words if w["word"] == "register")
assert reg["expected_stress_syllable"] == 1 and reg["estimated_stress_syllable"] == 2, reg
assert reg["expected_ipa"].startswith("ˈ")
assert reg["heard_ipa"] and reg["free_ipa"] and abs(sum(reg["stress_posterior"]) - 1) < 0.01
line = r["lines"][1]
assert line["contour_st"] and line["intonation"]["final_word"].startswith("register")
assert line["delivery"]["articulation_rate"] > 0 and "f0_declination" in line["delivery"]["features"]

for bad in ("noise.wav", "partial.wav"):
    try:
        analyze.analyze(str(tmp / bad), SCRIPT)
        raise AssertionError(f"{bad} should be rejected")
    except ValueError as e:
        assert "does not match the script" in str(e)
if reference.available():  # optional extra: model reading attached per line and per word
    ref = r["lines"][1]["reference"]
    assert r["reference_error"] is None and ref["prob"] > 0.8 and ref["words"][-1]["contour_rel"]
    assert reg["reference"]["tts_ipa"].startswith("ɹ")
print("ok")
