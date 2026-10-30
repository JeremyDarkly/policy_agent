#!/usr/bin/env python3
"""IdeaFactory local watcher.

Polls GitHub for pipeline issues and launches Claude Code runs for them, as
described in .claude/pipeline/STATE.md:

- `approved` issue  -> set `building`, create a worktree, start a build run as
                       a background session you can attach to (`claude attach`).
- `idea` issue      -> start a headless proposal run (only when
                       IDEAFACTORY_PROPOSALS=1).
- live runs         -> heartbeat comments and the wall clock; proposal runs
                       also have a hard $ budget.
- finished runs     -> a finishing comment with outcome and Claude spend.

One `poll` is one pass; launchd runs it every few minutes. Standard library
only; shells out to `gh`, `git` and the Claude Code CLI.

State lives in $IDEAFACTORY_HOME (default ~/.ideafactory): state.json, one
stream-json log per proposal run under runs/, and watcher.log.
"""

import argparse
import datetime as dt
import fcntl
import glob
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent

REPO = os.environ.get("IDEAFACTORY_REPO", "JeremyDarkly/policy_agent")
GH_USER = os.environ.get("IDEAFACTORY_GH_USER", "JeremyDarkly")
HOME = Path(os.environ.get("IDEAFACTORY_HOME", Path.home() / ".ideafactory"))
# The main checkout (even when this script sits in a worktree). Run worktrees
# are created under its .claude/worktrees/.
CHECKOUT = Path(os.environ.get("IDEAFACTORY_CHECKOUT") or Path(subprocess.run(
    ["git", "-C", str(HERE), "rev-parse", "--path-format=absolute", "--git-common-dir"],
    check=True, capture_output=True, text=True).stdout.strip()).parent)
BASE = os.environ.get("IDEAFACTORY_BASE", "origin/main")

ENABLE_PROPOSALS = os.environ.get("IDEAFACTORY_PROPOSALS", "0") == "1"
MAX_LIVE = int(os.environ.get("IDEAFACTORY_MAX_LIVE", "2"))
BUDGET_USD = float(os.environ.get("IDEAFACTORY_BUDGET_USD", "30"))
WALL_HOURS = float(os.environ.get("IDEAFACTORY_WALL_HOURS", "24"))
BUILD_WALL_HOURS = float(os.environ.get("IDEAFACTORY_BUILD_WALL_HOURS", "72"))
HEARTBEAT_HOURS = float(os.environ.get("IDEAFACTORY_HEARTBEAT_HOURS", "2"))
PERMISSION_MODE = os.environ.get("IDEAFACTORY_PERMISSION_MODE", "auto")
MODEL = os.environ.get("IDEAFACTORY_MODEL", "")

MARKER = "<!-- ideafactory:run kind={kind} session={session} -->"

# $ per million tokens: input, output, cache read. Cache writes are 1.25x input
# (5 min) or 2x input (1 h). Checked against the CLI's own costUSD for
# claude-sonnet-5-5 on 2026-10-30; the others are list prices.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-fable-5-1": (10.00, 50.00, 0.25),
}


# --- small helpers ---------------------------------------------------------

def now():
    return dt.datetime.now(dt.timezone.utc)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def hours_since(s):
    return (now() - parse_iso(s)).total_seconds() / 3600


def log(msg):
    line = f"{iso(now())} {msg}"
    print(line, flush=True)
    with open(HOME / "watcher.log", "a") as f:
        f.write(line + "\n")


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw).stdout


def notify(title, text):
    """A macOS notification, so you hear about a build waiting on you before the next heartbeat."""
    script = f"display notification {json.dumps(text)} with title {json.dumps(title)}"
    subprocess.run(["osascript", "-e", script], capture_output=True)


_gh_env = None


def gh_env():
    """Environment with GH_TOKEN for the pipeline account, whatever gh's active account is."""
    global _gh_env
    if _gh_env is None:
        token = os.environ.get("GH_TOKEN") or run(["gh", "auth", "token", "--user", GH_USER]).strip()
        _gh_env = {**os.environ, "GH_TOKEN": token}
    return _gh_env


def gh(*args):
    return run(["gh", *args], env=gh_env())


def gh_json(*args):
    return json.loads(gh(*args) or "null")


def comment(number, body, dry):
    if dry:
        log(f"[dry-run] comment on #{number}: {body.splitlines()[0]}")
        return
    gh("issue", "comment", str(number), "--repo", REPO, "--body", body)


