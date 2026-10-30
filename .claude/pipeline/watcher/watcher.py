#!/usr/bin/env python3
"""IdeaFactory local watcher.

Polls GitHub for pipeline issues and launches headless Claude Code runs for
them, as described in .claude/pipeline/STATE.md:

- `approved` issue  -> set `building`, create a worktree, start a build run.
- `idea` issue      -> start a proposal run (only when IDEAFACTORY_PROPOSALS=1).
- live runs         -> heartbeat comments, 24 h wall clock and $ budget.
- finished runs     -> a finishing comment with outcome and cost.

One `poll` is one pass; launchd runs it every few minutes. Standard library
only; shells out to `gh`, `git` and the Claude Code CLI.

State lives in $IDEAFACTORY_HOME (default ~/.ideafactory): state.json, one
stream-json log per run under runs/, and watcher.log.
"""

import argparse
import datetime as dt
import fcntl
import glob
import json
import os
import signal
import subprocess
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent

REPO = os.environ.get("IDEAFACTORY_REPO", "JeremyDarkly/policy_agent")
GH_USER = os.environ.get("IDEAFACTORY_GH_USER", "JeremyDarkly")
HOME = Path(os.environ.get("IDEAFACTORY_HOME", Path.home() / ".ideafactory"))
# The main checkout (even when this script sits in a worktree). Run worktrees
# are created under its .claude/worktrees/.
CHECKOUT = Path(os.environ.get("IDEAFACTORY_CHECKOUT") or subprocess.run(
    ["git", "-C", str(HERE), "rev-parse", "--path-format=absolute", "--git-common-dir"],
    check=True, capture_output=True, text=True).stdout.strip()).parent
BASE = os.environ.get("IDEAFACTORY_BASE", "origin/main")

ENABLE_PROPOSALS = os.environ.get("IDEAFACTORY_PROPOSALS", "0") == "1"
MAX_LIVE = int(os.environ.get("IDEAFACTORY_MAX_LIVE", "2"))
BUDGET_USD = float(os.environ.get("IDEAFACTORY_BUDGET_USD", "30"))
WALL_HOURS = float(os.environ.get("IDEAFACTORY_WALL_HOURS", "24"))
HEARTBEAT_HOURS = float(os.environ.get("IDEAFACTORY_HEARTBEAT_HOURS", "2"))
PERMISSION_MODE = os.environ.get("IDEAFACTORY_PERMISSION_MODE", "auto")
MODEL = os.environ.get("IDEAFACTORY_MODEL", "")

MARKER = "<!-- ideafactory:run kind={kind} session={session} -->"


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
    """True when the run's process still exists and is still that run (pids get reused)."""
    try:
        cmd = run(["ps", "-p", str(r["pid"]), "-o", "command="])
    except subprocess.CalledProcessError:
        return False
    return r["session_id"] in cmd


def read_log(r):
    """Counts and the final result event from a run's stream-json log."""
    turns = tools = 0
    result = None
    try:
        with open(r["log"]) as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("type") == "assistant":
                    turns += 1
                    content = ev.get("message", {}).get("content", [])
                    tools += sum(1 for c in content if c.get("type") == "tool_use")
                elif ev.get("type") == "result":
                    result = ev
    except FileNotFoundError:
        pass
    return turns, tools, result


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


