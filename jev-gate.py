#!/usr/bin/env python3
"""jev-gate.py — Bash コマンド実行前の危険度ゲート（PreToolUse フック、jev-stop-check.py の姉妹）

動き（上から順・前の層で決まったら後は見ない）:
  A. ハードルール   コードだけで判定。rm -r/-f・git push --force・git reset --hard 等・
                     sudo・curl|bash・chmod -R 777・ssh/scp/rsync のリモート指定・
                     launchctl unload/bootout・killall・kill -9・find -delete/-exec rm に
                     当たったら Jev を呼ばず必ず ask
  C. 送信スキップ   コマンド文字列に JEV_GATE_SKIP_PATHS のパス・denylist の語・メール・11桁電話が
                     含まれるなら Jev を呼ばず通す（顧客情報を外部APIへ送らない）
                     ※ 文書上の層順は A→B→C→D だが、読み取り専用でも
                       対象パスが機微な場合は必ずログに残したいため、
                       実装は B（許可リスト）より前に C（送信スキップ）を判定する
  B. 許可リスト     読み取り専用コマンド（ls/cat/grep/git status 等）だけで構成されるなら
                     Jev を呼ばず通す。ログも書かない（量が増えるため）
  D. Jev判定        上のどれにも当たらなければ jev-score.sh で risk（choice）と
                     irreversible（noul）を1回聞く。既定はシャドーモード（記録のみ・exit 0）。
                     JEV_GATE_ENFORCE=1 のときだけ、dangerous ≥ 閾値、または
                     irreversible≥0.80 かつ caution+dangerous≥0.80 で ask にする

出力:
  通す:       何も出さず exit 0
  確認を求める: stdout に PreToolUse の permissionDecision=ask JSON を出して exit 0（deny は使わない）
  エラー（キーなし・タイムアウト・応答不正・stdin不正）は全て exit 0 で通す（fail open）

使い方:
  python3 /path/to/claude-code-jev-hooks/jev-gate.py --hook                    # settings.json から stdin で呼ぶ
  python3 /path/to/claude-code-jev-hooks/jev-gate.py --explain "COMMAND"        # 送らずに層と判定材料を表示する
  python3 /path/to/claude-code-jev-hooks/jev-gate.py --explain "COMMAND" --send # 実際に Jev へ送って確率まで見る

環境変数:
  JEV_GATE_DISABLE=1         何もせず通す（緊急停止）
  JEV_GATE_ENFORCE=1         シャドーモードを解除し、実際に ask を出す
  JEV_GATE_THRESHOLD=0.70    dangerous の閾値（enforce時のみ有効）
  JEV_GATE_SKIP_PATHS=/clients/  層C（送信スキップ）に使う固定パス。カンマ区切り
  JEV_GATE_SKIP_RE           層C（送信スキップ）に追加する正規表現
  JEV_MODEL                  jev-score.sh に渡すモデル指定（jev-score.sh 側が環境から読む）

登録（settings.json の PreToolUse に手で追加する。このスクリプトは settings.json を編集しない）:
  {"matcher":"Bash","hooks":[{"type":"command","command":"python3 /path/to/claude-code-jev-hooks/jev-gate.py --hook","timeout":10,"statusMessage":"Jev がコマンドの危険度を確認中"}]}
"""
import argparse, datetime, json, os, pathlib, re, shlex, subprocess, sys, time

HOME = pathlib.Path.home()
SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
JEV_SCORE = SCRIPT_DIR / "jev-score.sh"
DENYLIST = pathlib.Path(__file__).parent / "denylist.txt"
HOOKS_HOME = pathlib.Path(os.environ.get("JEV_HOOKS_HOME", HOME / ".cache" / "jev-hooks")).expanduser()
LOG = HOOKS_HOME / "jev-gate.log"

USD_PER_MTOK = 0.042

# ---- 分割・トークン化（厳密なシェル解析ではないヒューリスティック） ----
SPLIT_RE = re.compile(r"&&|\|\||;|\|")


def split_segments(cmd):
    return [s.strip() for s in SPLIT_RE.split(cmd) if s.strip()]


def tokenize(seg):
    try:
        return shlex.split(seg)
    except ValueError:
        return seg.split()


def has_flag(tokens, short_chars, long_opts):
    for t in tokens:
        if t in long_opts:
            return True
        if len(t) > 1 and t[0] == "-" and not t.startswith("--"):
            if any(c in t[1:] for c in short_chars):
                return True
    return False


# ---- 層 A: ハードルール ----
RM_ALLOW_PREFIXES = ("/private/tmp/claude-", "/tmp/claude-", str(HOME / ".cache") + "/", "~/.cache/")


