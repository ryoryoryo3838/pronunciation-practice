"""SQLite store shared by the recorder (writer) and the MCP server (read-only)."""

import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

DATA = Path(os.environ.get("PRON_DATA_DIR", Path(__file__).parent / "data"))
DB = DATA / "pron.db"
SLIDES = Path(os.environ.get("PRON_SLIDES_DIR", Path(__file__).parent / "slides"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS recordings (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  slide_id TEXT NOT NULL,
  reference_revision TEXT NOT NULL,
  script TEXT NOT NULL,
  audio_file TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('recording', 'analyzing', 'completed', 'failed')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  result TEXT,
  error TEXT
);
"""


def script_lines(text: str) -> list[str]:
    """One scored segment per non-empty line; '#' lines are notes, not speech."""
    return [s.strip() for s in text.splitlines() if s.strip() and not s.lstrip().startswith("#")]


def scripts() -> dict[str, Path]:
    """slide_id -> script file; the only names tools accept, so no path is built from tool input."""
    return {p.stem: p for p in sorted(SLIDES.glob("*.txt"))}


SLIDE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
MAX_SCRIPT_CHARS = 50_000


def save_script(slide_id: str, text: str) -> str:
    """Write slides/<slide_id>.txt, keeping any previous version in slides/.history; returns the revision."""
    if not SLIDE_ID.fullmatch(slide_id):
        raise ValueError("slide_id: letters, digits, '-' and '_' only (max 64)")
    if len(text) > MAX_SCRIPT_CHARS or not script_lines(text):
        raise ValueError(f"script must have at least one line and at most {MAX_SCRIPT_CHARS} characters")
    path = SLIDES / f"{slide_id}.txt"
    SLIDES.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") != text:
        old = SLIDES / ".history" / f"{slide_id}.{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.txt"
        old.parent.mkdir(exist_ok=True)
        path.replace(old)
    tmp = path.with_name(f".{slide_id}.{os.getpid()}.tmp")
    tmp.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    tmp.replace(path)
    return revision(path.read_text(encoding="utf-8"))


def revision(script: str) -> str:
    import hashlib

    return hashlib.sha256(script.encode()).hexdigest()[:8]


def stage(slide_id: str):
    """Mark the script the user is about to practise; the recorder page selects it."""
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "next.json").write_text(json.dumps({"slide_id": slide_id, "at": now()}), encoding="utf-8")


def staged() -> dict | None:
    path = DATA / "next.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def connect(readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    else:
        (DATA / "audio").mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(DB)
        con.executescript(SCHEMA)
    con.row_factory = sqlite3.Row
    return con


def new_id(con: sqlite3.Connection, slide_id: str) -> str:
    # max + 1, not count + 1: recordings can be deleted, and ids must never be reused
    ids = [r[0] for r in con.execute("SELECT id FROM recordings WHERE slide_id = ?", (slide_id,))]
    n = max((int(i.rsplit("-", 1)[1]) for i in ids if i.rsplit("-", 1)[1].isdigit()), default=0)
    return f"{slide_id}-{n + 1:03d}"


def set_status(con: sqlite3.Connection, rid: str, status: str, result: dict | None = None, error: str | None = None):
    with con:
        con.execute("UPDATE recordings SET status = ?, result = ?, error = ?, updated_at = ? WHERE id = ?",
                    (status, json.dumps(result, ensure_ascii=False) if result else None, error, now(), rid))


def delete(con: sqlite3.Connection, rid: str) -> bool:
    """Remove a recording row and its audio file; returns False if there was no such recording."""
    row = con.execute("SELECT audio_file FROM recordings WHERE id = ?", (rid,)).fetchone()
    if row is None:
        return False
    with con:
        con.execute("DELETE FROM recordings WHERE id = ?", (rid,))
    (DATA / "audio" / row["audio_file"]).unlink(missing_ok=True)
    return True


# Abbreviations that end with "." but do not end a sentence.
_ABBR = re.compile(r"\b(?:e\.g|i\.e|et al|etc|vs|cf|approx|Dr|Mr|Ms|Mrs|Prof|Fig|Figs|Eq|Sec|No|Vol|pp)\.$", re.I)
_MD = [(re.compile(r"\\\[[^\]]*\\\]|\[(?:pause|PAUSE)[^\]]*\]"), ""),  # stage directions like \[pause\]
       (re.compile(r"!\[[^\]]*\]\([^)]*\)"), ""), (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),
       (re.compile(r"[*_`]{1,3}"), ""), (re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+"), ""),
       (re.compile(r"〔[^\x00-\x7F〕]*[:：]\s*"), ""), (re.compile(r"[〔〕]"), "")]  # 〔省略可: …〕 style notes


def split_sentences(text: str) -> list[str]:
    """Prose (or Markdown) -> one sentence per item; paragraphs may be hard-wrapped."""
    out = []
    for para in re.split(r"\n\s*\n", text):
        lines = []
        for raw in para.splitlines():
            line = raw.strip()
            if not line or line.startswith(("#", ">", "|", "<!--")):
                continue
            for pat, rep in _MD:
                line = pat.sub(rep, line)
            lines.append(line.strip())
        buf = ""
        for tok in re.split(r"(?<=[.!?])\s+|(?<=[。！？])\s*", " ".join(lines)):
            buf = f"{buf} {tok}".strip()
            if not _ABBR.search(buf):
                out.append(buf)
                buf = ""
        if buf:
            out.append(buf)
    return [s for s in out if _english(s)]


def _english(s: str) -> bool:
    """Mostly Latin letters: drops Japanese notes mixed into a talk script."""
    letters = [c for c in s if c.isalpha()]
    # a Japanese lead-in (「…」と答える) marks a note about the talk, not a line of it
    return bool(letters) and letters[0].isascii() and sum(c.isascii() for c in letters) / len(letters) >= 0.7


def slug(title: str, n: int) -> str:
    """'1. Cover（約0:36）' -> '01-cover'; the heading's own number wins over the position n."""
    words = re.findall(r"[A-Za-z0-9]+", re.sub(r"[（(].*?[）)]", "", title))
    if words and words[0].isdigit():
        n, words = int(words[0]), words[1:]
    return f"{n:02d}-" + ("-".join(w.lower() for w in words)[:48].strip("-") or "part")


def one_sentence_per_line(text: str) -> str:
    """Split only lines that hold several sentences; lines already one sentence (and # notes) stay as they are."""
    out = []
    for line in text.splitlines():
        out += [line] if not line.strip() or line.lstrip().startswith("#") else split_sentences(line) or [line]
    return "\n".join(out) + "\n"


def split_document(text: str, by_headings: bool) -> list[dict]:
    """Markdown with headings -> one script per lowest-level heading that has sentences under it."""
    if not by_headings:
        return [{"title": None, "lines": split_sentences(text)}]
    parts, title, body = [], None, []
    for line in text.splitlines():
        if re.match(r"^#{1,6}\s", line):
            parts.append((title, body))
            title, body = line.lstrip("#").strip(), []
        else:
            body.append(line)
    parts.append((title, body))
    return [{"title": t, "lines": s} for t, b in parts if (s := split_sentences("\n".join(b)))]
