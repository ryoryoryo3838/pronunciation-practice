"""Local recorder page in the browser: pick a script, record, review/play/delete takes, play the model reading.

Binds to 127.0.0.1 only. Every request needs a matching Host header and state-changing ones a per-launch
token, so other web pages cannot start recordings or delete takes through the browser.
"""

import json
import os
import secrets
import tempfile
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.routing import Route

import pron
import reference
import store

PORT = int(os.environ.get("PRON_GUI_PORT", "8792"))
HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
TOKEN = secrets.token_urlsafe(24)
state = {"rec": None, "proc": None, "analyzing": None, "message": "", "preparing": set()}
lock = threading.Lock()


def _json_error(msg: str, code: int = 400):
    return JSONResponse({"error": msg}, status_code=code)


def _guarded(handler):
    async def wrapped(request):
        if request.headers.get("host") not in HOSTS:
            return _json_error("bad host", 403)
        if request.method == "POST" and not secrets.compare_digest(request.headers.get("x-token", ""), TOKEN):
            return _json_error("bad token", 403)
        return await handler(request)

    return wrapped


def _lines(slide: str) -> list[str]:
    return store.script_lines(store.scripts()[slide].read_text(encoding="utf-8"))


def _prepare(slide: str):
    """Model readings for every line, in the background (cached; the MCP server does the same)."""
    if not reference.available() or slide in state["preparing"]:
        return

    def work():
        import analyze

        state["preparing"].add(slide)
        try:
            for i, line in enumerate(_lines(slide), 1):
                analyze.reference_line(line, i)
        except Exception as e:
            state["message"] = f"お手本の生成に失敗: {e}"
        finally:
            state["preparing"].discard(slide)

    threading.Thread(target=work, daemon=True).start()


async def page(request):
    return HTMLResponse(HTML.replace("__TOKEN__", TOKEN))


async def api_state(request):
    scripts = []
    for slide in store.scripts():
        lines = _lines(slide)
        ready = sum((store.DATA / "reference" / f"{reference.key(t)}.json").exists() for t in lines)
        scripts.append({"slide": slide, "lines": len(lines), "reference_ready": ready})
    rec = state["rec"]
    return JSONResponse({
        "scripts": scripts,
        "reference_available": reference.available(),
        "recording": rec and {"id": rec["id"], "slide": rec["slide"], "elapsed": time.monotonic() - rec["t0"]},
        "analyzing": state["analyzing"],
        "message": state["message"],
        "staged": store.staged(),
    })


async def api_history(request):
    if not store.DB.exists():
        return JSONResponse([])
    con = store.connect(readonly=True)
    rows = con.execute("SELECT id, slide_id, status, created_at, result, error FROM recordings "
                       "ORDER BY created_at DESC, rowid DESC LIMIT 200").fetchall()
    con.close()
    out = []
    for r in rows:
        res = json.loads(r["result"]) if r["result"] else None
        out.append({"id": r["id"], "slide": r["slide_id"], "status": r["status"], "created_at": r["created_at"],
                    "overall": res and res["overall"], "duration_ms": res and res["duration_ms"], "error": r["error"]})
    return JSONResponse(out)


async def api_prepare(request):
    slide = (await request.json()).get("slide")
    if slide not in store.scripts():
        return _json_error("unknown script")
    _prepare(slide)
    return JSONResponse({"ok": True})


async def api_start(request):
    slide = (await request.json()).get("slide")
    if slide not in store.scripts():
        return _json_error("unknown script")
    with lock:
        if state["proc"] is not None or state.get("retake_proc") is not None:
            return _json_error("already recording", 409)
        con = store.connect()
        rid, script, audio = pron._register(con, str(store.scripts()[slide]), None, None)
        con.close()
        state["proc"] = pron.start_recording(store.DATA / "audio" / audio)
        state["rec"] = {"id": rid, "slide": slide, "script": script, "audio": audio, "t0": time.monotonic()}
        state["message"] = ""
    _prepare(slide)
    return JSONResponse({"id": rid})


def _stop():
    with lock:
        proc, rec = state["proc"], state["rec"]
        if proc is None:
            return None
        state["proc"], state["rec"] = None, None
    ok = pron.stop_recording(proc, store.DATA / "audio" / rec["audio"])
    if not ok:
        con = store.connect()
        store.set_status(con, rec["id"], "failed", error="recording file is empty")
        state["message"] = "録音に失敗しました（マイクを確認してください）"
        return rec

    def work():
        state["analyzing"] = rec["id"]
        con = store.connect()  # sqlite connections stay in the thread that made them
        try:
            pron._analyse(con, rec["id"], rec["script"], rec["audio"])
            state["message"] = f"{rec['id']} の解析が終わりました。ChatGPT に「最新の発音分析を見て」と頼めます"
        except (Exception, SystemExit) as e:
            state["message"] = f"解析に失敗: {e}"
        finally:
            state["analyzing"] = None

    threading.Thread(target=work, daemon=True).start()
    return rec


async def api_stop(request):
    rec = _stop()
    return JSONResponse({"id": rec and rec["id"]})


async def api_delete(request):
    rid = (await request.json()).get("id")
    if rid in (state["analyzing"], (state["rec"] or {}).get("id")):
        return _json_error("この録音は処理中です", 409)
    con = store.connect()
    ok = store.delete(con, rid)
    con.close()
    return JSONResponse({"ok": ok}) if ok else _json_error("not found", 404)


async def audio(request):
    if not store.DB.exists():
        return _json_error("not found", 404)
    con = store.connect(readonly=True)
    row = con.execute("SELECT audio_file FROM recordings WHERE id = ?", (request.path_params["rid"],)).fetchone()
    con.close()
    path = row and store.DATA / "audio" / row["audio_file"]
    return FileResponse(path) if path and path.exists() else _json_error("not found", 404)


async def model_audio(request):
    slide, line = request.path_params["slide"], request.path_params["line"]
    if slide not in store.scripts() or not 1 <= line <= len(_lines(slide)):
        return _json_error("not found", 404)
    path = store.DATA / "reference" / f"{reference.key(_lines(slide)[line - 1])}.wav"
    return FileResponse(path) if path.exists() else _json_error("not ready", 404)


def _recording(rid: str):
    if not store.DB.exists():
        return None
    con = store.connect(readonly=True)
    row = con.execute("SELECT script, status, result, created_at FROM recordings WHERE id = ?", (rid,)).fetchone()
    con.close()
    return row


FLAG = 0.5  # same bar as the MCP's flagged_words
WITHHELD = ("not in dictionary", "model reading disagrees")