def hard_rm(toks):
    if not toks or toks[0] != "rm":
        return None
    if not has_flag(toks[1:], "rRf", {"--recursive", "--force", "--no-preserve-root"}):
        return None
    paths = [t for t in toks[1:] if not t.startswith("-")]
    if paths and all(any(p.startswith(pre) for pre in RM_ALLOW_PREFIXES) for p in paths):
        return None
    return "rm -r/-f（対象に一時領域以外を含む）"


def hard_git(toks):
    if len(toks) < 2 or toks[0] != "git":
        return None
    sub, rest = toks[1], toks[2:]
    if sub == "push" and has_flag(rest, "f", {"--force", "--force-with-lease"}):
        return "git push --force"
    if sub == "reset" and "--hard" in rest:
        return "git reset --hard"
    if sub == "clean" and has_flag(rest, "f", {"--force"}):
        return "git clean -f"
    if sub == "branch" and has_flag(rest, "D", set()):
        return "git branch -D"
    if sub == "stash" and rest and rest[0] in ("drop", "clear"):
        return f"git stash {rest[0]}"
    if sub == "checkout" and "--" in rest and "." in rest:
        return "git checkout -- ."
    if sub == "restore" and "." in rest:
        return "git restore ."
    if sub == "rebase":
        return "git rebase"
    return None


def hard_chmod(toks):
    if not toks or toks[0] != "chmod":
        return None
    if has_flag(toks[1:], "R", {"--recursive"}) and "777" in toks[1:]:
        return "chmod -R 777"
    return None


REMOTE_RE = re.compile(r"[\w.\-]+@[\w.\-]+|(?<!:)\b[a-zA-Z][\w.\-]*:(?!//)[~/\w]")


def hard_remote(toks, seg):
    if not toks or toks[0] not in ("ssh", "scp", "rsync"):
        return None
    return "ssh/scp/rsync のリモート指定" if REMOTE_RE.search(seg) else None


def hard_find(toks):
    if not toks or toks[0] != "find":
        return None
    rest = toks[1:]
    if "-delete" in rest:
        return "find -delete"
    for i, t in enumerate(rest):
        if t == "-exec" and i + 1 < len(rest) and rest[i + 1] in ("rm", "/bin/rm"):
            return "find -exec rm"
    return None


CURL_PIPE_RE = re.compile(r"\b(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?(sh|bash|zsh)\b")
SUDO_RE = re.compile(r"(^|[;&|]\s*)sudo\b")
LAUNCHCTL_RE = re.compile(r"\blaunchctl\s+(unload|bootout)\b")
KILLALL_RE = re.compile(r"\bkillall\b")
KILL9_RE = re.compile(r"\bkill\s+(-9\b|-s\s*KILL\b|-SIGKILL\b)")


def check_hard(cmd):
    for seg in split_segments(cmd):
        toks = tokenize(seg)
        for fn in (hard_rm, hard_git, hard_chmod, hard_find):
            r = fn(toks)
            if r:
                return r
        r = hard_remote(toks, seg)
        if r:
            return r
    if CURL_PIPE_RE.search(cmd):
        return "curl|wget を sh/bash にパイプ"
    if SUDO_RE.search(cmd):
        return "sudo"
    if LAUNCHCTL_RE.search(cmd):
        return "launchctl unload/bootout"
    if KILLALL_RE.search(cmd):
        return "killall"
    if KILL9_RE.search(cmd):
        return "kill -9"
    return None


# ---- 層 C: 送信スキップ ----
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE11_RE = re.compile(r"(?<!\d)\d{11}(?!\d)")


def load_denylist():
    words = []
    try:
        for line in DENYLIST.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            words.append(line)
    except Exception:
        pass
    return words


def gate_skip_paths():
    return [s.strip() for s in os.environ.get("JEV_GATE_SKIP_PATHS", "/clients/").split(",") if s.strip()]


def check_skip(cmd, denylist_words, extra_re, skip_paths):
    for s in skip_paths:
        if s in cmd:
            return s
    for w in denylist_words:
        if w in cmd:
            return f"denylist:{w}"
    if EMAIL_RE.search(cmd):
        return "メールアドレス"
    if PHONE11_RE.search(cmd):
        return "電話番号(11桁)"
    if extra_re and extra_re.search(cmd):
        return "JEV_GATE_SKIP_RE"
    return None