def set_label(number, add, remove, dry):
    if dry:
        log(f"[dry-run] #{number}: +{add} -{remove}")
        return
    args = ["issue", "edit", str(number), "--repo", REPO, "--add-label", add]
    for name in remove:
        args += ["--remove-label", name]
    gh(*args)


def claude_bin():
    """CLAUDE_BIN, else `claude` on PATH, else the newest CLI bundled with the desktop app."""
    if os.environ.get("CLAUDE_BIN"):
        return os.environ["CLAUDE_BIN"]
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = Path(d) / "claude"
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    pattern = str(Path.home() / "Library/Application Support/Claude/claude-code/*/*/claude.app/Contents/MacOS/claude")

    def version(p):
        v = Path(p).parts[-6]
        return tuple(int(x) for x in v.split(".") if x.isdigit())

    found = sorted(glob.glob(pattern), key=version)
    if not found:
        sys.exit("No Claude Code CLI found; set CLAUDE_BIN.")
    return found[-1]


def claude(*args, **kw):
    return run([claude_bin(), *args], **kw)


# --- state -----------------------------------------------------------------

def load_state():
    p = HOME / "state.json"
    return json.loads(p.read_text()) if p.exists() else {"runs": {}}


def save_state(state):
    p = HOME / "state.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(p)


def run_key(kind, number):
    return f"{kind}-{number}"


def alive(r):
    """True when a headless run's process still exists and is still that run (pids get reused)."""
    try:
        cmd = run(["ps", "-p", str(r["pid"]), "-o", "command="])
    except subprocess.CalledProcessError:
        return False
    return r["session_id"] in cmd


def read_log(r):
    """Counts and the final result event from a headless run's stream-json log."""
    turns, tools = set(), set()
    result = None
    try:
        with open(r["log"]) as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("type") == "assistant":
                    m = ev.get("message", {})
                    turns.add(m.get("id"))
                    tools |= {c.get("id") for c in m.get("content", []) if c.get("type") == "tool_use"}
                elif ev.get("type") == "result":
                    result = ev
    except FileNotFoundError:
        pass
    return len(turns), len(tools), result


def transcript_usage(session_id):
    """Turns, tool calls and estimated $ for a session, from its transcript and its subagents'.

    Background sessions report no cost, so this prices the token usage the
    transcript records. Each API message is written once per content block
    with the same usage, so messages are counted by id.
    """
    root = Path.home() / ".claude" / "projects"
    files = glob.glob(str(root / "*" / f"{session_id}.jsonl"))
    files += glob.glob(str(root / "*" / session_id / "subagents" / "*.jsonl"))
    messages, tools = {}, set()
    for path in files:
        with open(path) as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                m = ev.get("message") if ev.get("type") == "assistant" else None
                if not m or "usage" not in m:
                    continue
                tools |= {c.get("id") for c in m.get("content", []) if c.get("type") == "tool_use"}
                messages[m.get("id")] = (m.get("model", ""), m["usage"])
    cost = 0.0
    for model, u in messages.values():
        inp, out, read = PRICES.get(model, PRICES["claude-opus-5-5"])
        cc = u.get("cache_creation") or {}
        w5 = cc.get("ephemeral_5m_input_tokens", 0)
        w1h = cc.get("ephemeral_1h_input_tokens", u.get("cache_creation_input_tokens", 0) - w5)
        cost += (u.get("input_tokens", 0) * inp + u.get("output_tokens", 0) * out
                 + u.get("cache_read_input_tokens", 0) * read
                 + w5 * inp * 1.25 + w1h * inp * 2) / 1e6
    return len(messages), len(tools), cost


def background_sessions():
    """Background sessions by short id, from `claude agents`."""
    out = claude("agents", "--json", "--all")
    return {a["id"]: a for a in json.loads(out or "[]") if a.get("kind") == "background"}


# --- launching -------------------------------------------------------------

def prompt_for(kind, number):
    text = (HERE / "prompts" / f"{kind}.md").read_text()
    return text.replace("{{ISSUE}}", str(number)).replace("{{REPO}}", REPO).replace("{{CHECKOUT}}", str(CHECKOUT))


