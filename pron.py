"""Local practice CLI: record a talk against its script, analyse it, list past runs."""

import argparse
import shutil
import signal
import subprocess
import sys
from datetime import date
from pathlib import Path

import store


def _register(con, script_path: str, slide: str | None, session: str | None, ext: str = ".wav") -> tuple[str, str, str]:
    script = Path(script_path).read_text(encoding="utf-8")
    slide = slide or Path(script_path).stem
    rid = store.new_id(con, slide)
    audio = rid + ext
    with con:
        con.execute("INSERT INTO recordings VALUES (?, ?, ?, ?, ?, ?, 'recording', ?, ?, NULL, NULL)",
                    (rid, session or date.today().isoformat(), slide,
                     store.revision(script), script, audio, store.now(), store.now()))
    return rid, script, audio


def _analyse(con, rid: str, script: str, audio: str):
    import analyze  # heavy imports only when analysing

    store.set_status(con, rid, "analyzing")
    print(f"[{rid}] analysing...", file=sys.stderr)
    try:
        result = analyze.analyze(str(store.DATA / "audio" / audio), script)
    except Exception as e:  # keep the row so the MCP reports a failure instead of an older result
        store.set_status(con, rid, "failed", error=f"{type(e).__name__}: {e}")
        sys.exit(f"[{rid}] analysis failed: {type(e).__name__}: {e}")
    store.set_status(con, rid, "completed", result)
    print(f"[{rid}] done: overall {result['overall']}", file=sys.stderr)
    return result


def start_recording(path: Path) -> subprocess.Popen:
    return subprocess.Popen(["pw-record", "--rate", "48000", "--channels", "1", "--format", "s16", str(path)])


def stop_recording(proc: subprocess.Popen, path: Path) -> bool:
    proc.send_signal(signal.SIGINT)
    proc.wait(timeout=10)
    # pw-record exits 1 on SIGINT even after finalising the file, so judge by the file instead.
    return path.exists() and path.stat().st_size > 44


def cmd_record(a):
    con = store.connect()
    rid, script, audio = _register(con, a.script, a.slide, a.session)
    path = store.DATA / "audio" / audio
    proc = start_recording(path)
    try:
        input(f"● REC [{rid}]  Enter で停止 ")
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        ok = stop_recording(proc, path)
    if not ok:
        store.set_status(con, rid, "failed", error=f"pw-record exited with {proc.returncode}")
        sys.exit(f"recording failed (pw-record {proc.returncode})")
    _analyse(con, rid, script, audio)


def cmd_analyze(a):
    """Import an existing audio file, or rerun analysis of a stored recording with --id."""
    con = store.connect()
    if a.id:
        row = con.execute("SELECT script, audio_file FROM recordings WHERE id = ?", (a.id,)).fetchone()
        if row is None:
            sys.exit(f"no recording {a.id}")
        return _analyse(con, a.id, row["script"], row["audio_file"])
    if not (a.audio and a.script):
        sys.exit("give AUDIO and SCRIPT, or --id")
    rid, script, audio = _register(con, a.script, a.slide, a.session, Path(a.audio).suffix)
    shutil.copyfile(a.audio, store.DATA / "audio" / audio)
    _analyse(con, rid, script, audio)


def cmd_list(a):
    for r in store.connect().execute("SELECT id, session_id, status, created_at, error FROM recordings "
                                     "ORDER BY created_at DESC, rowid DESC LIMIT ?", (a.limit,)):
        print(r["id"], r["session_id"], r["status"], r["created_at"], r["error"] or "", sep="\t")


def main():
    p = argparse.ArgumentParser(prog="pron")
    sub = p.add_subparsers(required=True)
    r = sub.add_parser("record", help="record from the default microphone (pw-record) and analyse")
    r.add_argument("script", help="script file: one sentence per line, '#' lines ignored")
    an = sub.add_parser("analyze", help="analyse an existing audio file, or --id to rerun")
    an.add_argument("audio", nargs="?")
    an.add_argument("script", nargs="?")
    an.add_argument("--id")
    for q in (r, an):
        q.add_argument("--slide", help="slide id (default: script file name)")
        q.add_argument("--session", help="practice session id (default: today)")
    ls = sub.add_parser("list")
    ls.add_argument("--limit", type=int, default=20)
    r.set_defaults(f=cmd_record)
    an.set_defaults(f=cmd_analyze)
    ls.set_defaults(f=cmd_list)
    a = p.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