# ---- 層 B: 許可リスト ----
SAFE_SIMPLE = {
    "ls", "cat", "head", "tail", "wc", "grep", "rg", "egrep", "fgrep",
    "awk", "sort", "uniq", "cut", "tr", "echo", "printf", "date",
    "which", "type", "file", "stat", "du", "df", "pwd", "env", "printenv",
    "jq", "diff", "cmp", "basename", "dirname", "realpath",
}


def git_safe(rest):
    if not rest:
        return False
    sub, args = rest[0], rest[1:]
    if sub in ("status", "diff", "log", "show", "rev-parse", "ls-files", "blame"):
        return True
    if sub == "branch":
        return all(a in ("-a", "-r", "--list") for a in args)
    if sub == "remote":
        return args == ["-v"]
    if sub == "stash":
        return args == ["list"]
    return False


def check_allow(cmd):
    segs = split_segments(cmd)
    if not segs:
        return False
    for seg in segs:
        toks = tokenize(seg)
        if not toks:
            return False
        head = toks[0]
        if head == "find":
            if "-delete" in toks[1:] or "-exec" in toks[1:]:
                return False
        elif head == "sed":
            if has_flag(toks[1:], "i", {"--in-place"}):
                return False
        elif head == "git":
            if not git_safe(toks[1:]):
                return False
        elif head not in SAFE_SIMPLE:
            return False
    return True


# ---- 層 D: Jev判定 ----
QUESTIONS = {
    "risk": {
        "type": "choice",
        "instructions": "How risky is running this shell command in this context?",
        "criteria": {
            "safe": "Read-only, or writes only new files / scratch directories / append-only logs",
            "caution": "Modifies existing tracked files, installs packages, changes config or settings, "
                       "makes outbound network requests",
            "dangerous": "Deletes or overwrites existing data irreversibly, force-rewrites git history, "
                         "changes system or shell settings, sends messages or e-mail to people, "
                         "operates on remote or production hosts, or exfiltrates secrets",
        },
    },
    "irreversible": {
        "type": "noul",
        "instructions": "Does running this command have an effect that cannot be undone afterward?",
        "criteria": {"true": "実行後に元に戻せない副作用がある", "false": "やり直せる、または副作用がない"},
    },
}


def build_state(cmd, cwd):
    cwd_disp = cwd.replace(str(HOME), "~", 1) if cwd.startswith(str(HOME)) else cwd
    return {
        "command": cmd[:2000],
        "cwd": cwd_disp,
        "context": ("Claude Code agent running on the owner's macOS workspace. "
                    "Directories under /private/tmp/claude-*, /tmp/claude-* and ~/.cache are disposable scratch."),
    }


def ask_jev(state, timeout=6):
    tmp = pathlib.Path(os.environ.get("TMPDIR", "/tmp")) / f"jev-gate-{os.getpid()}"
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "state.json").write_text(json.dumps(state, ensure_ascii=False))
    (tmp / "questions.json").write_text(json.dumps(QUESTIONS, ensure_ascii=False))
    t0 = time.time()
    r = subprocess.run(["bash", str(JEV_SCORE), str(tmp / "state.json"), str(tmp / "questions.json"),
                        "--raw", "--timeout", str(timeout)], capture_output=True, text=True, timeout=timeout + 5)
    elapsed = time.time() - t0
    for f in tmp.iterdir():
        f.unlink()
    tmp.rmdir()
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:200] or r.stdout.strip()[:200])
    res = json.loads(r.stdout)
    ans = res["answers"]
    probs = ans["risk"].get("probabilities", {})
    usage = res.get("usage", {})
    tok_in = usage.get("input_tokens", 0)
    usdjpy = float(os.environ.get("JEV_USDJPY", "150"))
    cost_yen = tok_in * USD_PER_MTOK / 1e6 * usdjpy
    return {
        "dangerous": probs.get("dangerous", 0.0), "caution": probs.get("caution", 0.0),
        "safe": probs.get("safe", 0.0), "irreversible": ans["irreversible"]["noul"],
        "tokens": tok_in, "elapsed": elapsed, "cost_yen": cost_yen, "model": res.get("model"),
    }


def decide_jev(j, threshold):
    if j["dangerous"] >= threshold:
        return True
    if j["irreversible"] >= 0.80 and (j["caution"] + j["dangerous"]) >= 0.80:
        return True
    return False


def probs_str(j):
    return (f"dangerous={j['dangerous']:.2f} caution={j['caution']:.2f} "
            f"safe={j['safe']:.2f} irreversible={j['irreversible']:.2f}")


