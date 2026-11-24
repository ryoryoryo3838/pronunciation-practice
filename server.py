"""Optional MCP over the local analysis store, so ChatGPT can coach from the measurements.

The local recorder app is the primary tool (recording, analysis, visualisation, retakes); this server
only lets an assistant read results and stage the next script. Audio and local paths never leave this machine.
"""

import itertools
import json
import logging
import sys
import threading
import time

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

import reference
import store

log = logging.getLogger("pron-mcp")
mcp = MCPServer(
    "pronunciation-practice",
    instructions="Pronunciation analyses of the user's English talk rehearsals, measured locally. The user sees "
    "the visual analysis in their own recorder app; your job is coaching: pick at most three problems that "
    "matter most, quote the measured evidence, explain how to fix and practise each, and point to the line "
    "and word to compare and re-record in the app. Keep measured findings apart from your own interpretation "
    "(e.g. of intonation), and do not present null, guessed or low-confidence values as errors.",
)
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
# Writes a script file on the user's machine; earlier versions are kept, so nothing is destroyed.
WRITE_SCRIPT = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
# Increments per call so a voice answer can be matched to one specific log line, not a cached result.
_calls = itertools.count(1)
FLAG_PROB = 0.5


def _rows(sql: str, args=()) -> list:
    if not store.DB.exists():
        return []
    con = store.connect(readonly=True)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def _summary(row) -> dict:
    out = {k: row[k] for k in ("session_id", "slide_id", "reference_revision", "created_at")}
    out = {"recording_id": row["id"], "status": row["status"], **out}
    if row["status"] in ("recording", "analyzing"):
        return {**out, "status": "pending", "stage": row["status"]}
    if row["status"] == "failed":
        return {**out, "error": row["error"]}
    r = json.loads(row["result"])
    flagged = [
        {k: w[k] for k in ("word_index", "word", "line", "start_ms", "prob", "expected_ipa", "heard_ipa", "free_ipa",
                           "expected_stress_syllable", "estimated_stress_syllable", "stress_confidence",
                           "stress_posterior", "warnings")}
        for s in r["lines"] for w in s["words"]
        if (w["prob"] < FLAG_PROB or w["omitted"] or w["estimated_stress_syllable"] is not None)
        and not any(x.startswith(("not in dictionary", "model reading disagrees")) for x in w["warnings"])
    ]
    return {
        **out,
        "duration_ms": r["duration_ms"],
        "overall": r["overall"],
        "speaker": r["speaker"],
        "lines": [{**{k: s[k] for k in ("line", "text", "start_ms", "prob", "scores", "free_ipa", "intonation",
                                        "tips", "oov_words")},
                   "articulation_rate": s["delivery"]["articulation_rate"],
                   "reference": {k: (s.get("reference") or {}).get(k) for k in (
                       "voice", "phonemes", "final_contour", "final_movement_st", "pitch_peak_word",
                       "articulation_rate")} | {"word_f0_rel_st": [[w["word"], w["f0_rel_st"]] for w in
                                                                    (s.get("reference") or {}).get("words", [])]},
                   "pauses": s["delivery"]["pauses"],
                   "extra_speech": s["delivery"]["extra_speech"],
                   "audio_issues": (s["audio"] or {}).get("issues", [])} for s in r["lines"]],
        "flagged_words": sorted(flagged, key=lambda w: w["prob"]),
        "note": "scores are 0-1 probabilities that a rater would accept the speech (accuracy), not "
                "intelligibility. Words reported missing may have been paraphrased (e.g. an acronym read "
                "instead of the full form): ask the user before treating them as pronunciation errors. "
                "oov_words have guessed pronunciations and are excluded from flagged_words. "
                "free_ipa is unconstrained phone recognition (what was heard regardless of the script); "
                "heard_ipa is what each expected sound was aligned to. f0 values are semitones re 100 Hz; "
                "*_rel_st are re each speaker's line median. 'reference' is a local TTS model reading of the "
                "same line, scored the same way: one acceptable rendition, not the only correct intonation. "
                "Use get_line_detail for the pitch contour and rhythm features, get_word_detail for phones",
        "engine_versions": r["engine_versions"],
    }


@mcp.tool(annotations=READ_ONLY)
def get_latest_analysis(session_id: str | None = None) -> dict:
    """Latest recording and its analysis. Returns status 'pending' if the newest recording is not analysed yet."""
    call_no = next(_calls)
    log.warning("get_latest_analysis call=%d session_id=%r", call_no, session_id)
    rows = _rows("SELECT * FROM recordings WHERE ?1 IS NULL OR session_id = ?1 "
                 "ORDER BY created_at DESC, rowid DESC LIMIT 1", (session_id,))
    if not rows:
        return {"status": "none", "call_number": call_no}
    return {**_summary(rows[0]), "call_number": call_no}


@mcp.tool(annotations=READ_ONLY)
def get_analysis(recording_id: str) -> dict:
    """Analysis summary of one recording: overall scores, per-line tips, and flagged words."""
    rows = _rows("SELECT * FROM recordings WHERE id = ?", (recording_id,))
    return _summary(rows[0]) if rows else {"status": "not_found", "recording_id": recording_id}


@mcp.tool(annotations=READ_ONLY)
def list_analyses(session_id: str | None = None, limit: int = 10) -> list[dict]:
    """Past practice recordings, newest first, with overall scores for completed ones."""
    rows = _rows("SELECT * FROM recordings WHERE ?1 IS NULL OR session_id = ?1 "
                 "ORDER BY created_at DESC, rowid DESC LIMIT ?2", (session_id, max(1, min(limit, 50))))
    out = []
    for row in rows:
        item = {"recording_id": row["id"], "session_id": row["session_id"], "slide_id": row["slide_id"],
                "status": row["status"], "created_at": row["created_at"]}
        if row["status"] == "completed":
            item["overall"] = json.loads(row["result"])["overall"]
        out.append(item)
    return out


