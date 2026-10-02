#!/usr/bin/env bash
# jev-score.sh — Jev（TypeSafe AI）判定APIの薄い呼び出し関数
#
# 使い方:
#   /path/to/claude-code-jev-hooks/jev-score.sh STATE_FILE QUESTIONS_FILE [--model M] [--min-confidence 0.5]
#                                                         [--out DIR] [--log FILE] [--raw] [--dry-run] [--timeout SEC]
#
#   STATE_FILE      判断材料。拡張子 .json なら JSON として、それ以外は文字列として送る
#   QUESTIONS_FILE  質問の JSON オブジェクト（name → {type: score|choice|noul, instructions, criteria}）
#                   criteria は score=低い順の配列・choice=名前と説明のオブジェクト・noul={"true":…,"false":…}
#   --model         既定 $JEV_MODEL または jev-latest。閾値を調整したらここで固定する
#   --min-confidence choice の信頼度がこれ未満なら「人が決める」と表示する。既定 0.5
#   --out DIR       送受信 JSON を DIR/<時刻>_request.json / _response.json に保存する
#   --log FILE      メタ情報だけを1行追記する。既定 ~/.cache/jev-hooks/jev-calls.log
#   --raw           要約表を出さず応答 JSON をそのまま出す
#   --dry-run       送らずに組み立てた request JSON を出す
#   --timeout SEC   HTTP の待ち秒。既定 60。フックから呼ぶときは短くする
#
# 🔴 state は米国の外部 API に送られる。顧客案件は社名・人名・金額をマスクしてから渡す
# キーは ~/.config/jev-hooks/typesafe.env（TYPESAFE_API_KEY）
set -euo pipefail

if [[ -z "${TYPESAFE_API_KEY:-}" && -f "$HOME/.config/jev-hooks/typesafe.env" ]]; then
  set -a; . "$HOME/.config/jev-hooks/typesafe.env"; set +a
fi

exec python3 - "$@" <<'PY'
import argparse, datetime, json, os, pathlib, sys, time, urllib.request, urllib.error

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
USD_PER_MTOK = 0.042
USDJPY = float(os.environ.get("JEV_USDJPY", "150"))
DEFAULT_LOG = pathlib.Path(
    os.environ.get("JEV_HOOKS_HOME", pathlib.Path.home() / ".cache" / "jev-hooks")
).expanduser() / "jev-calls.log"

p = argparse.ArgumentParser(add_help=True)
p.add_argument("state_file")
p.add_argument("questions_file")
p.add_argument("--model", default=os.environ.get("JEV_MODEL", "jev-latest"))
p.add_argument("--min-confidence", type=float, default=0.5)
p.add_argument("--out")
p.add_argument("--log", default=str(DEFAULT_LOG))
p.add_argument("--raw", action="store_true")
p.add_argument("--dry-run", action="store_true")
p.add_argument("--timeout", type=float, default=60)
a = p.parse_args()

sp = pathlib.Path(a.state_file)
state = json.loads(sp.read_text()) if sp.suffix == ".json" else sp.read_text().strip()
questions = json.loads(pathlib.Path(a.questions_file).read_text())
if not isinstance(questions, dict) or not questions:
    sys.exit("questions は空でないJSONオブジェクトにしてください")
req = {"model": a.model, "state": state, "questions": questions}
body = json.dumps(req, ensure_ascii=False).encode()

if a.dry_run:
    print(json.dumps(req, ensure_ascii=False, indent=1)); sys.exit(0)

key = os.environ.get("TYPESAFE_API_KEY")
if not key:
    sys.exit("TYPESAFE_API_KEY がありません。~/.config/jev-hooks/typesafe.env を確認してください")

r = urllib.request.Request(ENDPOINT, data=body, method="POST",
                           headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
t0 = time.time()
try:
    with urllib.request.urlopen(r, timeout=a.timeout) as resp:
        res = json.load(resp)
except urllib.error.HTTPError as e:
    sys.exit(f"HTTP {e.code}: {e.read().decode(errors='replace')[:500]}")
except Exception as e:
    sys.exit(f"接続失敗: {type(e).__name__}: {e}")
elapsed = time.time() - t0

usage = res.get("usage", {})
tok_in = usage.get("input_tokens", 0)
cost_yen = tok_in * USD_PER_MTOK / 1e6 * USDJPY
stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")

if a.out:
    od = pathlib.Path(a.out); od.mkdir(parents=True, exist_ok=True)
    (od / f"{stamp}_request.json").write_text(json.dumps(req, ensure_ascii=False, indent=1))
    (od / f"{stamp}_response.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))

if a.log:
    lp = pathlib.Path(a.log); lp.parent.mkdir(parents=True, exist_ok=True)
    with lp.open("a") as f:
        f.write("\t".join([stamp, a.model, res.get("model", "?"), f"in={tok_in}", f"q={len(questions)}",
                           f"{elapsed:.2f}s", f"{cost_yen:.3f}円", f"min_conf={a.min_confidence}",
                           sp.name, pathlib.Path(a.questions_file).name]) + "\n")

if a.raw:
    print(json.dumps(res, ensure_ascii=False, indent=1)); sys.exit(0)

def pct(x): return f"{x*100:.0f}%"
ans = res.get("answers", {})
print(f"model={res.get('model')}  input={tok_in}tok  {elapsed:.2f}s  約{cost_yen:.3f}円  min_conf={a.min_confidence}")
scores = {k: v for k, v in ans.items() if v.get("type") == "score"}
if scores:
    print("\n| 質問 | score | 信頼度 | 最も近い段階 |\n|---|---|---|---|")
    for k, v in scores.items():
        leg = v.get("legend", {}); near = leg.get(str(round(v["score"])), "")
        print(f"| {k} | {v['score']:.2f} | {pct(v['confidence'])} | {near} |")
for k, v in ans.items():
    if v.get("type") == "noul":
        print(f"\n{k}: はい {pct(v['noul'])}")
for k, v in ans.items():
    if v.get("type") == "choice":
        probs = sorted(v.get("probabilities", {}).items(), key=lambda kv: -kv[1])
        dist = " ・ ".join(f"{c} {pct(pr)}" for c, pr in probs)
        verdict = "閾値以上。score の根拠を見て人が決裁" if v["confidence"] >= a.min_confidence else "閾値未満。人が決める"
        print(f"\n{k}: {v['choice']}（信頼度 {pct(v['confidence'])}）  {dist}\n  → {verdict}")
PY
