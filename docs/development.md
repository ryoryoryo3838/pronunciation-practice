# 開発

## 構成

| ファイル | 役割 |
|---|---|
| `gui.py` | ブラウザで開くアプリ（Starlette / Uvicorn、127.0.0.1 のみ） |
| `pron.py` | CLI（録音・取り込み・再解析・一覧） |
| `analyze.py` | Pronunciada による 2 段の採点と、お手本の付与 |
| `reference.py` | お手本の TTS（Kokoro-82M via kokoro-onnx、任意） |
| `store.py` | SQLite・原稿ファイル・原稿の分割 |
| `server.py` | MCP サーバー（任意。stdio / Streamable HTTP） |
| `skills/pronunciation-coach/` | ChatGPT のコーチ Skill |
| `deploy/` | Secure MCP Tunnel のセットアップと systemd ユニット |

## テスト

```sh
uv run python tests/test_server.py    # MCP: pending / failed / completed の返し方、書き込みツール
uv run python tests/test_analyze.py   # 解析: 2 段採点、時刻、強勢、雑音・原稿違いの拒否、お手本
uv run python tests/test_scripts.py   # 原稿: 文への分割、見出しごとの分割
```

`tests/fixtures/` の音声は speechocean762（CC BY 4.0）。出典は [ATTRIBUTION.md](../tests/fixtures/ATTRIBUTION.md)。

## 注意

- 解析は Pronunciada の内部キャッシュ（`SentenceResult._pronunciada`）を読む。公開 API ではないので、Pronunciada を更新したら `tests/test_analyze.py` で確かめる。
- MCP サーバーをローカルの HTTP で試すときは `uv run python server.py`（`http://127.0.0.1:8790/mcp`）。
- MCP サーバーのコードを変えたら `systemctl --user restart pron-tunnel`。
