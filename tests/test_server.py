"""Self-check against a temporary store: pending, completed and failed rows as the MCP sees them."""

import asyncio
import json
import os
import sys
import tempfile

os.environ["PRON_DATA_DIR"] = tempfile.mkdtemp()
os.environ["PRON_SLIDES_DIR"] = tempfile.mkdtemp()
open(os.path.join(os.environ["PRON_SLIDES_DIR"], "cover.txt"), "w").write("# note\nLet me begin.\n")

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
import store  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from server import mcp  # noqa: E402

WORD = {"word_index": 0, "word": "register", "line": 1, "start_ms": 0, "prob": 0.2, "omitted": False,
        "expected_ipa": "ˈɹɛdʒɪstɜ˞", "heard_ipa": "ɹɛdʒɪstə˞", "free_ipa": "ɹɛdʒɪsthə˞",
        "expected_stress_syllable": 1, "estimated_stress_syllable": 2, "stress_confidence": 0.9,
        "stress_posterior": [0.0, 1.0, 0.0], "warnings": [], "syllables": [],
        "phones": [{"ipa": "ɹ", "heard": "ɹ", "prob": 0.9, "evidence": {"llr_sub": 0.1}}]}
OOV = {**WORD, "word_index": 1, "word": "NASA", "warnings": ["not in dictionary: expected pronunciation is a guess"]}
LINE = {"line": 1, "text": "register NASA", "start_ms": 0, "prob": 0.2, "scores": {}, "free_ipa": "", "intonation": {},
        "tips": [], "oov_words": ["nasa"], "audio": {"issues": []}, "contour_st": [[0, 1.0]],
        "delivery": {"articulation_rate": 5.0, "pauses": [], "extra_speech": []}, "words": [WORD, OOV]}
RESULT = {"duration_ms": 1000, "overall": {"total": 0.8}, "speaker": {}, "engine_versions": {}, "lines": [LINE]}


async def call(name, args):
    return json.loads((await mcp.call_tool(name, args)).content[0].text)


async def main():
    assert (await call("get_latest_analysis", {}))["status"] == "none"
    con = store.connect()
    for i, (status, result, err) in enumerate([("completed", RESULT, None), ("analyzing", None, None)]):
        con.execute("INSERT INTO recordings VALUES (?, 's', 'cover', 'r', 'x', 'a.wav', ?, ?, ?, ?, ?)",
                    (f"cover-00{i + 1}", status, f"2026-11-25T10:0{i}:00", "t", json.dumps(result), err))
    con.commit()

    tools = await mcp.list_tools()
    # only prepare_practice writes (a script file, keeping old versions); everything else is read-only
    assert {t.name for t in tools if not t.annotations.read_only_hint} == {"prepare_practice"} and len(tools) == 8
    assert not next(t for t in tools if t.name == "prepare_practice").annotations.destructive_hint
    latest = await call("get_latest_analysis", {})
    assert latest["status"] == "pending" and latest["recording_id"] == "cover-002", latest  # never an older result
    done = await call("get_analysis", {"recording_id": "cover-001"})
    assert done["flagged_words"][0]["estimated_stress_syllable"] == 2 and "audio_file" not in done
    assert [w["word"] for w in done["flagged_words"]] == ["register"]  # out-of-dictionary words (guessed pronunciation) are not flagged
    line = await call("get_line_detail", {"recording_id": "cover-001", "line": 1})
    assert line["contour_st"] and "evidence" not in line["words"][0]["phones"][0]
    assert (await call("get_line_detail", {"recording_id": "cover-001", "line": 2}))["status"] == "not_found"
    assert (await call("get_word_detail", {"recording_id": "cover-001", "word_index": 0}))["word"] == "register"
    assert (await call("get_word_detail", {"recording_id": "cover-001", "word_index": 5}))["status"] == "not_found"
    assert (await call("get_word_detail", {"recording_id": "cover-002", "word_index": 0}))["status"] == "analyzing"
    assert [r["recording_id"] for r in await call_list()] == ["cover-002", "cover-001"]
    script = await call("get_script", {"slide_id": "cover"})
    assert [line["text"] for line in script["lines"]] == ["Let me begin."]
    try:  # tool input never becomes a path: only names found in the slides folder are accepted
        await mcp.call_tool("get_script", {"slide_id": "../cover"})
        raise AssertionError("traversal accepted")
    except ToolError:
        pass
    sent = await call("prepare_practice", {"slide_id": "rq1", "text": "First, the data.\n"})
    assert sent["lines"] == ["First, the data."] and store.staged()["slide_id"] == "rq1"
    await call("prepare_practice", {"slide_id": "rq1", "text": "First, the data matters.\n"})
    assert len(list((store.SLIDES / ".history").glob("rq1.*.txt"))) == 1  # the earlier version is kept
    for bad in ({"slide_id": "../rq1", "text": "x"}, {"slide_id": "rq1", "text": "# only a note"}):
        try:
            await mcp.call_tool("prepare_practice", bad)
            raise AssertionError(f"accepted {bad}")
        except ToolError:
            pass

    assert store.delete(con, "cover-001") and not store.delete(con, "cover-001")
    assert store.new_id(con, "cover") == "cover-003"  # ids are never reused after a delete
    print("ok")


async def call_list():
    r = await mcp.call_tool("list_analyses", {})
    return r.structured_content["result"] if r.structured_content else json.loads(r.content[0].text)


asyncio.run(main())