def make_worktree(kind, number):
    """A fresh worktree for the run. Builds get a branch; proposals a detached checkout."""
    name = f"{kind}-{number}"
    path = CHECKOUT / ".claude" / "worktrees" / name
    if path.exists():
        return path
    run(["git", "-C", str(CHECKOUT), "fetch", "--quiet", "origin"])
    if kind == "build":
        branch = f"build/issue-{number}"
        exists = subprocess.run(["git", "-C", str(CHECKOUT), "rev-parse", "--verify", "--quiet", branch],
                                capture_output=True).returncode == 0
        args = [str(path), branch] if exists else [str(path), "-b", branch, BASE]
    else:
        args = ["--detach", str(path), BASE]
    run(["git", "-C", str(CHECKOUT), "worktree", "add", "--quiet", *args])
    return path


def start_build(number, cwd):
    """Start an interactive background session; returns (short id, session id)."""
    name = f"build-{number}"
    cmd = ["--bg", "--name", name, "--permission-mode", PERMISSION_MODE]
    if MODEL:
        cmd += ["--model", MODEL]
    out = claude(*cmd, prompt_for("build", number), cwd=cwd, env=gh_env(), stdin=subprocess.DEVNULL)
    m = re.search(r"backgrounded\W+([0-9a-f]{8})", out)
    if not m:
        raise RuntimeError(f"claude --bg printed no session id: {out.strip()[:200]}")
    short = m.group(1)
    # --bg picks the session id itself; read it back.
    for _ in range(10):
        s = background_sessions().get(short)
        if s and s.get("sessionId"):
            return short, s["sessionId"]
        time.sleep(1)
    raise RuntimeError(f"background session {short} did not appear in `claude agents`")


def start_proposal(number, title, cwd, logfile):
    """Start a headless run with a hard $ cap; returns (pid, session id)."""
    session = str(uuid.uuid4())
    cmd = [claude_bin(), "-p", prompt_for("proposal", number),
           "--output-format", "stream-json", "--verbose",
           "--session-id", session,
           "--name", f"proposal #{number}: {title}"[:80],
           "--permission-mode", PERMISSION_MODE,
           "--max-budget-usd", str(BUDGET_USD)]
    if MODEL:
        cmd += ["--model", MODEL]
    with open(logfile, "w") as out:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=gh_env(), start_new_session=True)
    return proc.pid, session


def launch(kind, issue, state, dry):
    number = issue["number"]
    if dry:
        log(f"[dry-run] would launch {kind} run for #{number}")
        return

    if kind == "build":
        # Claim the issue first, so a second poll never launches twice.
        set_label(number, "building", ["approved"], dry)

    cwd = make_worktree(kind, number)
    rel = cwd.relative_to(CHECKOUT)
    r = {"kind": kind, "issue": number, "title": issue["title"], "worktree": str(cwd),
         "started_at": iso(now()), "last_heartbeat": iso(now()), "status": "running"}

    if kind == "build":
        short, session = start_build(number, cwd)
        r.update(short_id=short, session_id=session, last_state="busy")
        how = [f"- Talk to it: `claude attach build-{number}` (or `claude attach {short}`) in any terminal",
               f"- Session: `{session}`",
               f"- Worktree: `{rel}`, branch `build/issue-{number}`",
               f"- Limits: {BUILD_WALL_HOURS:g} h wall clock; Claude spend is estimated and reported, not capped"]
    else:
        runs_dir = HOME / "runs"
        runs_dir.mkdir(exist_ok=True)
        logfile = runs_dir / f"{kind}-{number}-{iso(now())}.jsonl"
        pid, session = start_proposal(number, issue["title"], cwd, logfile)
        r.update(pid=pid, session_id=session, log=str(logfile))
        how = [f"- Session: `{session}` (headless; `claude --resume {session}` from `{rel}` once it ends)",
               f"- Worktree: `{rel}`",
               f"- Limits: {WALL_HOURS:g} h wall clock, ${BUDGET_USD:g} of Claude spend"]

    state["runs"][run_key(kind, number)] = r
    save_state(state)
    log(f"launched {kind} run for #{number}: session {r['session_id']}")
    comment(number, "\n".join([f"{kind.capitalize()} run started by the local watcher.", "", *how, "",
                               MARKER.format(kind=kind, session=r["session_id"])]), dry)


# --- watching live runs ----------------------------------------------------