def advice_ja(c: dict) -> str | None:
    """Japanese advice built from Pronunciada's structured coaching item (its own tips are English only)."""
    k, w = c["kind"], c.get("word")
    sec = lambda v: f"{(v or 0):.1f} 秒"  # noqa: E731
    if k == "substitution":
        heard = f"/{c['heard']}/ に聞こえた" if c.get("heard") else "はっきり聞き取れなかった"
        return f"「{w}」の /{c['expected']}/ が {heard}"
    if k == "missing":
        return f"「{w}」の /{c['expected']}/ が抜けて聞こえた"
    if k == "missing_word":
        return f"「{w}」が聞こえなかった（言い換えや言い淀みかもしれない）"
    if k == "stress":
        heard = f"。第 {c['heard'] + 1} 音節に強勢があるように聞こえた" if c.get("heard") is not None else "が弱かった"
        return f"「{w}」は第 {c['expected'] + 1} 音節に強勢を置く{heard}"
    if k == "length":
        return f"「{w}」の /{c.get('expected')}/ の長さがずれた"
    if k == "extra_speech":
        return f"原稿にない発話 /{c.get('heard')}/ があった"
    if k == "long_pause":
        return f"「{w}」のあとで {sec(c.get('duration'))}止まった（長め）"
    if k == "pause_mid_phrase":
        return f"「{w}」のあと、句の途中で {sec(c.get('duration'))}止まった"
    if k == "rushed_punctuation":
        return f"「{w}」のあとの句読点で、ほとんど間を取らなかった"
    if k == "flat_pitch":
        return f"抑揚が平板（音域 {c.get('pitch_range_st') or 0:.1f} 半音）"
    if k == "slow":
        return "話す速さがゆっくりめ"
    if k == "fast":
        return "話す速さが速め"
    return None  # mispronounced_word only repeats its sound-level items


def _pieces(w: dict) -> tuple[list, list]:
    """Expected and heard IPA phone by phone as [text, flagged], so single sounds can turn red."""
    exp, heard, i, ipa = [], [], 0, w["expected_ipa"] or ""
    stress_bad = w["estimated_stress_syllable"] is not None
    for p in w["phones"]:
        while i < len(ipa) and ipa[i] in "ˈˌ":
            exp.append([ipa[i], stress_bad and ipa[i] == "ˈ"])
            i += 1
        i += len(p["ipa"])
        bad = p["prob"] is not None and p["prob"] < FLAG
        exp.append([p["ipa"], bad])
        heard.append([p["heard"] or "∅", bad])
    return exp, heard


def _view_line(ln: dict) -> dict:
    """One analysed line as the analysis view draws it (also used for single-sentence retakes)."""
    words, held = [], set()
    for k, w in enumerate(ln["words"]):
        withheld = any(x.startswith(WITHHELD) for x in w["warnings"])
        if withheld:
            held.add(k)
        ref = w.get("reference") or {}
        exp, heard = _pieces(w)
        words.append({
            "w": w["word"], "s": w["start_ms"], "e": w["end_ms"], "prob": w["prob"], "omitted": w["omitted"],
            "withheld": withheld, "oov": any(x.startswith("not in dictionary") for x in w["warnings"]),
            "exp": exp, "heard": heard, "tts": ref.get("tts_ipa"),
            "stress": (f"{w['expected_stress_syllable']}→{w['estimated_stress_syllable']}"
                       if w["estimated_stress_syllable"] is not None else None),
            "contour": w.get("contour_rel") or [], "ref_contour": ref.get("contour_rel") or [],
            "ref_s": ref.get("start_ms"), "ref_e": ref.get("end_ms"),
        })
    coach = [c for c in ln["coaching"] if c.get("word_index") not in held]
    advice, seen = [], set()
    for c in coach:  # already ranked by Pronunciada; keep the first of each (kind, word, sound)
        key = (c["kind"], c.get("word_index"), c.get("expected"))
        if key not in seen and (t := advice_ja(c)):
            seen.add(key)
            advice.append({"text": t, "kind": c["kind"], "start_ms": c.get("start_ms")})
    it, ref = ln["intonation"], ln.get("reference") or {}
    return {
        "line": ln["line"], "text": ln["text"], "s": ln["start_ms"], "e": ln["end_ms"],
        "total": ln["scores"].get("total"), "words": words, "advice": advice,
        "flat": any(c["kind"] == "flat_pitch" for c in coach),
        "pauses": ln["delivery"]["pauses"],
        "bad_pauses": sorted({c["word_index"] for c in coach
                              if c["kind"] in ("long_pause", "pause_mid_phrase", "rushed_punctuation")}),
        "final": [it["final_contour"], it["final_movement_st"]],
        "ref_final": [ref.get("final_contour"), ref.get("final_movement_st")],
    }


async def api_analysis(request):
    """Everything the analysis view draws for one take: per line, per word, model reading alongside."""
    row = _recording(request.path_params["rid"])
    if row is None or not row["result"]:
        return _json_error("not analysed", 404)
    res = json.loads(row["result"])
    return JSONResponse({"overall": res["overall"], "lines": [_view_line(ln) for ln in res["lines"]],
                         "model": reference.available()})


# ---- single-sentence retakes: scored on the spot, never stored (only the latest WAV lives in /tmp)
def _drop_retake_file():
    if state.get("retake_file"):
        Path(state["retake_file"]).unlink(missing_ok=True)
        state["retake_file"] = None


async def api_retake_start(request):
    body = await request.json()
    row, line = _recording(str(body.get("rid"))), body.get("line")
    if row is None or not isinstance(line, int) or not 1 <= line <= len(store.script_lines(row["script"])):
        return _json_error("not found", 404)
    with lock:
        if state["proc"] is not None or state.get("retake_proc") is not None:
            return _json_error("録音中です", 409)
        _drop_retake_file()
        fd, path = tempfile.mkstemp(prefix="pron-retake-", suffix=".wav")
        os.close(fd)
        state["retake_file"], state["retake_text"], state["retake_line"] = path, store.script_lines(row["script"])[line - 1], line
        state["retake_proc"] = pron.start_recording(Path(path))
    return JSONResponse({"ok": True})


def _score_retake(path: str, text: str, line: int) -> dict:
    import analyze

    x = analyze.load_audio(path)
    if len(x) < analyze.SR * 0.3 or float(abs(x).max()) < 1e-4:
        raise ValueError("無音でした。マイクを確認してください")
    seg = analyze._score_line(x, text, 0.0, line)
    if (seg["audio"] or {}).get("retry_recommended") or (seg["completeness"] or 0) < analyze.MIN_COMPLETENESS:
        raise ValueError(f"原稿の文と合いませんでした（聞き取れた語の割合 {seg['completeness']}）")
    if reference.available():
        analyze._attach_references([seg])
    return seg


async def api_retake_stop(request):
    with lock:
        proc, state["retake_proc"] = state.get("retake_proc"), None
    if proc is None:
        return _json_error("録音していません", 409)
    path, text, line = state["retake_file"], state["retake_text"], state["retake_line"]
    if not pron.stop_recording(proc, Path(path)):
        _drop_retake_file()
        return _json_error("録音に失敗しました（マイクを確認してください）")
    try:
        seg = await run_in_threadpool(_score_retake, path, text, line)
    except Exception as e:
        _drop_retake_file()
        return _json_error(str(e))
    state["retake_n"] = state.get("retake_n", 0) + 1
    return JSONResponse({"line": _view_line(seg), "n": state["retake_n"]})


