# pronunciation-practice

英語の発表練習を手元の PC で録音・発音評価するアプリ。原稿に対して音・強勢・イントネーション・ポーズを測り、IPA とピッチ曲線で見せ、お手本の音声と語ごとに聴き比べ、1 文ずつ録り直して確かめられる。

*English: a local app for rehearsing English talks. It scores each take against the script with Pronunciada, shows expected vs heard IPA and pitch, plays your words against a local TTS model reading, and lets you re-record single sentences. Audio never leaves your machine.*

**基本はこのアプリだけで完結する。** ChatGPT 連携（MCP と Skill）は任意のサブ機能で、解析結果をもとに直し方を言葉で説明してもらいたいときだけ使う（[docs/chatgpt.md](docs/chatgpt.md)）。

## 始め方

Linux と PipeWire（録音に `pw-record`）、[uv](https://docs.astral.sh/uv/) が必要。

```sh
git clone https://github.com/ryoryoryo3838/pronunciation-practice.git
cd pronunciation-practice
uv sync --extra reference   # お手本の音声が要らなければ uv sync
uv run python gui.py        # http://127.0.0.1:8792/ が開く
```

初回の解析とお手本の生成で、それぞれのモデルを自動で取得する。

## 使っているモデル

- **[Pronunciada](https://github.com/moglearn/pronunciada)**（Apache-2.0）：原稿に対する発音評価。音素・語・文の受容確率、単語の強勢、ピッチ、ポーズ、自由な音素認識を出す。CPU だけで、オフラインで動く。音素認識に ZIPA（CC BY 4.0）を使う。
- **[Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M)**（Apache-2.0、[kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx) で fp32 を使用）：原稿を 1 文ずつ読み上げるお手本の TTS。お手本も同じ Pronunciada で解析し、本人とピッチの形を比べる。任意（`--extra reference`）。

## ドキュメント

- [アプリの使い方](docs/app.md)：原稿の登録・修正、録音、分析画面、1 文の録り直し、CLI
- [解析の中身と限界](docs/analysis.md)：採点の方法、保存する情報、お手本の扱い、強勢判定の妥当性チェック
- [ChatGPT 連携（任意）](docs/chatgpt.md)：Secure MCP Tunnel、コーチの Skill、MCP ツール
- [開発](docs/development.md)：構成、テスト
- [初期の設計書](docs/design.md)

## ライセンス

[Apache License 2.0](LICENSE)。例外と第三者の権利は [NOTICE](NOTICE)。

- `tests/fixtures/` の音声は speechocean762 の抜粋で CC BY 4.0。
- お手本の機能（`--extra reference`）は GPL-3.0 のライブラリ（phonemizer、espeak-ng）を使う。このリポジトリには含めず、本体はなくても動く。