def check_build(r, sessions, labels, dry):
    """One live build: hand-off, wall clock, waiting-on-you notice, heartbeat."""
    number, short = r["issue"], r["short_id"]
    s = sessions.get(short)
    turns, tools, cost = transcript_usage(r["session_id"])
    spend = f"about ${cost:.2f}"
    elapsed = hours_since(r["started_at"])
    live = s is not None and s.get("status") in ("busy", "idle")

    if "building" not in labels:
        # The build moved on (in-review, declined, closed). The session stays
        # for review changes; the watcher stops tracking it.
        where = ", ".join(sorted(labels)) or "no lifecycle label"
        log(f"build-{number}: issue now {where}; done tracking")
        if not dry:
            r.update(status="ended", ended_at=iso(now()), outcome=where, cost_usd=cost)
        comment(number, f"Build run handed off after {elapsed:.1f} h ({where}): {turns} turns, "
                        f"{tools} tool calls, Claude spend {spend} (estimated from tokens). The session "
                        f"is still there: `claude attach build-{number}`.", dry)
        return

    if not live:
        log(f"build-{number}: session {short} is gone while the issue is still building")
        if not dry:
            r.update(status="ended", ended_at=iso(now()), outcome="session ended", cost_usd=cost)
        comment(number, f"Build session ended without a PR after {elapsed:.1f} h: {turns} turns, Claude spend "
                        f"{spend}. Resume it with `claude --resume {r['session_id']}` from "
                        f"`{Path(r['worktree']).relative_to(CHECKOUT)}`.", dry)
        return

    if elapsed >= BUILD_WALL_HOURS:
        log(f"build-{number}: {elapsed:.1f} h, over the wall clock; stopping")
        if not dry:
            claude("stop", short)
            r.update(status="stopped", ended_at=iso(now()), cost_usd=cost)
        comment(number, f"Build run stopped by the watcher at the {BUILD_WALL_HOURS:g} h wall-clock limit: "
                        f"{turns} turns, Claude spend {spend}. The conversation is kept: "
                        f"`claude attach build-{number}` reopens it.", dry)
        return

    state_now = s["status"]
    if state_now == "idle" and r.get("last_state") != "idle":
        notify(f"Build #{number} is waiting on you", f"claude attach build-{number}")
    if hours_since(r["last_heartbeat"]) >= HEARTBEAT_HOURS:
        doing = "working" if state_now == "busy" else f"waiting on you (`claude attach build-{number}`)"
        comment(number, f"Heartbeat: build run {doing}, {elapsed:.1f} h in, {turns} turns, "
                        f"{tools} tool calls, Claude spend {spend}.", dry)
        if not dry:
            r["last_heartbeat"] = iso(now())
    if not dry:
        r["last_state"] = state_now
        r["cost_usd"] = cost


def check_proposal(r, dry):
    """One live headless proposal run: wall clock, heartbeat, or how it ended."""
    number = r["issue"]
    turns, tools, result = read_log(r)

    if alive(r):
        elapsed = hours_since(r["started_at"])
        if elapsed >= WALL_HOURS:
            log(f"proposal-{number}: {elapsed:.1f} h, over the wall clock; stopping")
            if not dry:
                os.killpg(r["pid"], signal.SIGTERM)
                r["status"] = "stopped"
                r["ended_at"] = iso(now())
            comment(number, f"Proposal run stopped by the watcher: it reached the {WALL_HOURS:g} h wall-clock "
                            f"limit after {turns} turns. The last checkpoint in the issue is the result.", dry)
        elif hours_since(r["last_heartbeat"]) >= HEARTBEAT_HOURS:
            comment(number, f"Heartbeat: proposal run still going after {elapsed:.1f} h "
                            f"({turns} turns, {tools} tool calls).", dry)
            if not dry:
                r["last_heartbeat"] = iso(now())
        return

    cost = result.get("total_cost_usd") if result else None
    subtype = result.get("subtype") if result else "no result event"
    cost_text = f"${cost:.2f}" if cost is not None else "unknown"
    if subtype == "error_max_budget_usd":
        outcome = f"stopped at the ${BUDGET_USD:g} budget"
    elif result and not result.get("is_error"):
        outcome = "finished"
    else:
        outcome = f"ended with an error (`{subtype}`)"
    log(f"proposal-{number}: {outcome}, cost {cost_text}")
    if not dry:
        r.update(status="ended", ended_at=iso(now()), outcome=subtype, cost_usd=cost)
    comment(number, f"Proposal run {outcome} after {hours_since(r['started_at']):.1f} h: {turns} turns, "
                    f"Claude spend {cost_text}.", dry)