async def retake_audio(request):
    path = state.get("retake_file")
    return FileResponse(path) if path and Path(path).exists() else _json_error("not found", 404)


async def model_for_recording(request):
    """The model reading of line N of the script exactly as it was when this take was recorded."""
    row, line = _recording(request.path_params["rid"]), request.path_params["line"]
    if row is None or not reference.available():
        return _json_error("not found", 404)
    lines = store.script_lines(row["script"])
    if not 1 <= line <= len(lines):
        return _json_error("not found", 404)
    return FileResponse(await run_in_threadpool(reference.line_audio, lines[line - 1]))


async def api_script_preview(request):
    """Paste or file -> proposed scripts (one sentence per line), split by headings if asked."""
    body = await request.json()
    text = str(body.get("text", ""))[: store.MAX_SCRIPT_CHARS * 20]
    parts = store.split_document(text, bool(body.get("by_headings")))
    out, used = [], set()
    for i, p in enumerate(parts, 1):
        sid = store.slug(p["title"], i) if p["title"] else (body.get("name") or "script")
        base, k = sid, 2
        while sid in used:  # two headings may slug alike
            sid, k = f"{base}-{k}", k + 1
        used.add(sid)
        out.append({"title": p["title"], "slide_id": sid, "lines": p["lines"], "exists": sid in store.scripts()})
    return JSONResponse({"parts": out, "has_headings": any(ln.lstrip().startswith("#") for ln in text.splitlines())})


async def api_script_save(request):
    saved = []
    for p in (await request.json()).get("parts", []):
        # a line edited into two sentences becomes two lines (one model reading each)
        lines = store.script_lines(store.one_sentence_per_line("\n".join(str(x).strip() for x in p.get("lines", []))))
        try:
            store.save_script(str(p.get("slide_id", "")), "\n".join(lines) + "\n")
        except ValueError as e:
            return _json_error(f"{p.get('slide_id')}: {e}")
        saved.append(p["slide_id"])
        _prepare(p["slide_id"])
    return JSONResponse({"saved": saved})


async def api_script_edit(request):
    """Edited prose for one script -> one sentence per line; '#' notes already in the file are kept."""
    body = await request.json()
    slide, text = str(body.get("slide_id", "")), str(body.get("text", ""))
    if slide not in store.scripts():
        return _json_error("not found", 404)
    notes = [ln for ln in store.scripts()[slide].read_text(encoding="utf-8").splitlines() if ln.lstrip().startswith("#")]
    lines = store.split_sentences(text)
    try:
        store.save_script(slide, "\n".join(notes + lines) + "\n")
    except ValueError as e:
        return _json_error(str(e))
    _prepare(slide)
    return JSONResponse({"lines": len(lines)})


async def api_script_get(request):
    slide = request.path_params["slide"]
    if slide not in store.scripts():
        return _json_error("not found", 404)
    text = store.scripts()[slide].read_text(encoding="utf-8")
    return JSONResponse({"slide_id": slide, "text": text, "lines": [
        {"text": t, "ready": (store.DATA / "reference" / f"{reference.key(t)}.json").exists()}
        for t in store.script_lines(text)]})


app = Starlette(routes=[
    Route("/", _guarded(page)),
    Route("/api/state", _guarded(api_state)),
    Route("/api/history", _guarded(api_history)),
    Route("/api/prepare", _guarded(api_prepare), methods=["POST"]),
    Route("/api/start", _guarded(api_start), methods=["POST"]),
    Route("/api/stop", _guarded(api_stop), methods=["POST"]),
    Route("/api/delete", _guarded(api_delete), methods=["POST"]),
    Route("/audio/{rid}", _guarded(audio)),
    Route("/model/{slide}/{line:int}", _guarded(model_audio)),
    Route("/api/analysis/{rid}", _guarded(api_analysis)),
    Route("/api/retake/start", _guarded(api_retake_start), methods=["POST"]),
    Route("/api/retake/stop", _guarded(api_retake_stop), methods=["POST"]),
    Route("/retake-audio", _guarded(retake_audio)),
    Route("/api/scripts/preview", _guarded(api_script_preview), methods=["POST"]),
    Route("/api/scripts/save", _guarded(api_script_save), methods=["POST"]),
    Route("/api/scripts/edit", _guarded(api_script_edit), methods=["POST"]),
    Route("/api/scripts/{slide}", _guarded(api_script_get)),
    Route("/model-for/{rid}/{line:int}", _guarded(model_for_recording)),
])

