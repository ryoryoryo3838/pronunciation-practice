"""Self-check of turning pasted prose / Markdown into one-sentence-per-line scripts."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import store  # noqa: E402

assert store.split_sentences("The results, e.g. in Table 2, look clear. Others (Lee et al.) disagree!\n"
                             "This sentence is hard\nwrapped? Yes. \\[pause\\] **Bold** text.") == [
    "The results, e.g. in Table 2, look clear.", "Others (Lee et al.) disagree!",
    "This sentence is hard wrapped?", "Yes.", "Bold text."]
assert store.split_sentences("導入で要旨を伝える。Then English. 日本語の注記（LLM）です。") == ["Then English."]
assert store.split_sentences("知らないことは「We have not tested that in this study」と答えます。") == []
assert store.split_sentences("〔省略可: This part is optional.〕") == ["This part is optional."]
assert store.split_sentences("It works. 〔省略可: It is optional.〕") == ["It works.", "It is optional."]

doc = "# Talk\n\n## 1. Cover（約0:36）\nThank you, chair. Let me begin.\n\n## メモ\n日本語だけ。\n\n## 2. Outline\nHere is the plan.\n"
parts = store.split_document(doc, by_headings=True)
assert [(store.slug(p["title"], i), p["lines"]) for i, p in enumerate(parts, 1)] == [
    ("01-cover", ["Thank you, chair.", "Let me begin."]), ("02-outline", ["Here is the plan."])]
assert store.split_document(doc, by_headings=False)[0]["lines"][0] == "Thank you, chair."

# From ChatGPT: only lines holding several sentences are split; notes and single sentences stay.
assert store.one_sentence_per_line("# Method\nFirst, the data. Then the model.\nThank you, chair\n") == \
    "# Method\nFirst, the data.\nThen the model.\nThank you, chair\n"
print("ok")
