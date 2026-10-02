#!/usr/bin/env python3
"""jev-stop-check.py — 「完了しました」に証拠を求める Stop / SubagentStop フック（jev-belay の型）

動き:
  1. 今回のターン（最後のユーザー発言以降）でファイル変更があったかを transcript から数える
  2. 最後の変更より後に検査コマンド（テスト・lint・check スクリプト・git diff・スクショ・変更ファイル自体の実行）が走ったかを見る
  3. 「変更あり かつ 検査なし」のときだけ Jev に4問を1回聞く
     claims_done / claims_verified / verification_applies（noul）と outcome（choice）
  4. 完了を主張（>0.70）し、検査が意味を持ち（>0.5）、blocked でないなら exit 2 で止めて検査を促す
  5. それ以外とエラー時（キーなし・タイムアウト・応答不正・stop_hook_active）はすべて exit 0 で通す

使い方:
  python3 /path/to/claude-code-jev-hooks/jev-stop-check.py --hook                 # settings.json から stdin で呼ぶ
  python3 /path/to/claude-code-jev-hooks/jev-stop-check.py --explain TRANSCRIPT   # 送らずに判定材料だけ表示する
  python3 /path/to/claude-code-jev-hooks/jev-stop-check.py --explain TRANSCRIPT --send   # 実際に Jev へ送って判定まで見る

環境変数:
  JEV_STOP_DISABLE=1        何もせず通す（緊急停止）
  JEV_STOP_THRESHOLD=0.70   claims_done の閾値
  JEV_STOP_SKIP_RE          変更ファイルの絶対パスがこれに一致したら Jev を呼ばない。既定 /clients/
                            （顧客案件の文面を米国の外部 API に送らないため）
  JEV_STOP_CHECK_RE         検査コマンドとみなす正規表現を追加する
  JEV_STOP_MAX_BLOCKS=3     1セッションで止める回数の上限。60秒以内の連続ブロックもしない
"""
import argparse, datetime, json, os, pathlib, re, subprocess, sys, time

HOME = pathlib.Path.home()
SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
JEV_SCORE = SCRIPT_DIR / "jev-score.sh"
HOOKS_HOME = pathlib.Path(os.environ.get("JEV_HOOKS_HOME", HOME / ".cache" / "jev-hooks")).expanduser()
LOG = HOOKS_HOME / "jev-stop.log"
STATE_DIR = HOOKS_HOME / "jev-stop"

CHANGE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
BASH_WRITE_RE = re.compile(
    r"(sed\s+-i|\btee\b|(?<![0-9&])>\s*(?!/dev/null|&)[\"'/~.\w]|cat\s*>|\bcp\s|\bmv\s|\bmkdir\s"
    r"|git\s+(commit|push|mv|rm)|Path\([^)]*\)\.write_text|open\([^)]*[\"'][wa][\"'])")
CHECK_RE = re.compile(
    r"(check-[\w-]+\.(py|sh)|jp_writing_check|coverage-check|skill-eval|lint-daily-log|check-md-links"
    r"|\blint\b|pytest|rspec|npm\s+test|\bjest\b|vitest|\btsc\b|eslint|ruff|mypy|rubocop|cargo\s+test"
    r"|go\s+test|git\s+diff|--screenshot|playwright|\bdiff\s|python3?\s+-m\s+(py_compile|json\.tool)"
    r"|bash\s+-n\b|jq\s+\.|\btest\b)", re.I)
RESULT_RE = re.compile(r"(\d+ passed|\d+ failed|All checks passed|✅ .*(0件|なし|問題なし)|リンク切れ 0)", re.I)
SKIP_DIRS = ("/private/tmp/claude-", "/tmp/claude-", "/.cache/", "/.claude/projects/")

QUESTIONS = {
    "claims_done": {"type": "noul",
        "instructions": "final_message は、依頼された作業が完了した・出来上がった・反映したと報告しているか",
        "criteria": {"true": "完了・作成済み・反映済み・修正済みなど、作業が終わったと主張している",
                     "false": "途中経過の報告、質問、調査結果の提示、または止まっている理由の説明で、完了の主張がない"}},
    "claims_verified": {"type": "noul",
        "instructions": "final_message は、検査・テスト・確認・検証を実施してその結果を見たと主張しているか",
        "criteria": {"true": "テストを通した・検査した・確認した・動作を見たなど、検証済みだと述べている",
                     "false": "検証についての言及がない、または未検証と明言している"}},
    "verification_applies": {"type": "noul",
        "instructions": "run の内容（変更ファイルの種類・件数・使ったツール）に対して、検査（テスト・lint・差分確認・"
                        "リンク実在チェック・書式チェック・スクショ）を走らせることに意味があるか",
        "criteria": {"true": "コード・スクリプト・設定・公開文書・スライド・LP などを変更しており、検査で誤りを見つけられる",
                     "false": "ログへの1行追記・メモ・TODO のチェック更新・読み取りだけで、検査しても得るものがない"}},
    "outcome": {"type": "choice",
        "instructions": "final_message が表す作業の到達状態",
        "criteria": {"complete": "依頼の全体が終わった", "partial": "一部が終わり残りがある",
                     "blocked": "決裁・入力・権限などユーザー側の対応待ちで止まっている", "other": "上のどれでもない"}},
}


