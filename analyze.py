"""Score a recording against its script with Pronunciada, keeping every piece of evidence it exposes."""

import json
import math
import os
import shutil
import subprocess
from importlib.metadata import version

import numpy as np
import pronunciada
import soundfile as sf

import reference
import store
from store import script_lines  # noqa: F401  (part of this module's interface for callers and tests)
from pronunciada import delivery_coach as DC
from pronunciada.audio import resample
from pronunciada.text import get_g2p
from pronunciada.tips import tips

SR = 16000
PAD_S = 0.15  # context kept around each line when cutting the long recording
MIN_COMPLETENESS = 0.5  # share of script words heard; below this the run is rejected, not scored
CONTOUR_STEP_S = 0.05
# shortcut: fixed ±2 st threshold for labelling a final rise/fall; calibrate on the user's own speech if it misleads.
FINAL_MOVE_ST = 2.0
# A larger jump inside one word is more likely an octave error of the pitch tracker than speech.
MAX_WORD_MOVE_ST = 12.0
# If the model reading itself scores below this, the dictionary's expected form is suspect (e.g. a
# noun/verb homograph such as "present" given the other part of speech), so the user's word is not judged.
REF_DISAGREE = 0.5


def load_audio(path: str) -> np.ndarray:
    try:
        x, sr = sf.read(path, dtype="float32", always_2d=True)
    except (ValueError, sf.LibsndfileError):
        # Streamed FLACs can carry a bogus total-frame count that libsndfile cannot read; ffmpeg can.
        if not shutil.which("ffmpeg"):
            raise
        raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "f32le", "-ac", "1", "-ar", str(SR), "-"],
                             capture_output=True, check=True).stdout
        return np.frombuffer(raw, dtype=np.float32).copy()
    x = x.mean(axis=1)
    return resample(x, sr, SR).astype(np.float32) if sr != SR else x


def _r(v, n=3):
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else round(float(v), n)


def _st(hz):
    """Semitones re 100 Hz."""
    return 12 * np.log2(np.asarray(hz) / 100.0)


def _expected_ipa(ref, wi: int) -> str:
    out = []
    for si in range(*ref.words[wi].syllables):
        s = ref.syllables[si]
        out.append({1: "ˈ", 2: "ˌ"}.get(s.stress, "") + "".join(ref.phones[pi].ipa for pi in range(*s.phones)))
    return "".join(out)


def _z(vals: list[float | None]) -> list[float | None]:
    xs = [v for v in vals if v is not None]
    if len(xs) < 3 or np.std(xs) == 0:
        return [None] * len(vals)
    m, sd = float(np.mean(xs)), float(np.std(xs))
    return [None if v is None else round((v - m) / sd, 2) for v in vals]


class _Pitch:
    """Window queries on Pronunciada's own F0/energy track (10 ms frames, segment-relative seconds)."""

    def __init__(self, trk):
        self.t, self.f0, self.en = trk.times, np.asarray(trk.f0), np.asarray(trk.energy_db)
        self.v = np.asarray(trk.voiced, bool) & (self.f0 > 0)

    def st(self, a, b):
        m = (self.t >= a) & (self.t <= b) & self.v
        return _st(self.f0[m]) if m.any() else np.array([])

    def energy_max(self, a, b):
        m = (self.t >= a) & (self.t <= b)
        return float(self.en[m].max()) if m.any() else None

    def word(self, a, b) -> dict:
        s = self.st(a, b)
        k = min(3, len(s))
        return {"f0_mean_st": _r(s.mean(), 2) if len(s) else None, "f0_max_st": _r(s.max(), 2) if len(s) else None,
                # start-to-end pitch movement inside the word, medians of the first/last voiced frames
                "f0_movement_st": _r(np.median(s[-k:]) - np.median(s[:k]), 2) if len(s) >= 4 else None,
                "energy_max_db": _r(self.energy_max(a, b), 1)}

    def rel_contour(self, a, b, med) -> list:
        """Pitch inside one word as [position 0-1, semitones re the line median], comparable across speakers."""
        out, n = [], max(2, round((b - a) / CONTOUR_STEP_S))
        for i in range(n):
            s = self.st(a + (b - a) * i / n, a + (b - a) * (i + 1) / n)
            out.append([round((i + 0.5) / n, 2), _r(np.median(s) - med, 2) if len(s) else None])
        return out

    def contour(self, a, b, offset_s) -> list:
        out = []
        for t in np.arange(a, b, CONTOUR_STEP_S):
            s = self.st(t, t + CONTOUR_STEP_S)
            out.append([round((t + offset_s) * 1000), _r(np.median(s), 2) if len(s) else None])
        return out


