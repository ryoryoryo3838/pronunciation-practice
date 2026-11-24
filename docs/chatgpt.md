# ChatGPT 連携（任意）

アプリだけで練習は完結する。ここは、ChatGPT に解析結果を読ませて「何を優先して、どう直すか」を言葉で説明させたい場合だけの手順。

## できること

- 録音のあと「何を直せばいい？」「イントネーションどうだった？」と聞くと、コーチの Skill が直す点を最大 3 つに絞り、計測値を引いて直し方と練習方法を説明する。Live（音声会話）でも使える。
- 「次は RQ1 のパートを練習する」と原稿を渡すと、ChatGPT が `prepare_practice` で PC に送り、アプリがその原稿を選ぶ。

## 現状

| 項目 | 状態 |
|---|---|
| Secure MCP Tunnel 経由で ChatGPT に接続 | 手順は公式資料どおり。個人の Pro アカウントでの動作は**未検証** |
| Live からの呼び出し | **未検証** |
| コーチの Skill | ChatGPT 上での動作は**未検証** |

公式資料では、自作 MCP の接続は Business / Enterprise / Edu でフル機能、Pro では read/fetch 権限に限って可能（[Connecting custom MCP servers in ChatGPT](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt)）。登録は Web 版のみ。

## プライバシー

ChatGPT に渡るのは解析結果だけで、音声ファイルやローカルのパスは返さない。ただし解析結果には**原稿の文が含まれる**。未発表の内容を扱うときは所属先の規則を確認すること。

## Secure MCP Tunnel でつなぐ

サーバーは手元でしか動かず、公開 URL は作らない。OpenAI の [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) を使い、手元から外向きの接続だけでつなぐ。

1. **Tunnel と API キーを作る**（ブラウザ）
   - https://platform.openai.com/settings/organization/tunnels で tunnel を作り、`tunnel_` で始まる ID を控える。
   - https://platform.openai.com/settings/organization/api-keys で API キーを作る（Tunnels の Read と Use 権限）。
2. **tunnel-client を入れる**：[openai/tunnel-client](https://github.com/openai/tunnel-client/releases) のリリースから自分の環境のものを取り、`SHA256SUMS.txt` で照合して PATH に置く。
3. **セットアップスクリプトを実行する**

   ```sh
   deploy/tunnel-setup.sh tunnel_xxxxxxxx
   ```

   API キーの入力を求められる（表示されない）。キーは `~/.config/pronunciation-practice/tunnel.env` に権限 600 で保存され、リポジトリには入らない。systemd のユーザーサービス `pron-tunnel` が起動する。状態は http://127.0.0.1:8791/ui 。止めるときは `systemctl --user disable --now pron-tunnel`。コードを更新したら `systemctl --user restart pron-tunnel`。
4. **ChatGPT に登録する**（Web 版のみ。サービスが動いているあいだに行う）
   - chatgpt.com → Plugins → ＋ → **Add custom MCP server**（画面によっては Settings → Connectors）。名前は `pronunciation-practice` など
   - Connection で **Tunnel** を選び、1 の tunnel を指定する。認証はなし。
   - 警告を確認してチェックを入れ、**Create as a plugin**。
5. **確かめる**：通常のチャットで「pronunciation-practice で最新の発音分析を取得して、call_number を教えて」と頼む。次に Live で同じことを話しかけ、番号が 1 つ増えて返ってくれば Live からも呼べている。

Tunnel を選べない場合は、通常のチャットで結果を取得してから Live で続きを話す。認証なしの公開 URL（ngrok、Tailscale Funnel など）で出すと、URL を知る誰でも原稿の文を含む結果を読めるので避ける。

## コーチの Skill を入れる

```sh
cd skills && zip -r pronunciation-coach.zip pronunciation-coach
```

ChatGPT の Skills → Create → Upload from your computer で zip をアップロードする。図はアプリの分析画面で見るので、Skill は図を作らない。

- 直す点を最大 3 つに絞る（内容語の強勢 → 取り違えにつながる音 → 要所のイントネーションと区切り、の順）。
- それぞれ計測値を引いて説明し、直し方と練習方法、アプリのどの文・どの語で聴き比べて録り直すかまで案内する。
- イントネーションは、お手本とのピッチの差（計測値）と、焦点や文末についての解釈（提案）を分けて述べる。
- 練習ごとの伸びを比べる。言いにくい表現の言い換えは提案にとどめ、同意を得てから原稿を送る。
- 辞書にない語、判定保留の語、抜けた語（言い換えの可能性）は誤りとして扱わない。

## MCP ツール

`prepare_practice` を除いてすべて読み取り専用（`readOnlyHint`）。録音の開始・停止・削除は手元のアプリだけが行う。

| ツール | 内容 |
|---|---|
| `get_latest_analysis(session_id?)` | 最新の録音。未解析なら `pending`、失敗なら `failed` を返し、古い結果で代用しない。`call_number` は呼び出しごとに増え、ログと照合できる |
| `get_analysis(recording_id)` | 全体スコア、行ごとの指摘・抑揚の要約、問題のある単語 |
| `list_analyses(session_id?, limit)` | 過去の練習の一覧 |
| `get_line_detail(recording_id, line)` | 1 行のピッチ曲線、ポーズ、リズムの特徴量、全指摘、語・音素ごとの IPA、お手本 |
| `get_word_detail(recording_id, word_index)` | 1 語の音素・音節・強勢・ピッチの詳細 |
| `list_scripts()` | 原稿の一覧と、お手本の準備ができた文の数 |
| `get_script(slide_id)` | 原稿の各文と、お手本の IPA・文末の上昇下降・語ごとのピッチ |
| `prepare_practice(slide_id, text)` | **唯一の書き込みツール**。次に話すパートの原稿を PC に保存し（1 行に複数の文があれば分ける）、アプリで選択させ、お手本の生成を始める。同名の原稿は古い版を `slides/.history/` に残す。Pro は公式資料上 read/fetch のみなので、**使えない可能性が高い** |

録音にはその時点の原稿の全文と版（`reference_revision`）が残る。どの録音をどの原稿で比べるかは ChatGPT が判断する。MCP サーバーを常駐させていると、`slides/` に置いた原稿のお手本も数秒以内に作り始める。