def read_transcript(path):
    rows = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("type") in ("user", "assistant") and isinstance(o.get("message"), dict):
                    rows.append(o)
    except Exception:
        pass
    return rows


def blocks(o):
    c = o["message"].get("content")
    if isinstance(c, str):
        return [{"type": "text", "text": c}]
    return c if isinstance(c, list) else []


def analyze(rows, last_message=""):
    """最後のユーザー発言以降を切り出し、変更・検査・最終文を返す"""
    start = 0
    task = ""
    for i, o in enumerate(rows):
        if o.get("type") == "user" and not o.get("isMeta"):
            texts = [b.get("text", "") for b in blocks(o) if b.get("type") == "text"]
            if texts and not any(b.get("type") == "tool_result" for b in blocks(o)):
                start, task = i, "\n".join(texts)
    turn = rows[start:]
    changes, checks, tools_after, results = [], [], [], []
    last_change_idx = -1
    final = last_message
    for i, o in enumerate(turn):
        if o.get("type") == "assistant":
            for b in blocks(o):
                if b.get("type") == "text" and b.get("text", "").strip():
                    final = b["text"]
                if b.get("type") != "tool_use":
                    continue
                name, inp = b.get("name", ""), b.get("input", {}) or {}
                if name in CHANGE_TOOLS:
                    fp = inp.get("file_path") or inp.get("notebook_path") or ""
                    if fp and not any(s in fp for s in SKIP_DIRS):
                        changes.append(fp); last_change_idx = i; tools_after = []
                    continue
                if name == "Bash":
                    cmd = inp.get("command", "")
                    if CHECK_RE.search(cmd) and last_change_idx >= 0:
                        checks.append(cmd.strip().split("\n")[0][:80])
                    elif BASH_WRITE_RE.search(cmd):
                        changes.append("bash:" + cmd.strip().split("\n")[0][:60]); last_change_idx = i; tools_after = []
                        continue
                if last_change_idx >= 0:
                    tools_after.append(name)
        elif o.get("type") == "user" and last_change_idx >= 0:
            for b in blocks(o):
                if b.get("type") == "tool_result":
                    txt = b.get("content") if isinstance(b.get("content"), str) else json.dumps(b.get("content"), ensure_ascii=False)
                    m = RESULT_RE.search(txt or "")
                    if m:
                        results.append(m.group(0)[:40])
    # 最後の変更より後の検査だけを証拠にする
    checks_after = []
    if last_change_idx >= 0:
        names = [pathlib.Path(c).name for c in changes if not c.startswith("bash:")]
        for o in turn[last_change_idx + 1:]:
            if o.get("type") != "assistant":
                continue
            for b in blocks(o):
                if b.get("type") != "tool_use" or b.get("name") != "Bash":
                    continue
                cmd = (b.get("input") or {}).get("command", "")
                # 検査コマンド、または変更したファイル自体を実行・照合したコマンドを証拠にする
                if CHECK_RE.search(cmd) or any(n and n in cmd for n in names):
                    checks_after.append(cmd.strip().split("\n")[0][:80])
    return {"task": task, "final": final, "changes": changes, "checks_after": checks_after + results,
            "tools_after": tools_after}


def build_state(a):
    kinds = sorted({(pathlib.Path(c).suffix or "(none)") for c in a["changes"] if not c.startswith("bash:")})
    return {
        "task": a["task"][:600],
        "final_message": a["final"][:2000],
        "run": {"file_changes": len(a["changes"]), "changed_kinds": kinds,
                "bash_writes": sum(c.startswith("bash:") for c in a["changes"]),
                "tools_after_last_change": a["tools_after"][-10:], "checks_run": a["checks_after"]},
    }


def ask_jev(state, timeout=15):
    tmp = pathlib.Path(os.environ.get("TMPDIR", "/tmp")) / f"jev-stop-{os.getpid()}"
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "state.json").write_text(json.dumps(state, ensure_ascii=False))
    (tmp / "questions.json").write_text(json.dumps(QUESTIONS, ensure_ascii=False))
    r = subprocess.run(["bash", str(JEV_SCORE), str(tmp / "state.json"), str(tmp / "questions.json"),
                        "--raw", "--timeout", str(timeout)], capture_output=True, text=True, timeout=timeout + 5)
    for f in tmp.iterdir():
        f.unlink()
    tmp.rmdir()
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:200] or r.stdout.strip()[:200])
    ans = json.loads(r.stdout)["answers"]
    return {"claims_done": ans["claims_done"]["noul"], "claims_verified": ans["claims_verified"]["noul"],
            "verification_applies": ans["verification_applies"]["noul"], "outcome": ans["outcome"]["choice"],
            "outcome_conf": ans["outcome"]["confidence"], "model": json.loads(r.stdout).get("model")}