def _score_line(seg: np.ndarray, text: str, offset_s: float, line_no: int) -> dict:
    r = pronunciada.score((seg, SR), text, lang="en")
    c = r._pronunciada  # cached evidence; the public coaching API reads the same object
    ref, ev = c["ref"], c["ev"]
    ms = lambda t: round((t + offset_s) * 1000)  # noqa: E731
    oov = {w.removeprefix("oov:") for w in ref.warnings if w.startswith("oov:")}
    is_oov = lambda word: word.lower().strip(".,;:!?\"'()") in oov  # noqa: E731
    # The expected sounds of out-of-dictionary words (names, acronyms) are guessed, so errors against them are not evidence.
    coaching = [i for i in r.coaching(delivery="all") if not is_oov(i["word"] or "")]
    pitch = _Pitch(c["f0"])

    # Unconstrained phone recognition (what was heard regardless of the script), assigned to words by time.
    spans = DC.word_spans(c)
    free = [(tok, a, b) for tok, a, b in (ev.free_tokens or [])]
    free_by_word = [[tok for tok, a, b in free if sp and sp[0] - 1 <= (a + b) / 2 < sp[1] + 1] for sp in spans]

    sylls_all = [(wi, s) for wi, w in enumerate(r.words) for s in w.syllables]
    f0s = [pitch.st(s.start, s.end) for _, s in sylls_all]
    f0z = _z([float(f.mean()) if len(f) else None for f in f0s])
    enz = _z([pitch.energy_max(s.start, s.end) for _, s in sylls_all])
    duz = _z([s.end - s.start for _, s in sylls_all])

    words, k = [], 0
    for wi, w in enumerate(r.words):
        rw = ref.words[wi]
        posterior = next((c["prosody"][si]["stress_dist"] for si in range(*rw.syllables)
                          if si < len(c["prosody"]) and "stress_dist" in c["prosody"][si]), None)
        sylls = []
        for s in w.syllables:
            sylls.append({"start_ms": ms(s.start), "end_ms": ms(s.end), "stress_expected": s.stress_expected,
                          "stress_prob": _r(s.stress_prob), "f0_z": f0z[k], "energy_z": enz[k], "duration_z": duz[k]})
            k += 1
        st_item = next((i for i in coaching if i["kind"] == "stress" and i["word_index"] == wi), None)
        warnings = ["not in dictionary: expected pronunciation is a guess"] if is_oov(w.text) else []
        if st_item is None and w.stress_prob is not None and w.stress_prob < 0.5:
            warnings.append("stress not reliably determined")
        slots = ev.slots[rw.phones[0]:rw.phones[1]]
        words.append({
            "word_index": wi,
            "word": w.text,
            "line": line_no,
            "start_ms": ms(w.start),
            "end_ms": ms(w.end),
            "prob": _r(w.prob),
            "sounds_prob": _r(w.sounds_prob),
            "present_prob": _r(w.present_prob),
            "omitted": w.omitted,
            "length_prob": _r(w.length_prob),
            "contrast_prob": _r(w.contrast_prob),
            "expected_ipa": _expected_ipa(ref, wi),
            # what the aligner judged each expected sound to be, in script order ("" = not heard)
            "heard_ipa": "".join(s.heard or "" for s in slots),
            "free_ipa": "".join(free_by_word[wi]),
            "expected_stress_syllable": next((i + 1 for i, s in enumerate(w.syllables) if s.stress_expected == 1), None),
            # Only Pronunciada's stress detector names a heard position; otherwise unknown, not "correct".
            "estimated_stress_syllable": st_item["heard"] + 1 if st_item and st_item["heard"] is not None else None,
            "stress_confidence": st_item["confidence"] if st_item else None,
            "stress_prob": _r(w.stress_prob),
            "stress_posterior": [_r(p) for p in posterior] if posterior else None,
            **pitch.word(w.start, w.end),
            "phones": [{"ipa": p.ipa, "heard": s.heard, "prob": _r(p.prob), "start_ms": ms(p.start), "end_ms": ms(p.end),
                        "length_expected": p.length_expected, "length_prob": _r(p.length_prob),
                        "evidence": {k2: _r(v) for k2, v in p.features.items()}} for p, s in zip(w.phones, slots)],
            "syllables": sylls,
            "warnings": warnings,
        })

    d_in = DC.delivery_inputs(c)
    voiced = [w for w in words if w["f0_max_st"] is not None]
    for w in words:
        if w["f0_movement_st"] is not None and abs(w["f0_movement_st"]) > MAX_WORD_MOVE_ST:
            w["warnings"].append("pitch jump over 12 st: possible pitch-tracking error")
            w["f0_movement_st"] = None
    last = words[-1]
    move = last["f0_movement_st"]
    s_all = pitch.st(r.words[0].start, r.words[-1].end)
    med = float(np.median(s_all)) if len(s_all) else None
    for w, rw in zip(words, r.words):
        w["f0_rel_st"] = _r(w["f0_mean_st"] - med, 2) if med is not None and w["f0_mean_st"] is not None else None
        w["contour_rel"] = pitch.rel_contour(rw.start, rw.end, med) if med is not None else []
    return {
        "line": line_no,
        "text": text,
        "start_ms": ms(r.words[0].start),
        "end_ms": ms(r.words[-1].end),
        "prob": _r(r.prob),
        "scores": {k2: _r(v) for k2, v in r.scores.items()},
        "fluency": _r(r.fluency),
        "completeness": _r(r.completeness),
        "free_ipa": " ".join("".join(f) or "∅" for f in free_by_word),
        "intonation": {
            "f0_median_hz": _r(100 * 2 ** (np.median(s_all) / 12), 1) if len(s_all) else None,
            "f0_range_st": _r(d_in["f0_range_st"], 2),
            "final_word": last["word"],
            "final_movement_st": move,
            "final_contour": None if move is None else "rise" if move >= FINAL_MOVE_ST else
                             "fall" if move <= -FINAL_MOVE_ST else "level",
            "pitch_peak_word": max(voiced, key=lambda w: w["f0_max_st"])["word"] if voiced else None,
            # semitones re this line's median, so the shape compares with the model reading's
            "word_f0_rel_st": [[w["word"], w["f0_rel_st"]] for w in words],
        },
        "contour_st": pitch.contour(r.words[0].start, r.words[-1].end, offset_s),
        "delivery": {
            "articulation_rate": _r(d_in["articulation_rate"], 2),
            "pauses": [{"start_ms": ms(p["start"]), "duration_ms": round(p["duration"] * 1000),
                        "after_word": r.words[p["after_word"]].text, "before_word": r.words[p["before_word"]].text,
                        "in_word": p["in_word"], "punctuation": p["punctuation"]} for p in d_in["pauses"]],
            "punctuation_gaps": [{"after_word": r.words[m["after_word"]].text, "kind": m["kind"],
                                  "gap_ms": round(m["gap"] * 1000)} for m in d_in["marks"]],
            "extra_speech": [{"start_ms": ms(e["start"]), "end_ms": ms(e["end"]), "heard_ipa": e["heard_ipa"],
                              "restart": e["restart"]} for e in DC.extra_segments(c)],
            # Pronunciada's raw rhythm/pitch/energy descriptors (names as in pronunciada.delivery)
            "features": {k2: _r(v) for k2, v in (c.get("delivery") or {}).items() if not k2.startswith("emb_")},
        },
        "audio": r.audio,
        "coaching": [{k2: v for k2, v in i.items() if k2 not in ("schema_version", "schema_minor")} | {
                     "start_ms": ms(i["start"]) if i.get("start") is not None else None,
                     "end_ms": ms(i["end"]) if i.get("end") is not None else None} for i in coaching],
        "tips": [{"text": t["text"], "word": t["item"]["word"], "kind": t["item"]["kind"],
                  "start_ms": ms(t["item"]["start"]) if t["item"].get("start") is not None else None}
                 for t in tips(coaching, max_tips=len(coaching) or 1, result=r)],
        "oov_words": sorted(oov),
        "words": words,
        "warnings": r.warnings,
    }