def launch(kind, issue, state, dry):
    number = issue["number"]
    session = str(uuid.uuid4())
    if dry:
        log(f"[dry-run] would launch {kind} run for #{number}")
        return

    if kind == "build":
        # Claim the issue first, so a second poll never launches twice.
        set_label(number, "building", ["approved"], dry)

    cwd = make_worktree(kind, number)
    runs_dir = HOME / "runs"
    runs_dir.mkdir(exist_ok=True)
    logfile = runs_dir / f"{kind}-{number}-{session[:8]}.jsonl"

    cmd = [claude_bin(), "-p", prompt_for(kind, number),
           "--output-format", "stream-json", "--verbose",
           "--session-id", session,
           "--name", f"{kind} #{number}: {issue['title']}"[:80],
           "--permission-mode", PERMISSION_MODE,
           "--max-budget-usd", str(BUDGET_USD)]
    if MODEL:
        cmd += ["--model", MODEL]

    with open(logfile, "w") as out:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=gh_env(), start_new_session=True)

    r = {"kind": kind, "issue": number, "title": issue["title"], "pid": proc.pid,
         "session_id": session, "worktree": str(cwd), "log": str(logfile),
         "started_at": iso(now()), "last_heartbeat": iso(now()), "status": "running"}
    state["runs"][run_key(kind, number)] = r
    save_state(state)
    log(f"launched {kind} run for #{number}: pid {proc.pid}, session {session}")

    comment(number, "\n".join([
        f"{kind.capitalize()} run started by the local watcher.",
        "",
        f"- Session: `{session}` (`claude --resume {session}` from `{cwd.relative_to(CHECKOUT)}` once it ends)",
        f"- Worktree: `{cwd.relative_to(CHECKOUT)}`",
        f"- Limits: {WALL_HOURS:g} h wall clock, ${BUDGET_USD:g} of Claude spend",
        "",
        MARKER.format(kind=kind, session=session),
    ]), dry)


# --- watching live runs ----------------------------------------------------

def stop(r):
    try:
        os.killpg(r["pid"], signal.SIGTERM)
    except ProcessLookupError:
        pass


def check_runs(state, dry):
    for key, r in state["runs"].items():
        if r["status"] == "stopped" and alive(r) and not dry:
            # Still there a poll after SIGTERM.
            os.killpg(r["pid"], signal.SIGKILL)
        if r["status"] != "running":
            continue
        number = r["issue"]
        turns, tools, result = read_log(r)

        if alive(r):
            elapsed = hours_since(r["started_at"])
            if elapsed >= WALL_HOURS:
                log(f"{key}: {elapsed:.1f} h, over the wall clock; stopping")
                if not dry:
                    stop(r)
                    r["status"] = "stopped"
                    r["ended_at"] = iso(now())
                comment(number, f"{r['kind'].capitalize()} run stopped by the watcher: it reached the "
                                f"{WALL_HOURS:g} h wall-clock limit after {turns} turns. The last checkpoint "
                                f"in the issue is the result.", dry)
            elif hours_since(r["last_heartbeat"]) >= HEARTBEAT_HOURS:
                comment(number, f"Heartbeat: {r['kind']} run still going after {elapsed:.1f} h "
                                f"({turns} turns, {tools} tool calls).", dry)
                if not dry:
                    r["last_heartbeat"] = iso(now())
            continue

        # The process is gone: report how it ended.
        cost = result.get("total_cost_usd") if result else None
        subtype = result.get("subtype") if result else "no result event"
        cost_text = f"${cost:.2f}" if cost is not None else "unknown"
        if subtype == "error_max_budget_usd":
            outcome = f"stopped at the ${BUDGET_USD:g} budget"
        elif result and not result.get("is_error"):
            outcome = "finished"
        else:
            outcome = f"ended with an error (`{subtype}`)"
        log(f"{key}: {outcome}, cost {cost_text}")
        if not dry:
            r.update(status="ended", ended_at=iso(now()), outcome=subtype, cost_usd=cost)
        comment(number, f"{r['kind'].capitalize()} run {outcome} after "
                        f"{hours_since(r['started_at']):.1f} h: {turns} turns, Claude spend {cost_text}.", dry)
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
        turns, tools, result = read_log(r)
        cost = (result or {}).get("total_cost_usd", r.get("cost_usd"))
        print(f"{key:16} {r['status']:8} started {r['started_at']}  turns {turns:4}  "
              f"cost {'-' if cost is None else f'${cost:.2f}'}  {r['log']}")


def stop_cmd(kind, number):
    state = load_state()
    r = state["runs"].get(run_key(kind, number))
    if not r or r["status"] != "running" or not alive(r):
        sys.exit(f"No live {kind} run for #{number}.")
    stop(r)
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
            log(f"command failed: {' '.join(e.cmd[:4])}: {e.stderr.strip()}")
            raise


if __name__ == "__main__":
    main()