@mcp.tool(annotations=READ_ONLY)
def get_word_detail(recording_id: str, word_index: int) -> dict:
    """Phones, syllables, stress probabilities and per-syllable F0/intensity/duration z-scores of one word."""
    rows = _rows("SELECT status, result FROM recordings WHERE id = ?", (recording_id,))
    if not rows or rows[0]["status"] != "completed":
        return {"status": rows[0]["status"] if rows else "not_found", "recording_id": recording_id}
    words = [w for s in json.loads(rows[0]["result"])["lines"] for w in s["words"]]
    if not 0 <= word_index < len(words):
        return {"status": "not_found", "recording_id": recording_id, "word_count": len(words)}
    return {
        "recording_id": recording_id,
        **words[word_index],
    }


@mcp.tool(annotations=READ_ONLY)
def get_line_detail(recording_id: str, line: int) -> dict:
    """One script line in full: pitch contour, pauses, rhythm features, every coaching item, per-word/per-phone
    IPA and pitch, and the model reading alongside. Use it to explain a line in depth."""
    rows = _rows("SELECT status, result FROM recordings WHERE id = ?", (recording_id,))
    if not rows or rows[0]["status"] != "completed":
        return {"status": rows[0]["status"] if rows else "not_found", "recording_id": recording_id}
    lines = json.loads(rows[0]["result"])["lines"]
    if not 1 <= line <= len(lines):
        return {"status": "not_found", "recording_id": recording_id, "line_count": len(lines)}
    s = lines[line - 1]
    return {"recording_id": recording_id, **s,
            # phones kept (minus raw model evidence) so a renderer can mark single sounds
            "words": [{**w, "phones": [{k: v for k, v in p.items() if k != "evidence"} for p in w["phones"]]}
                      for w in s["words"]]}


def _cached_reference(text: str) -> dict | None:
    path = store.DATA / "reference" / f"{reference.key(text)}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _script(slide_id: str) -> list[str]:
    path = store.scripts().get(slide_id)
    if path is None:
        raise ToolError(f"unknown slide_id {slide_id!r}; see list_scripts")
    return store.script_lines(path.read_text(encoding="utf-8"))


@mcp.tool(annotations=READ_ONLY)
def list_scripts() -> list[dict]:
    """Talk scripts saved on the user's machine, and how many lines already have a model reading."""
    return [{"slide_id": s, "lines": len(lines), "reference_ready": sum(_cached_reference(t) is not None for t in lines)}
            for s, p in store.scripts().items() for lines in [store.script_lines(p.read_text(encoding="utf-8"))]]


@mcp.tool(annotations=READ_ONLY)
def get_script(slide_id: str) -> dict:
    """A script's lines with the model reading (TTS) of each: its IPA, intonation and per-word pitch."""
    out = []
    for i, text in enumerate(_script(slide_id), 1):
        ref = _cached_reference(text)
        if ref is None:
            out.append({"line": i, "text": text, "reference": "generating" if reference.available() else "unavailable"})
            continue
        out.append({"line": i, "text": text, "reference": {
            **{k: ref[k] for k in ("voice", "phonemes", "final_contour", "final_movement_st", "pitch_peak_word")},
            "words": [{k: w.get(k) for k in ("word", "tts_ipa", "f0_rel_st", "f0_movement_st")} for w in ref["words"]]}})
    return {"slide_id": slide_id, "lines": out,
            "note": "the model reading is one acceptable rendition by a local TTS, not the only correct intonation"}


@mcp.tool(annotations=WRITE_SCRIPT)
def prepare_practice(slide_id: str, text: str) -> dict:
    """Send the script of the part the user will rehearse next (one sentence per line, '#' lines are notes).
    It is saved on the user's machine, selected in their recorder, and its model reading starts generating."""
    text = store.one_sentence_per_line(text)  # one model reading per sentence, even if a paragraph was sent
    try:
        rev = store.save_script(slide_id, text)
    except ValueError as e:
        raise ToolError(str(e)) from e
    store.stage(slide_id)
    log.warning("prepare_practice slide_id=%s revision=%s", slide_id, rev)
    return {"slide_id": slide_id, "revision": rev, "lines": store.script_lines(text),
            "next": "the recorder has selected this script; the user presses record when ready. "
                    "Model readings are generated in the background (see get_script)."}


def _prepare_references():
    """Generate model readings as soon as a script appears or changes, so nothing waits for TTS later."""
    import analyze

    seen = {}
    while True:
        for slide, path in store.scripts().items():
            mtime = path.stat().st_mtime
            if seen.get(slide) == mtime:
                continue
            try:
                for i, text in enumerate(store.script_lines(path.read_text(encoding="utf-8")), 1):
                    analyze.reference_line(text, i)
                seen[slide] = mtime
                log.warning("model reading ready: %s", slide)
            except Exception as e:  # keep serving; retry on the next scan
                log.warning("model reading failed for %s: %s", slide, e)
        time.sleep(5)


if __name__ == "__main__":
    logging.basicConfig(stream=sys.stderr, format="%(asctime)s %(message)s")
    if reference.available():
        threading.Thread(target=_prepare_references, daemon=True).start()
    if "--stdio" in sys.argv:  # for Secure MCP Tunnel's tunnel-client --mcp-command
        mcp.run("stdio")
    else:
        # Binds to localhost only; expose it through Secure MCP Tunnel or an authenticated HTTPS proxy, never directly.
        mcp.run("streamable-http", host="127.0.0.1", port=8790)