HTML = r"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>発音練習レコーダー</title>
<style>
:root{--bg:#f6f5f2;--card:#fff;--ink:#1d1d1f;--sub:#6e6e73;--line:#e6e4df;--red:#e5483f;--red2:#c4302b;
--ok:#2e8b57;--warn:#c77700;--shadow:0 1px 2px rgba(0,0,0,.04),0 8px 24px rgba(0,0,0,.06)}
@media (prefers-color-scheme:dark){:root{--bg:#161618;--card:#212124;--ink:#f2f2f4;--sub:#9a9aa1;--line:#323236;
--red:#ff5a50;--red2:#ff6b62;--ok:#4cc38a;--warn:#f0a020;--shadow:0 1px 2px rgba(0,0,0,.3),0 8px 24px rgba(0,0,0,.35)}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 "Noto Sans CJK JP","Noto Sans JP",system-ui,sans-serif}
main{max-width:760px;margin:0 auto;padding:32px 16px 48px}
h1{font-size:20px;font-weight:700;margin:0 0 4px;letter-spacing:.02em}
.lead{color:var(--sub);margin:0 0 24px;font-size:13px}
.card{background:var(--card);border-radius:18px;box-shadow:var(--shadow);padding:24px;margin-bottom:20px}
.row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
select{flex:1;min-width:200px;font:inherit;padding:10px 12px;border-radius:12px;border:1px solid var(--line);
background:var(--card);color:var(--ink)}
.pill{font-size:12px;color:var(--sub);background:var(--bg);border-radius:999px;padding:4px 10px;white-space:nowrap}
.pill.ok{color:var(--ok)}.pill.gpt{color:var(--ink);background:color-mix(in srgb,#10a37f 16%,transparent)}
.stage{display:flex;flex-direction:column;align-items:center;padding:20px 0 4px}
.rec{width:120px;height:120px;border-radius:50%;border:6px solid color-mix(in srgb,var(--red) 22%,transparent);
background:var(--card);display:grid;place-items:center;cursor:pointer;transition:transform .15s,border-color .2s}
.rec:hover{transform:scale(1.04)}.rec:active{transform:scale(.97)}
.rec .dot{width:64px;height:64px;border-radius:50%;background:var(--red);transition:all .25s}
.rec.on{border-color:var(--red);animation:pulse 1.6s infinite}
.rec.on .dot{width:40px;height:40px;border-radius:9px;background:var(--red2)}
.rec:disabled{opacity:.45;cursor:default;transform:none}
.rec:focus-visible,button:focus-visible,select:focus-visible{outline:3px solid color-mix(in srgb,var(--red) 50%,transparent);outline-offset:3px}
@keyframes pulse{0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--red) 45%,transparent)}
70%{box-shadow:0 0 0 18px transparent}100%{box-shadow:0 0 0 0 transparent}}
.timer{font-size:34px;font-weight:300;font-variant-numeric:tabular-nums;margin-top:14px;letter-spacing:.04em}
.hint{color:var(--sub);font-size:13px;min-height:1.6em;text-align:center}
button.ghost{font:inherit;font-size:14px;border:1px solid var(--line);background:transparent;color:var(--ink);
padding:8px 14px;border-radius:12px;cursor:pointer}
button.ghost:hover{background:var(--bg)}button.ghost:disabled{opacity:.4;cursor:default}
.scores{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:18px;width:100%}
.score{background:var(--bg);border-radius:12px;padding:10px;text-align:center}
.score b{display:block;font-size:22px;font-weight:600;font-variant-numeric:tabular-nums}
.score span{font-size:12px;color:var(--sub)}
h2{font-size:15px;margin:0 0 12px}
table{width:100%;border-collapse:collapse;font-size:14px}
td{padding:10px 6px;border-top:1px solid var(--line);vertical-align:middle}
td.num{font-variant-numeric:tabular-nums;text-align:right;font-weight:600}
.st{font-size:12px;border-radius:999px;padding:2px 8px;background:var(--bg);color:var(--sub)}
.st.completed{color:var(--ok)}.st.failed{color:var(--red2)}.st.analyzing,.st.recording{color:var(--warn)}
.icon{border:0;background:transparent;color:var(--sub);cursor:pointer;font-size:15px;padding:4px 8px;border-radius:8px}
.icon:hover{background:var(--bg);color:var(--ink)}.icon.del:hover{color:var(--red2)}
.empty{color:var(--sub);text-align:center;padding:16px}
.chip{font:inherit;font-size:12px;border:1px solid var(--line);background:var(--card);color:var(--ink);
border-radius:999px;padding:4px 11px;cursor:pointer;white-space:nowrap}.chip:hover{border-color:var(--sub)}
.chip:disabled{opacity:.4;cursor:default}.chip.on{background:var(--ink);color:var(--card);border-color:var(--ink)}
.top{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:16px}
.top h1{margin:0;flex:1}
.legend{font-size:12px;color:var(--sub);margin:0 0 16px;line-height:1.8}
.legend i{font-style:normal;padding:0 2px}
.ln-head{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;margin-bottom:6px}
.ln-head .n{color:var(--sub);font-variant-numeric:tabular-nums}
.ln-head .txt{flex:1;min-width:200px;font-weight:600}
.ln-head .sc{font-weight:700;font-variant-numeric:tabular-nums}
.ln-meta{font-size:12px;color:var(--sub);margin-bottom:10px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.words{display:flex;flex-wrap:wrap;gap:6px 4px;align-items:flex-start}
.wd{font:inherit;color:inherit;border:0;background:transparent;border-radius:10px;padding:4px 6px;cursor:pointer;
text-align:left;display:flex;flex-direction:column;min-width:34px}
.wd:hover{background:var(--bg)}.wd[data-playing]{background:color-mix(in srgb,#2f6fde 14%,transparent)}
.wd svg{width:0;min-width:100%;height:56px;display:block;overflow:visible;margin-bottom:2px}
.wd .t{font-size:17px;font-weight:600;line-height:1.3}.wd.bad .t{color:var(--red2)}.wd.oov .t,.wd.held .t{color:var(--sub)}
.wd .x{font:14px/1.35 "DejaVu Sans","Noto Sans",sans-serif;white-space:nowrap}
.wd .h{color:var(--sub)}.wd .r{color:var(--red2);font-weight:700}
.wd .note{font-size:11px;color:var(--red2);white-space:nowrap}.wd.oov .note,.wd.held .note{color:var(--sub)}
.pz{align-self:center;font-size:11px;color:var(--sub);padding:0 2px}.pz.bad{color:var(--red2);font-weight:700}
.adv{margin:12px 0 0;padding:0;list-style:none;font-size:13px}
.adv li{padding:6px 0 6px 18px;position:relative;border-top:1px dashed var(--line)}
.adv li:before{content:"";position:absolute;left:4px;top:13px;width:6px;height:6px;border-radius:50%;background:var(--red)}
.adv li.info:before{background:var(--sub)}
.ok-line{font-size:13px;color:var(--ok);margin-top:10px}
dialog{border:0;border-radius:18px;padding:0;background:var(--card);color:var(--ink);box-shadow:var(--shadow);
width:min(720px,calc(100vw - 32px));max-height:calc(100vh - 32px)}dialog::backdrop{background:rgba(0,0,0,.35)}
.dlg{padding:22px 22px 0;display:flex;flex-direction:column;max-height:calc(100vh - 32px)}.dlg h2{font-size:17px}
#pv{overflow:auto;flex:1;min-height:0}
.dlg-foot{position:sticky;bottom:0;background:var(--card);padding:12px 0 18px;border-top:1px solid var(--line)}
textarea,#name{width:100%;font:14px/1.5 inherit;font-family:inherit;padding:10px 12px;border-radius:12px;
border:1px solid var(--line);background:var(--bg);color:var(--ink)}textarea.drop{outline:2px dashed var(--red)}
#name{flex:1;min-width:200px;width:auto}.chk{font-size:13px;display:flex;gap:6px;align-items:center}
.part{border-top:1px solid var(--line);padding:8px 0;font-size:13px}
.part .row{gap:8px}.part input[type=text]{font:inherit;font-size:13px;padding:4px 8px;border-radius:8px;
border:1px solid var(--line);background:var(--bg);color:var(--ink);width:220px}
.part ol{margin:6px 0 0;padding-left:22px;color:var(--sub);max-height:160px;overflow:auto}
.part .warn{color:var(--warn);font-size:12px}
button.primary,button.primary:hover{background:var(--ink);color:var(--card);border-color:var(--ink)}
button.primary:hover{opacity:.85}
.sc{outline:0;border-radius:12px;padding:10px 12px;line-height:1.8;white-space:pre-wrap}
.sc.editing{background:var(--bg);cursor:text;box-shadow:0 0 0 2px color-mix(in srgb,#2f6fde 40%,transparent)}
.sc.big{font-size:24px;line-height:1.7;font-weight:500}
.chip.retake{color:var(--red2);border-color:color-mix(in srgb,var(--red) 35%,var(--line))}
.chip.retake.on{background:var(--red);color:#fff;border-color:var(--red);animation:pulse 1.6s infinite}
.rt{margin-top:14px;padding:14px;border-radius:14px;background:var(--bg)}
.rt .wd:hover{background:var(--card)}
.rt-head{display:flex;gap:10px;align-items:baseline;margin-bottom:8px;flex-wrap:wrap}
.rt-head .trail{color:var(--sub);font-size:13px;font-variant-numeric:tabular-nums;flex:1}
.delta{font-weight:700;font-variant-numeric:tabular-nums}.delta.up{color:var(--ok)}.delta.down{color:var(--red2)}
details.more summary{font-size:12px;color:var(--sub);cursor:pointer;padding:6px 0 0 4px}
details.more .adv{margin-top:4px}
polyline.me{stroke:#2f6fde;stroke-width:2.5;stroke-linejoin:round;stroke-linecap:round}
polyline.flat{stroke:var(--red2);stroke-width:2.5;stroke-linejoin:round;stroke-linecap:round}
polyline.refc{stroke:var(--sub);stroke-width:2;stroke-dasharray:4 3}
line.mid{stroke:var(--line);stroke-width:1}
.icon.an{font-size:12px;border:1px solid var(--line);border-radius:999px;padding:3px 10px}.icon.an:disabled{opacity:.35;cursor:default}
.err{color:var(--red2);font-size:12px}
@media (max-width:520px){.scores{grid-template-columns:repeat(2,1fr)}td.hide{display:none}}
</style></head><body><main>
<div id="home">
<h1>発音練習レコーダー</h1>
<p class="lead">原稿を選んで録音し、停止すると自動で解析します。結果はここでも ChatGPT からも見られます。</p>

<section class="card">
  <div class="row">
    <select id="script" aria-label="原稿"></select>
    <button class="ghost" id="add">＋ 原稿</button>
    <span class="pill gpt" id="staged" hidden></span>
    <span class="pill" id="ref"></span>
  </div>
  <div class="stage">
    <button class="rec" id="rec" aria-label="録音開始"><span class="dot"></span></button>
    <div class="timer" id="timer">0:00</div>
    <div class="hint" id="hint" aria-live="polite">ボタンを押すと録音を始めます</div>
    <div class="scores" id="scores" hidden></div>
  </div>
</section>

<section class="card" id="sc-card" hidden>
  <div class="row" style="margin-bottom:6px">
    <h2 style="margin:0;flex:1">原稿</h2>
    <span class="hint" id="sc-status" style="min-height:0"></span>
    <button class="ghost" id="model">▶ お手本</button>
    <button class="ghost" id="sc-edit">修正</button>
    <button class="ghost" id="sc-cancel" hidden>取消</button>
    <button class="ghost primary" id="sc-ok" hidden>確定</button>
    <label class="chk"><input type="checkbox" id="big"> 大きく表示</label>
  </div>
  <p class="lead" id="sc-help" style="margin:0 0 8px">「修正」で本文を直し、「確定」で保存します。内容が変わった文はお手本を作り直します。</p>
  <div class="sc" id="sc-text" spellcheck="true" role="textbox" aria-multiline="true" aria-readonly="true" aria-label="原稿の本文"></div>
</section>

<section class="card">
  <h2>録音の履歴</h2>
  <table><tbody id="history"></tbody></table>
</section>
</div>

<div id="analysis" hidden>
  <div class="top">
    <button class="ghost" id="back">← 戻る</button>
    <h1 id="an-title"></h1>
  </div>
  <div class="scores" id="an-scores" style="margin:0 0 16px"></div>
  <p class="legend">語をクリックすると、その語を自分 → お手本の順に再生します。
  <i style="color:var(--ink)">黒</i>正しい発音（辞書） <i>灰</i>聞こえた発音 <i style="color:var(--red2)">赤</i>ずれた音・強勢・抜け
  <i style="color:#2f6fde">━</i>自分のピッチ <i>┅</i>お手本のピッチ（どちらも各自の行内中央値からの半音） ‖ ポーズ（秒）</p>
  <div id="an-lines"></div>
</div>
<audio id="player"></audio>
<dialog id="dlg">
  <form method="dialog" class="dlg">
    <h2 id="dlg-title">原稿を追加</h2>
    <p class="lead" style="margin:0 0 10px">英文を貼り付けるか、.txt / .md ファイルをここにドロップ。1 行 1 文に自動で分け、保存するとすぐにお手本を作り始めます。</p>
    <textarea id="src" rows="9" placeholder="Thank you, chair. My name is ..."></textarea>
    <div class="row" style="margin:10px 0">
      <label class="chk"><input type="checkbox" id="byh"> 見出し（#）ごとに別の原稿にする</label>
      <input id="name" placeholder="原稿の名前（英数字と - _）" pattern="[A-Za-z0-9][A-Za-z0-9_-]{0,63}">
    </div>
    <div id="pv"></div>
    <div class="row dlg-foot" style="justify-content:flex-end;margin-top:10px">
      <button class="ghost" value="cancel" formnovalidate>キャンセル</button>
      <button class="ghost primary" id="save" value="default">保存してお手本を作る</button>
    </div>
  </form>
</dialog>
</main>
<script>
const TOKEN="__TOKEN__";
const $=id=>document.getElementById(id);
const esc=s=>String(s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const post=(url,body)=>fetch(url,{method:"POST",headers:{"content-type":"application/json","x-token":TOKEN},
  body:JSON.stringify(body||{})}).then(r=>r.json());
const fmt=s=>`${Math.floor(s/60)}:${String(Math.floor(s%60)).padStart(2,"0")}`;
let S=null, lastBusy=undefined, playing=null, scriptKey="", stagedAt=null, finished=null;

async function refresh(){
  S=await (await fetch("/api/state")).json();
  const sel=$("script"), cur=sel.value, key=S.scripts.map(s=>s.slide).join("|");
  if(key!==scriptKey){
    scriptKey=key;
    sel.innerHTML=S.scripts.map(s=>`<option value="${esc(s.slide)}">${esc(s.slide)}（${s.lines} 文）</option>`).join("")
      ||'<option value="">slides/ に原稿（.txt）を置いてください</option>';
    if(S.scripts.some(s=>s.slide===cur)) sel.value=cur;
    prepare();
  }
  // A script sent from ChatGPT (prepare_practice) becomes the selection, unless a take is running.
  if(S.staged && S.staged.at!==stagedAt && !S.recording && !scEditMode && S.scripts.some(s=>s.slide===S.staged.slide_id)){
    stagedAt=S.staged.at; sel.value=S.staged.slide_id; prepare();
  }
  const fromGpt=S.staged && S.staged.slide_id===sel.value;
  $("staged").hidden=!fromGpt; $("staged").textContent="ChatGPT から受け取った原稿";
  const sc=S.scripts.find(s=>s.slide===sel.value), ref=$("ref");
  if(!S.reference_available){ref.textContent="お手本なし";ref.className="pill";$("model").disabled=true}
  else if(sc){const done=sc.reference_ready===sc.lines;
    ref.textContent=done?"お手本 準備完了":`お手本 生成中 ${sc.reference_ready}/${sc.lines}`;
    ref.className="pill"+(done?" ok":"");$("model").disabled=!done&&playing!=="model"}
  const rec=$("rec"), on=!!S.recording;
  rec.classList.toggle("on",on); rec.setAttribute("aria-label",on?"停止":"録音開始");
  rec.disabled=!on&&(!!S.analyzing||!sc); sel.disabled=on||scEditMode;
  $("timer").textContent=fmt(on?S.recording.elapsed:0);
  $("hint").textContent=on?`録音中 · ${S.recording.id} · もう一度押すと停止`:
    S.analyzing?`解析中… ${S.analyzing}`:(S.message||"ボタンを押すと録音を始めます");
  const busy=(S.recording&&S.recording.id)||S.analyzing||null;
  if(busy!==lastBusy){if(lastBusy&&!busy) finished=lastBusy; lastBusy=busy; loadHistory()}
}
function prepare(){const v=$("script").value; if(v) post("/api/prepare",{slide:v}); loadScript(true)}
$("script").onchange=prepare;
$("rec").onclick=async()=>{
  if(S.recording) await post("/api/stop"); else {$("scores").hidden=true; await post("/api/start",{slide:$("script").value})}
  refresh();
};
// One player for everything; a new press stops whatever is playing.
const P=$("player"); let tok=0;
function stopPlay(){tok++; P.pause();
  document.querySelectorAll("[data-playing]").forEach(b=>{b.textContent=b.dataset.label; delete b.dataset.playing}); playing=null}
function clip(src,a,b){return new Promise(res=>{
  P.onended=P.onpause=()=>res(); P.onerror=()=>res();
  P.ontimeupdate=()=>{if(b!=null&&P.currentTime>=b)P.pause()};
  P.onloadedmetadata=()=>{if(a)P.currentTime=a; P.play().catch(()=>res())};
  P.src=src;})}
async function playSeq(btn,items,tag){
  if(btn.dataset.playing){stopPlay();return}
  stopPlay(); const t=tok; btn.dataset.label=btn.textContent; btn.dataset.playing=1; btn.textContent="■"; playing=tag||null;
  for(const [src,a,b] of items){if(t!==tok)break; await clip(src,a,b); if(t!==tok)break; await new Promise(r=>setTimeout(r,300))}
  if(t===tok){btn.textContent=btn.dataset.label; delete btn.dataset.playing; playing=null}
}
$("model").onclick=()=>{
  const sc=S.scripts.find(s=>s.slide===$("script").value);
  playSeq($("model"),Array.from({length:sc.lines},(_,i)=>[`/model/${encodeURIComponent(sc.slide)}/${i+1}`,0,null]),"model");
};
function showScores(o,id){
  const k=[["総合","total"],["正確さ","accuracy"],["流暢さ","fluency"],["韻律","prosodic"]];
  $("scores").innerHTML=k.map(([n,x])=>`<div class="score"><b>${(o[x]*100).toFixed(0)}</b><span>${n}</span></div>`).join("")
    +`<button class="ghost" style="grid-column:1/-1" onclick="openAnalysis('${esc(id)}')">分析を見る →</button>`;
  $("scores").hidden=false;
}
async function loadHistory(){
  const rows=await (await fetch("/api/history")).json();
  // Scores only for the take that just finished, not for whatever was recorded last time.
  const done=rows.find(r=>r.id===finished);
  if(done?.overall && !S?.recording) showScores(done.overall,done.id);
  $("history").innerHTML=rows.length?rows.map(r=>{
    const d=new Date(r.created_at), when=`${d.getMonth()+1}/${d.getDate()} ${d.getHours()}:${String(d.getMinutes()).padStart(2,"0")}`;
    const tot=r.overall?(r.overall.total*100).toFixed(0):"–", len=r.duration_ms?fmt(r.duration_ms/1000):"";
    const st={completed:"完了",failed:"失敗",analyzing:"解析中",recording:"録音中"}[r.status], id=esc(r.id);
    return `<tr><td>${when}</td><td>${id}</td><td class="hide">${len}</td>
      <td><span class="st ${r.status}">${st}</span>${r.error?`<div class="err">${esc(r.error.slice(0,60))}</div>`:""}</td>
      <td class="num">${tot}</td>
      <td style="text-align:right;white-space:nowrap">
      <button class="icon an" title="分析を開く" aria-label="${id} の分析を開く" data-id="${id}" ${r.status==="completed"?"":"disabled"}>分析</button>
      <button class="icon play" title="録音を再生" aria-label="${id} を再生" data-id="${id}">▶</button>
      <button class="icon del" title="削除" aria-label="${id} を削除" data-id="${id}">🗑</button></td></tr>`}).join("")
    :'<tr><td class="empty">まだ録音はありません</td></tr>';
}
const parse=s=>{const [src,a,b]=s.split("|"); return [src,a?+a:0,b?+b:null]};
$("history").onclick=async e=>{
  const b=e.target.closest("button"); if(!b) return; const id=b.dataset.id;
  if(b.classList.contains("an")) return openAnalysis(id);
  if(b.classList.contains("play")) return playSeq(b,[[`/audio/${encodeURIComponent(id)}`,0,null]]);
  if(confirm(`${id} を削除しますか？\n音声と解析結果が消え、元に戻せません。`)){
    const r=await post("/api/delete",{id}); if(r.error) alert(r.error); loadHistory();
  }
};

// ---- analysis view: the app alone is enough to see and hear what went wrong
let AN=null;
$("back").onclick=()=>{stopPlay(); $("analysis").hidden=true; $("home").hidden=false; location.hash=""};
const Y=(v,lo,hi)=>52-Math.min(1,Math.max(0,(v-lo)/(hi-lo||1)))*48;  // 56px band; outliers clip to the edge
// Scale per line on the 5th-95th percentile, at least 4 semitones, so one spike does not flatten the rest.
function scale(vals){
  if(!vals.length) return [-2,2];
  const v=[...vals].sort((a,b)=>a-b), q=f=>v[Math.min(v.length-1,Math.floor(f*(v.length-1)))];
  let lo=Math.min(q(.05),0), hi=Math.max(q(.95),0);
  if(hi-lo<4){const m=(hi+lo)/2; lo=m-2; hi=m+2}
  return [lo,hi];
}
function spark(pts,lo,hi,cls){
  const p=pts.filter(q=>q[1]!=null); if(p.length<2) return "";
  const y=v=>Y(v,lo,hi);
  return `<polyline class="${cls}" points="${p.map(([x,v])=>`${(x*100).toFixed(1)},${y(v).toFixed(1)}`).join(" ")}"
    fill="none" vector-effect="non-scaling-stroke"/>`;
}
const TOP=4;  // the most important few first; the rest folded away
function advice(list){
  if(!list.length) return `<div class="ok-line">指摘なし</div>`;
  const li=a=>`<li class="${["slow","fast"].includes(a.kind)?"info":""}">${esc(a.text)}</li>`;
  const more=list.slice(TOP);
  return `<ul class="adv">${list.slice(0,TOP).map(li).join("")}</ul>`+(more.length?
    `<details class="more"><summary>ほか ${more.length} 件</summary><ul class="adv">${more.map(li).join("")}</ul></details>`:"");
}
function ipa(pieces){return pieces.map(([t,bad])=>bad?`<span class="r">${esc(t)}</span>`:esc(t)).join("")}
const fin=f=>f&&f[0]?`${{rise:"上昇",fall:"下降",level:"平ら"}[f[0]]}（${f[1]>0?"+":""}${f[1]}）`:"–";
// Word cells for one analysed line; src says whose audio a click plays ("orig" take or the latest "retake").
function wordsHTML(l,src){
  const vals=l.words.flatMap(w=>[...w.contour,...w.ref_contour]).map(q=>q[1]).filter(v=>v!=null);
  const [lo,hi]=scale(vals);
  return l.words.map((w,k)=>{
    const cls=w.oov?"oov":w.withheld?"held":(w.prob<0.5||w.omitted)?"bad":"";
    const note=w.withheld&&!w.oov?`判定保留（お手本 ${esc(w.tts||"")}）`:w.oov?"辞書外":
      [w.stress?`強勢 ${w.stress}`:"", w.omitted?"抜け?":""].filter(Boolean).join(" · ");
    const p=l.pauses.find(p=>k+1<l.words.length && p.start_ms>=w.e-40 && p.start_ms<=l.words[k+1].s);
    const pz=p?`<span class="pz ${l.bad_pauses.includes(k)?"bad":""}">‖${(p.duration_ms/1000).toFixed(1)}</span>`:"";
    return `<button class="wd ${cls}" data-src="${src}" data-line="${l.line}" data-k="${k}" title="クリックで再生">
      <svg viewBox="0 0 100 56" preserveAspectRatio="none"><line class="mid" x1="0" x2="100" y1="${Y(0,lo,hi).toFixed(1)}" y2="${Y(0,lo,hi).toFixed(1)}" vector-effect="non-scaling-stroke"/>${spark(w.ref_contour,lo,hi,"refc")}${spark(w.contour,lo,hi,l.flat?"flat":"me")}</svg>
      <span class="t">${esc(w.w)}</span><span class="x">${ipa(w.exp)}</span><span class="x h">${ipa(w.heard)}</span>
      <span class="note">${note}</span></button>${pz}`}).join("");
}
const RT={}; let rtLine=null, rtTimer=null;
const selfSrc=(src,l)=>src==="retake"?`/retake-audio?v=${RT[l.line].n}`:`/audio/${encodeURIComponent(AN.id)}`;
async function openAnalysis(id){
  const d=await (await fetch(`/api/analysis/${encodeURIComponent(id)}`)).json(); if(d.error) return alert(d.error);
  AN={id,...d}; const rid=encodeURIComponent(id); for(const k in RT) delete RT[k];
  $("an-title").textContent=id;
  const k=[["総合","total"],["正確さ","accuracy"],["流暢さ","fluency"],["韻律","prosodic"]];
  $("an-scores").innerHTML=k.map(([n,x])=>`<div class="score"><b>${(d.overall[x]*100).toFixed(0)}</b><span>${n}</span></div>`).join("");
  $("an-lines").innerHTML=d.lines.map(l=>{
    const self=`/audio/${rid}|${l.s/1000}|${l.e/1000+0.15}`, model=`/model-for/${rid}/${l.line}|0|`;
    return `<section class="card"><div class="ln-head"><span class="n">${l.line}</span><span class="txt">${esc(l.text)}</span>
      <span class="sc">${l.total!=null?(l.total*100).toFixed(0):"–"}</span></div>
      <div class="ln-meta"><button class="chip lseg" data-src="${self}">▶ 自分</button>
      <button class="chip lseg" data-src="${model}" ${d.model?"":"disabled"}>▶ お手本</button>
      <button class="chip lseg" data-src="${self}" data-then="${model}" ${d.model?"":"disabled"}>▶ 続けて比べる</button>
      <button class="chip retake" data-line="${l.line}">● この文を録り直す</button>
      <span>文末 ${fin(l.final)} · お手本 ${fin(l.ref_final)}</span></div>
      <div class="words">${wordsHTML(l,"orig")}</div>
      ${advice(l.advice)}<div class="rt" id="rt-${l.line}" hidden></div></section>`}).join("");
  $("home").hidden=true; $("analysis").hidden=false; window.scrollTo(0,0); location.hash=id;
}
function renderRetake(line){
  const st=RT[line], l=st.view, orig=AN.lines.find(x=>x.line===line), box=$(`rt-${line}`);
  const pct=v=>v!=null?(v*100).toFixed(0):"–", d=(st.view.total-orig.total)*100;
  const self=`/retake-audio?v=${st.n}|${l.s/1000}|${l.e/1000+0.15}`, model=`/model-for/${encodeURIComponent(AN.id)}/${line}|0|`;
  box.innerHTML=`<div class="rt-head"><b>録り直し</b>
      <span class="trail">元 ${pct(orig.total)} → ${st.scores.map(pct).join(" → ")}</span>
      <span class="delta ${d>=0?"up":"down"}">${d>=0?"+":""}${d.toFixed(0)}</span></div>
    <div class="ln-meta"><button class="chip lseg" data-src="${self}">▶ 今回</button>
      <button class="chip lseg" data-src="${self}" data-then="${model}" ${AN.model?"":"disabled"}>▶ 今回とお手本</button>
      <span>文末 ${fin(l.final)} · お手本 ${fin(l.ref_final)}</span></div>
    <div class="words">${wordsHTML(l,"retake")}</div>${advice(l.advice)}`;
  box.hidden=false;
}
async function retake(btn,line){
  if(rtLine===line){  // second press: stop, then score this one sentence
    clearInterval(rtTimer); btn.disabled=true; btn.textContent="解析中…";
    const r=await post("/api/retake/stop",{});
    rtLine=null; btn.disabled=false; btn.classList.remove("on"); btn.textContent="● もう一度録る";
    document.querySelectorAll(".chip.retake").forEach(b=>b.disabled=false);
    if(r.error){const box=$(`rt-${line}`); box.hidden=false; box.innerHTML=`<div class="err">${esc(r.error)}</div>`; return}
    const st=RT[line]||(RT[line]={scores:[]}); st.view=r.line; st.n=r.n; st.scores.push(r.line.total); renderRetake(line);
    return;
  }
  if(rtLine!==null||S?.recording) return;
  stopPlay();
  const r=await post("/api/retake/start",{rid:AN.id,line}); if(r.error) return alert(r.error);
  rtLine=line; const t0=Date.now(); btn.classList.add("on");
  document.querySelectorAll(".chip.retake").forEach(b=>b.disabled=b!==btn);
  const tick=()=>btn.textContent=`■ 停止 ${fmt((Date.now()-t0)/1000)}`; tick(); rtTimer=setInterval(tick,250);
}
$("an-lines").onclick=e=>{
  const b=e.target.closest("button"); if(!b||!AN) return; const rid=encodeURIComponent(AN.id);
  if(b.classList.contains("retake")) return retake(b,+b.dataset.line);
  if(b.classList.contains("lseg")){const items=[parse(b.dataset.src)]; if(b.dataset.then) items.push(parse(b.dataset.then)); return playSeq(b,items)}
  const line=+b.dataset.line, l=b.dataset.src==="retake"?RT[line].view:AN.lines.find(x=>x.line===line), w=l.words[b.dataset.k], items=[];
  const self=[selfSrc(b.dataset.src,l),Math.max(0,w.s/1000-0.05),w.e/1000+0.08];
  const model=w.ref_s!=null?[`/model-for/${rid}/${line}`,Math.max(0,w.ref_s/1000-0.05),w.ref_e/1000+0.08]:null;
  items.push(self); if(model) items.push(model);  // a word click plays yours, then the model's
  if(items.length) playSeq(b,items);
};
// ---- add / edit scripts: paste prose or Markdown, preview the split, save
let pv=[], pvTimer=null;
function openDlg(title,text,name){
  $("dlg-title").textContent=title; $("src").value=text||""; $("name").value=name||"";
  $("byh").checked=/^#/m.test(text||"") && !name; $("pv").innerHTML=""; pv=[]; $("dlg").showModal(); preview();
}
$("add").onclick=()=>openDlg("原稿を追加","","");
async function preview(){
  const text=$("src").value; if(!text.trim()){$("pv").innerHTML="";pv=[];return}
  const d=await post("/api/scripts/preview",{text,by_headings:$("byh").checked,name:$("name").value||"script"});
  pv=d.parts.map(p=>({...p,on:true}));
  $("pv").innerHTML=pv.length?pv.map((p,i)=>`<div class="part"><div class="row">
    ${pv.length>1?`<input type="checkbox" data-i="${i}" class="on" checked>`:""}
    <input type="text" data-i="${i}" class="sid" value="${esc(p.slide_id)}" aria-label="原稿の名前">
    <span>${p.lines.length} 文${p.title?` · ${esc(p.title)}`:""}</span>
    ${p.exists?`<span class="warn">上書き（前の版は .history に残る）</span>`:""}</div>
    <ol>${p.lines.map(l=>`<li>${esc(l)}</li>`).join("")}</ol></div>`).join("")
    :'<div class="part warn">英文が見つかりませんでした</div>';
}
const later=()=>{clearTimeout(pvTimer); pvTimer=setTimeout(preview,250)};
$("src").oninput=later; $("byh").onchange=preview; $("name").oninput=later;
$("pv").oninput=e=>{const i=+e.target.dataset.i; if(e.target.classList.contains("sid")) pv[i].slide_id=e.target.value;
  if(e.target.classList.contains("on")) pv[i].on=e.target.checked};
$("src").ondragover=e=>{e.preventDefault(); $("src").classList.add("drop")};
$("src").ondragleave=()=>$("src").classList.remove("drop");
$("src").ondrop=async e=>{e.preventDefault(); $("src").classList.remove("drop");
  const f=e.dataTransfer.files[0]; if(!f) return; const t=await f.text(); $("src").value=t;
  if(!$("name").value) $("name").value=f.name.replace(/\.[^.]+$/,"").replace(/[^A-Za-z0-9_-]+/g,"-").replace(/^-+|-+$/g,"").slice(0,64);
  $("byh").checked=/^#/m.test(t); preview()};
$("save").onclick=async e=>{
  e.preventDefault(); const parts=pv.filter(p=>p.on); if(!parts.length) return;
  const r=await post("/api/scripts/save",{parts}); if(r.error) return alert(r.error);
  $("dlg").close(); scriptKey=""; await refresh(); $("script").value=r.saved[0]; prepare(); refresh();
};

// ---- script view: shown read-only; 修正 → edit → 確定 saves (and regenerates changed sentences) or 取消
let SC=null, scSig="", scSaving=false, scEditMode=false;
async function loadScript(force){
  const v=$("script").value; if(!v){$("sc-card").hidden=true; return}
  if(scEditMode) return;  // never replace text that is being edited
  const d=await (await fetch(`/api/scripts/${encodeURIComponent(v)}`)).json(); if(d.error) return;
  const text=d.lines.map(l=>l.text).join(" "), sig=v+"\n"+text;
  if(sig===scSig&&!force) return; scSig=sig; SC={...d,text};
  $("sc-card").hidden=false; $("sc-text").textContent=text;
}
function setEdit(on){
  scEditMode=on; const t=$("sc-text");
  t.contentEditable=on?"plaintext-only":"false"; t.classList.toggle("editing",on); t.setAttribute("aria-readonly",String(!on));
  $("sc-edit").hidden=on; $("sc-ok").hidden=!on; $("sc-cancel").hidden=!on; $("script").disabled=on||!!S?.recording;
  if(on){t.focus(); const r=document.createRange(); r.selectNodeContents(t); r.collapse(false);
    const sel=getSelection(); sel.removeAllRanges(); sel.addRange(r)}
}
$("sc-edit").onclick=()=>{if(SC){$("sc-status").textContent=""; setEdit(true)}};
$("sc-cancel").onclick=()=>{$("sc-text").textContent=SC?.text||""; setEdit(false)};
$("sc-ok").onclick=async()=>{
  const text=$("sc-text").innerText.replace(/\s+/g," ").trim();
  if(text===SC.text){setEdit(false); $("sc-status").textContent="変更なし"; return}
  if(scSaving) return; scSaving=true; $("sc-ok").disabled=true; $("sc-status").textContent="保存中…";
  const r=await post("/api/scripts/edit",{slide_id:SC.slide_id,text});
  scSaving=false; $("sc-ok").disabled=false;
  if(r.error){$("sc-status").textContent=`保存できません: ${r.error}`; return}  // stay in edit mode
  $("sc-status").textContent=`保存しました（${r.lines} 文）。変わった文のお手本を作り直しています`;
  setTimeout(()=>$("sc-status").textContent="",4000);
  setEdit(false); scriptKey=""; await loadScript(true);
};
$("big").onchange=()=>{$("sc-text").classList.toggle("big",$("big").checked); try{localStorage.big=$("big").checked?"1":""}catch{}};
try{if(localStorage.big){$("big").checked=true;$("sc-text").classList.add("big")}}catch{}
setInterval(()=>loadScript(false),2000);  // picks up readiness and edits made elsewhere (ChatGPT, editor)

refresh(); setInterval(refresh,500);
if(location.hash.length>1) openAnalysis(decodeURIComponent(location.hash.slice(1)));
</script></body></html>"""


def main():
    url = f"http://127.0.0.1:{PORT}/"
    threading.Timer(1.0, webbrowser.open, (url,)).start()
    print(f"発音練習レコーダー: {url}  (Ctrl+C で終了)")
    try:
        uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
    finally:
        _stop()  # never leave pw-record running
        if state.get("retake_proc") is not None:
            pron.stop_recording(state["retake_proc"], Path(state["retake_file"]))
        _drop_retake_file()


if __name__ == "__main__":
    main()