def log(event, sid, a, decision, j=None, threshold=None):
    # 列: 時刻・イベント・セッション・変更数・検査数・判定・4問の確率・応答モデルの版・閾値（決裁ルール §2 条件6）
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write("\t".join([datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S"), event, sid[:8],
                               f"changes={len(a['changes'])}", f"checks={len(a['checks_after'])}", decision,
                               "" if not j else f"done={j['claims_done']:.2f} verified={j['claims_verified']:.2f} "
                                                f"applies={j['verification_applies']:.2f} outcome={j['outcome']}",
                               "" if not j else f"model={j.get('model') or '-'}",
                               "" if threshold is None else f"thr=done>{threshold:.2f}&applies>0.50&outcome!=blocked"]) + "\n")
    except Exception:
        pass


def session_state(sid):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    p = STATE_DIR / f"{sid}.json"
    try:
        return p, json.loads(p.read_text())
    except Exception:
        return p, {"blocks": 0, "last": 0}


def decide(a, j, threshold):
    if j["outcome"] == "blocked":
        return None
    if j["claims_done"] > threshold and j["verification_applies"] > 0.5:
        n = len(a["changes"])
        msg = (f"jev-stop-check: 完了を報告しています（確率 {j['claims_done']:.2f}）が、{n}件のファイル変更のあとに検査が走っていません。")
        if j["claims_verified"] > 0.5:
            msg += f" 検査済みと述べています（{j['claims_verified']:.2f}）が、transcript に検査コマンドがありません。"
        msg += ("\n変更したものに合う検査を1つ以上実行し、結果を報告してから終了してください。"
                "例: md は scripts/check-md-links.py と scripts/check-date-weekday.py、標準ファイルは scripts/check-standard-file.py、"
                "スクリプトは実行かテスト、HTML はスクショ、コミットは git diff --stat。"
                "検査が要らない変更なら、その理由を1行書いて終了してください。")
        return msg
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hook", action="store_true")
    ap.add_argument("--explain")
    ap.add_argument("--send", action="store_true")
    o = ap.parse_args()
    threshold = float(os.environ.get("JEV_STOP_THRESHOLD", "0.70"))
    skip_re = re.compile(os.environ.get("JEV_STOP_SKIP_RE", r"/clients/"))
    if os.environ.get("JEV_STOP_CHECK_RE"):
        global CHECK_RE
        CHECK_RE = re.compile(CHECK_RE.pattern + "|" + os.environ["JEV_STOP_CHECK_RE"], re.I)

    if o.explain:
        a = analyze(read_transcript(o.explain))
        print(json.dumps({"changes": a["changes"], "checks_after": a["checks_after"], "task": a["task"][:120],
                          "final": a["final"][:200]}, ensure_ascii=False, indent=1))
        if not a["changes"]:
            print("→ 変更なし。Jev は呼ばない"); return
        if a["checks_after"]:
            print("→ 検査あり。Jev は呼ばない"); return
        if any(skip_re.search(c) for c in a["changes"]):
            print("→ 顧客案件のパスを含む。Jev は呼ばない"); return
        st = build_state(a)
        print("state:", json.dumps(st, ensure_ascii=False)[:600])
        if o.send:
            j = ask_jev(st); print("jev:", j); print("decision:", decide(a, j, threshold) or "通す")
        return

    if not o.hook:
        ap.print_help(); return
    try:
        inp = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    sid = inp.get("session_id", "")
    event = inp.get("hook_event_name", "Stop")
    if os.environ.get("JEV_STOP_DISABLE") or inp.get("stop_hook_active"):
        sys.exit(0)
    try:
        a = analyze(read_transcript(inp.get("transcript_path", "")), inp.get("last_assistant_message", ""))
        p0, ss0 = session_state(sid)
        if ss0.get("pending_block"):
            log(event, sid, a, f"after_block:checks={len(a['checks_after'])}")
            ss0["pending_block"] = False; p0.write_text(json.dumps(ss0))
        if not a["changes"] or a["checks_after"]:
            sys.exit(0)
        if any(skip_re.search(c) for c in a["changes"]) or skip_re.search(inp.get("cwd", "")):
            log(event, sid, a, "skip:clients"); sys.exit(0)
        p, ss = session_state(sid)
        if ss["blocks"] >= int(os.environ.get("JEV_STOP_MAX_BLOCKS", "3")) or time.time() - ss["last"] < 60:
            log(event, sid, a, "skip:cap"); sys.exit(0)
        j = ask_jev(build_state(a))
        reason = decide(a, j, threshold)
        if not reason:
            log(event, sid, a, "pass", j, threshold); sys.exit(0)
        ss["blocks"] += 1; ss["last"] = time.time(); ss["pending_block"] = True; p.write_text(json.dumps(ss))
        log(event, sid, a, "block", j, threshold)
        print(reason, file=sys.stderr)
        sys.exit(2)
    except SystemExit:
        raise
    except Exception as e:
        try:
            log(event, sid, {"changes": [], "checks_after": []}, f"error:{type(e).__name__}:{str(e)[:60]}")
        except Exception:
            pass
        sys.exit(0)


if __name__ == "__main__":
    main()