def log(mode, layer, result, cmd, probs="", tokens="", elapsed="", cost="", model="", threshold=None):
    # 列: 時刻・モード・層・判定・確率4つ・入力トークン・秒・円・コマンド先頭60字・応答モデルの版・閾値（決裁ルール §2 条件6）
    # 判定は ask／pass に加え、シャドーで閾値を越えたときは would_ask（有効化していれば ask だった）
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write("\t".join([
                datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S"), mode, layer, result, probs,
                str(tokens), (f"{elapsed:.2f}" if isinstance(elapsed, float) else str(elapsed)),
                (f"{cost:.4f}" if isinstance(cost, float) else str(cost)),
                cmd.strip().split("\n")[0].replace("\t", " ")[:60],
                (f"model={model}" if model else ""),
                ("" if threshold is None else f"thr=dangerous>={threshold:.2f}|irrev>=0.80&c+d>=0.80"),
            ]) + "\n")
    except Exception:
        pass


def ask_json(reason):
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "ask", "permissionDecisionReason": reason,
    }}, ensure_ascii=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hook", action="store_true")
    ap.add_argument("--explain")
    ap.add_argument("--send", action="store_true")
    o = ap.parse_args()

    threshold = float(os.environ.get("JEV_GATE_THRESHOLD", "0.70"))
    enforce = os.environ.get("JEV_GATE_ENFORCE") == "1"
    mode = "enforce" if enforce else "shadow"
    skip_extra_re = re.compile(os.environ["JEV_GATE_SKIP_RE"]) if os.environ.get("JEV_GATE_SKIP_RE") else None
    skip_paths = gate_skip_paths()
    denylist_words = load_denylist()

    if o.explain is not None:
        cmd = o.explain
        hard = check_hard(cmd)
        if hard:
            print(f"layer=hard rule={hard}")
            print(f"reason: ハードルール: {hard}。実行前に利用者の確認が必要です")
            return
        skip_reason = check_skip(cmd, denylist_words, skip_extra_re, skip_paths)
        if skip_reason:
            print(f"layer=skip reason={skip_reason} → Jev を呼ばず通す")
            return
        if check_allow(cmd):
            print("layer=allow（読み取り専用）→ 通す")
            return
        state = build_state(cmd, os.getcwd())
        print(f"layer=jev mode={mode} threshold={threshold}")
        print("state:", json.dumps(state, ensure_ascii=False))
        if o.send:
            j = ask_jev(state)
            print(f"jev: {probs_str(j)}  tokens={j['tokens']} {j['elapsed']:.2f}s 約{j['cost_yen']:.4f}円")
            ask = decide_jev(j, threshold)
            print("decision:", "ask" if (ask and enforce) else "通す" + ("（shadowなら実際は ask 相当）" if ask else ""))
        return

    if not o.hook:
        ap.print_help()
        return
    try:
        inp = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    if os.environ.get("JEV_GATE_DISABLE"):
        sys.exit(0)
    if inp.get("tool_name") != "Bash":
        sys.exit(0)
    cmd = (inp.get("tool_input") or {}).get("command", "")
    if not cmd:
        sys.exit(0)
    cwd = inp.get("cwd") or os.getcwd()

    try:
        hard = check_hard(cmd)
        if hard:
            reason = f"ハードルール: {hard}。実行前に利用者の確認が必要です"
            log(mode, "hard", "ask", cmd)
            print(ask_json(reason))
            sys.exit(0)

        skip_reason = check_skip(cmd, denylist_words, skip_extra_re, skip_paths)
        if skip_reason:
            log(mode, "skip", "pass", cmd)
            sys.exit(0)

        if check_allow(cmd):
            sys.exit(0)  # 層B: ログなし

        state = build_state(cmd, cwd)
        j = ask_jev(state)
        ask = decide_jev(j, threshold)
        ps = probs_str(j)
        if ask and enforce:
            reason = (f"Jev: dangerous {j['dangerous']:.2f} / irreversible {j['irreversible']:.2f}。"
                      f"{cmd.strip()[:60]}。実行前に確認してください")
            log(mode, "jev", "ask", cmd, ps, j["tokens"], j["elapsed"], j["cost_yen"], j.get("model"), threshold)
            print(ask_json(reason))
            sys.exit(0)
        log(mode, "jev", "would_ask" if ask else "pass", cmd, ps, j["tokens"], j["elapsed"], j["cost_yen"],
            j.get("model"), threshold)
        sys.exit(0)
    except SystemExit:
        raise
    except Exception as e:
        try:
            log(mode, "error", "pass", cmd, f"{type(e).__name__}:{str(e)[:80]}")
        except Exception:
            pass
        sys.exit(0)


if __name__ == "__main__":
    main()
