# claude-code-jev-hooks

Claude Code の Bash 実行前と Stop 時に Jev 判定を挟むフック集です。
危険なコマンドは実行前に確認し、完了報告には検査の証拠を求めます。
Bash ゲートの Jev 層は既定でシャドーモードです。判定を記録するだけで、確認には回しません。
Stop フックは既定で有効です。条件に当たると exit 2 で止め、検査を促します。

<!-- QIITA_URL -->

## 前提

- Python 3 標準ライブラリのみを使います。
- macOS / Linux の Claude Code を想定しています。
- TypeSafe AI の API キーが必要です。

## 導入

1. このリポジトリを clone します。

```sh
git clone https://github.com/nons-inc/claude-code-jev-hooks.git /path/to/claude-code-jev-hooks
chmod +x /path/to/claude-code-jev-hooks/jev-gate.py
chmod +x /path/to/claude-code-jev-hooks/jev-stop-check.py
chmod +x /path/to/claude-code-jev-hooks/jev-score.sh
```

2. API キーを置きます。

```sh
mkdir -p ~/.config/jev-hooks
printf 'TYPESAFE_API_KEY=...\n' > ~/.config/jev-hooks/typesafe.env
chmod 600 ~/.config/jev-hooks/typesafe.env
```

3. `settings.example.json` を参考に、対象プロジェクトの `.claude/settings.json` に `hooks` を貼ります。

4. 発火を確認します。

```sh
python3 /path/to/claude-code-jev-hooks/jev-gate.py --explain "git push --force origin main"
python3 /path/to/claude-code-jev-hooks/jev-gate.py --explain "ls"
```

5. 環境変数で有効化・停止します。

```sh
export JEV_GATE_ENFORCE=1    # Bash ゲートのシャドーを解除し、閾値越えを確認に回す
export JEV_GATE_DISABLE=1    # Bash ゲートの緊急停止
export JEV_STOP_DISABLE=1    # Stop フックの緊急停止
```

## 3本の説明

### jev-gate.py

PreToolUse / Bash 用です。
層 A は `rm -r/-f`、`git push --force`、`sudo` などをコードだけで ask にします。
層 C は skip パス、denylist、メール、11桁電話を Jev に送らず通します。
層 B は読み取り専用コマンドをログなしで通します。
層 D は Jev の `dangerous >= 0.70` などで判定し、エラー時は fail-open します。

### jev-stop-check.py

Stop 用です。
最後のユーザー発言以降のファイル変更と検査コマンドを transcript から見ます。
変更あり・検査なしのときだけ Jev に4問を聞きます。
`claims_done > 0.70` かつ `verification_applies > 0.5` で停止候補にします。
キーなし、タイムアウト、応答不正、上限到達時は fail-open します。

### jev-score.sh

TypeSafe AI の Jev API を呼ぶ薄いラッパーです。
state と questions を受け取り、`score` / `choice` / `noul` の答えを返します。
既定モデルは `jev-latest`、HTTP timeout は60秒です。
呼び出しメタ情報を `jev-calls.log` に追記します。
API キーが無いと非0で終了し、呼び出し元のフックは fail-open します。

## 環境変数

| 変数 | 既定 | 説明 |
|---|---:|---|
| `TYPESAFE_API_KEY` | なし | TypeSafe AI の API キー |
| `JEV_HOOKS_HOME` | `~/.cache/jev-hooks` | ログと Stop 状態の保存先 |
| `JEV_MODEL` | `jev-latest` | Jev モデル |
| `JEV_USDJPY` | `150` | 概算円コスト計算用 |
| `JEV_GATE_DISABLE` | なし | `1` なら Bash ゲートを停止 |
| `JEV_GATE_ENFORCE` | なし | `1` なら shadow ではなく ask を返す |
| `JEV_GATE_THRESHOLD` | `0.70` | `dangerous` の閾値 |
| `JEV_GATE_SKIP_PATHS` | `/clients/` | Jev に送らないパス。カンマ区切り |
| `JEV_GATE_SKIP_RE` | なし | Jev に送らない追加正規表現 |
| `JEV_STOP_DISABLE` | なし | `1` なら Stop チェックを停止 |
| `JEV_STOP_THRESHOLD` | `0.70` | `claims_done` の閾値 |
| `JEV_STOP_SKIP_RE` | `/clients/` | 変更ファイルのパス送信を避ける正規表現 |
| `JEV_STOP_CHECK_RE` | なし | 検査コマンドに足す正規表現 |
| `JEV_STOP_MAX_BLOCKS` | `3` | 1セッションで止める回数の上限 |

## ログ

既定では `~/.cache/jev-hooks/` に出ます。
`JEV_HOOKS_HOME` を指定すると、そのディレクトリに出ます。

| ファイル | 列 |
|---|---|
| `jev-gate.log` | 時刻、モード、層、判定、確率4つ、入力トークン、秒、円、コマンド先頭60字、応答モデル、閾値 |
| `jev-stop.log` | 時刻、イベント、セッション、変更数、検査数、判定、4問の確率、応答モデル、閾値 |
| `jev-calls.log` | 時刻、要求モデル、応答モデル、入力トークン、質問数、秒、円、min_conf、state名、questions名 |

## 注意

- Jev は米国の外部 API に state を送ります。
- 顧客情報は `denylist.txt` と skip パスで送らないようにしてください。
- Bash ゲートの Jev 層は既定でシャドーモードです。実際に ask で確認に回すには `JEV_GATE_ENFORCE=1` が必要です。
- Stop フックは `exit 2` で検査を促しますが、エラー時は通します。

## ライセンス

MIT。Copyright (c) 2026 Nons Inc.