def issue_labels(number):
    i = gh_json("issue", "view", str(number), "--repo", REPO, "--json", "labels,state")
    labels = {l["name"] for l in i["labels"]}
    return labels if i["state"] == "OPEN" else labels | {"closed"}


def check_runs(state, dry):
    runs = state["runs"].values()
    sessions = background_sessions() if any(r["kind"] == "build" and r["status"] == "running" for r in runs) else {}
    for r in runs:
        if r["kind"] == "proposal" and r["status"] == "stopped" and alive(r) and not dry:
            # Still there a poll after SIGTERM.
            os.killpg(r["pid"], signal.SIGKILL)
        if r["status"] != "running":
            continue
        if r["kind"] == "build":
            check_build(r, sessions, issue_labels(r["issue"]), dry)
        else:
            check_proposal(r, dry)
    if not dry:
        save_state(state)


# --- polling ---------------------------------------------------------------

def issues_with(label):
    return gh_json("issue", "list", "--repo", REPO, "--state", "open", "--label", label,
                   "--json", "number,title,labels", "--limit", "100")


def has_marker(number, kind):
    bodies = gh_json("issue", "view", str(number), "--repo", REPO, "--json", "comments",
                     "--jq", "[.comments[].body]")
    return any(f"ideafactory:run kind={kind} " in b for b in bodies)


def poll(dry):
    state = load_state()
    check_runs(state, dry)

    live = sum(1 for r in state["runs"].values() if r["status"] == "running")
    wanted = [("build", i) for i in issues_with("approved")]
    if ENABLE_PROPOSALS:
        for i in issues_with("idea"):
            labels = {l["name"] for l in i["labels"]}
            if "blocked" in labels or run_key("proposal", i["number"]) in state["runs"]:
                continue
            if not has_marker(i["number"], "proposal"):
                wanted.append(("proposal", i))

    for kind, issue in wanted:
        existing = state["runs"].get(run_key(kind, issue["number"]))
        if existing and existing["status"] == "running":
            continue
        if live >= MAX_LIVE:
            log(f"#{issue['number']} waiting: {live} runs live (max {MAX_LIVE})")
            continue
        launch(kind, issue, state, dry)
        live += 1


def status():
    state = load_state()
    for key, r in sorted(state["runs"].items()):
        if r["kind"] == "build":
            turns, _, cost = transcript_usage(r["session_id"])
            where = f"claude attach build-{r['issue']}"
        else:
            turns, _, result = read_log(r)
            cost = (result or {}).get("total_cost_usd", r.get("cost_usd"))
            where = r["log"]
        print(f"{key:16} {r['status']:8} started {r['started_at']}  turns {turns:4}  "
              f"cost {'-' if cost is None else f'${cost:.2f}'}  {where}")


def stop_cmd(kind, number):
    state = load_state()
    r = state["runs"].get(run_key(kind, number))
    if not r or r["status"] != "running":
        sys.exit(f"No live {kind} run for #{number}.")
    if kind == "build":
        claude("stop", r["short_id"])
        print(f"Stopped build session {r['short_id']} for #{number}; `claude attach build-{number}` reopens it.")
        return
    if not alive(r):
        sys.exit(f"No live {kind} run for #{number}.")
    os.killpg(r["pid"], signal.SIGTERM)
    print(f"Sent SIGTERM to {kind} run for #{number} (pid {r['pid']}). The next poll reports how it ended.")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("poll", help="one pass: check live runs, launch new ones")
    p.add_argument("--dry-run", action="store_true", help="report what would happen; change nothing")
    sub.add_parser("status", help="list runs the watcher knows about")
    s = sub.add_parser("stop", help="stop a live run")
    s.add_argument("kind", choices=["build", "proposal"])
    s.add_argument("issue", type=int)
    args = ap.parse_args()

    HOME.mkdir(parents=True, exist_ok=True)
    if args.cmd == "status":
        return status()
    if args.cmd == "stop":
        return stop_cmd(args.kind, args.issue)

    # Only one poll at a time, even if launchd and a manual run overlap.
    with open(HOME / "poll.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("another poll is running; skipping")
            return
        try:
            poll(args.dry_run)
        except subprocess.CalledProcessError as e:
            log(f"command failed: {' '.join(str(c) for c in e.cmd[:4])}: {(e.stderr or '').strip()}")
            raise


if __name__ == "__main__":
    main()