def analyze(audio_path: str, script: str) -> dict:
    lines = script_lines(script)
    if not lines:
        raise ValueError("script has no lines")
    x = load_audio(audio_path)
    if len(x) < SR * 0.3 or float(np.abs(x).max()) < 1e-4:
        raise ValueError("recording is empty or silent")

    g2p = get_g2p("en")
    counts = [len(g2p(s).words) for s in lines]
    # Pass 1 aligns the whole script to find line boundaries; scores are calibrated per sentence, so
    # pass 2 rescores each line on its own (a long single pass lowers every probability).
    whole = pronunciada.score((x, SR), " ".join(lines), lang="en")
    # Without this, room noise or a different script still gets aligned and scored (~0.5 total).
    audio = whole.audio or {}
    if audio.get("retry_recommended") or (whole.completeness or 0) < MIN_COMPLETENESS:
        raise ValueError(f"recording does not match the script (completeness {whole.completeness:.2f}, "
                         f"audio issues {audio.get('issues', [])})")
    if len(whole.words) != sum(counts):
        raise RuntimeError(f"word count mismatch: {len(whole.words)} aligned vs {sum(counts)} expected")
    bounds, i = [], 0
    for n in counts:
        bounds.append((whole.words[i].start, whole.words[i + n - 1].end))
        i += n
    cuts = [0.0] + [(bounds[j][1] + bounds[j + 1][0]) / 2 for j in range(len(bounds) - 1)] + [len(x) / SR]

    segments = []
    for j, text in enumerate(lines):
        a = max(cuts[j], bounds[j][0] - PAD_S) if j else 0.0
        b = min(cuts[j + 1], bounds[j][1] + PAD_S) if j < len(lines) - 1 else cuts[-1]
        segments.append(_score_line(x[math.floor(a * SR):math.ceil(b * SR)], text, a, j + 1))

    ref_error = _attach_references(segments) if reference.available() else "not installed (uv sync --extra reference)"
    words = [w for s in segments for w in s["words"]]
    for n, w in enumerate(words):
        w["word_index"] = n
    hz = [s["intonation"]["f0_median_hz"] for s in segments if s["intonation"]["f0_median_hz"]]
    return {
        "duration_ms": round(len(x) / SR * 1000),
        "overall": {k: round(float(np.mean([s["scores"][k] for s in segments if k in s["scores"]])), 3)
                    for k in ("accuracy", "fluency", "prosodic", "total")},
        "speaker": {"f0_median_hz": _r(np.median(hz), 1) if hz else None,
                    "articulation_rate": _r(np.mean([s["delivery"]["articulation_rate"] for s in segments]), 2)},
        "lines": segments,
        "reference_error": ref_error,
        "engine_versions": {"pronunciada": version("pronunciada"),
                            **({"kokoro-onnx": version("kokoro-onnx"), "tts_voice": reference.VOICE}
                               if reference.available() else {})},
    }


def reference_line(text: str, line_no: int) -> dict:
    """The model reading of one line, scored like the user's; cached as JSON next to its WAV."""
    cache = store.DATA / "reference" / f"{reference.key(text)}.json"
    if cache.exists():
        out = json.loads(cache.read_text(encoding="utf-8"))
        if out["words"] and "start_ms" in out["words"][0]:  # older caches lack word times; rebuild those
            return {**out, "line": line_no}
    s = _score_line(load_audio(str(reference.line_audio(text))), text, 0.0, line_no)
    it = s["intonation"]
    ph = reference.phonemes(text)
    # espeak-ng writes one token per word; only trust the pairing when the counts agree.
    tts_words = [t.strip(".,;:!?\"'()") for t in ph.split()]
    tts_words = tts_words if len(tts_words) == len(s["words"]) else [None] * len(s["words"])
    out = {
        "voice": reference.VOICE,
        "phonemes": ph,
        "prob": s["prob"],
        "final_contour": it["final_contour"],
        "final_movement_st": it["final_movement_st"],
        "pitch_peak_word": it["pitch_peak_word"],
        "articulation_rate": s["delivery"]["articulation_rate"],
        "pauses": s["delivery"]["pauses"],
        "words": [{k: w[k] for k in ("word", "start_ms", "end_ms", "prob", "expected_ipa", "heard_ipa", "f0_rel_st",
                                     "f0_movement_st", "contour_rel", "stress_posterior", "warnings")} | {"tts_ipa": t}
                  for w, t in zip(s["words"], tts_words)],
    }
    tmp = cache.with_name(f"{cache.stem}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    tmp.replace(cache)
    return {**out, "line": line_no}


def _attach_references(segments: list[dict]) -> str | None:
    try:
        for seg in segments:
            ref = reference_line(seg["text"], seg["line"])
            seg["reference"] = ref
            for w, rw in zip(seg["words"], ref["words"], strict=True):
                w["reference"] = {k: rw.get(k) for k in ("tts_ipa", "start_ms", "end_ms", "prob", "heard_ipa",
                                                         "f0_rel_st", "f0_movement_st", "contour_rel")}
                guessed = any(x.startswith("not in dictionary") for x in w["warnings"])
                if rw["prob"] is not None and rw["prob"] < REF_DISAGREE and not guessed:
                    w["warnings"].append("model reading disagrees with the dictionary (homograph?): judgement withheld")
    except Exception as e:  # the model reading is an extra; never lose the user's analysis over it
        return f"{type(e).__name__}: {e}"
    return None
