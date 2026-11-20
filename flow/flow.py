#!/usr/bin/env python3
"""flow: parallel agent sessions for Rojo projects, one Studio scratch place each. Zero AI, zero tokens.

  One scratch place (slot) per session. Each slot has its own stage worktree served by its own rojo;
  a session claims a free slot and pushes its own worktree onto that slot's stage.

  flow slot <name> <placeId>   (user) add a scratch place as a slot
  flow up [--slot s|--all]     (user) serve the slot's stage; connect that Studio once
  flow doctor                  (session) claim a free slot + preflight
  flow push                    (session) mirror this worktree onto its slot's stage + stamp
  flow release                 (session) free the slot when done
  flow status / where / wait / prune / check
"""
import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import flowui
from flowui import busy

print = flowui.say  # colored output, plain when piped

VERSION = "1.0.0"

def project_key():
    """Folder name of the main checkout, so every project keeps its own slots and leases."""
    p = subprocess.run(["git", "worktree", "list", "--porcelain"], capture_output=True, text=True)
    first = next((l[9:] for l in p.stdout.splitlines() if l.startswith("worktree ")), "")
    return re.sub(r"[^\w.-]", "_", Path(first).name) if first else "default"


_BASE = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "flow"  # per-machine state, not in git
# FLOW_HOME is not read: older per-project flow copies set it user-wide, which would make every project share state.
HERE = Path(os.environ["FLOW_STATE_DIR"]) if os.environ.get("FLOW_STATE_DIR") else _BASE / project_key()
FLOW_ROOT = _BASE  # every project's folder sits here
HERE.mkdir(parents=True, exist_ok=True)
CONFIG = HERE / "config.json"
STATE = HERE / "state.json"
LEASES = HERE / "leases.json"
NO_WINDOW = 0x08000000
LEASE_HOURS = 8  # a lease nobody pushed under for this long is free again


def stage_name(slot):
    return f"flow-{slot}"


def is_stage(path):
    name = Path(path).name
    return name == "flow-stage" or (name.startswith("flow-") and name[5:] in config()["slots"])

# Empty on purpose: every machine adds its own scratch places (`flow slot s3 <placeId>`), so two
# devices never serve or claim the same place.
DEFAULT_CONFIG = {"default": None, "slots": {}}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def run(cmd, cwd=None, check=False, label=None):
    label = label or flowui.label_for(cmd)
    with busy(label) if label else contextlib.nullcontext():
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           creationflags=NO_WINDOW)
    if check and p.returncode != 0:
        die(f"{' '.join(cmd)} failed:\n{p.stdout}{p.stderr}")
    return p


def die(msg, code=1):
    sys.stderr.write(f"{flowui.prefix(sys.stderr)} {msg}\n")
    sys.exit(code)


def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def config():
    if not CONFIG.exists():
        CONFIG.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
    return load_json(CONFIG, DEFAULT_CONFIG)


def main_port():
    """The port of the main place: servePort in the main checkout's default.project.json."""
    return load_json(main_checkout() / "default.project.json", {}).get("servePort", 34872)


def other_ports():
    """Ports claimed by other projects on this machine (their main place and their slots)."""
    mine = CONFIG.resolve()
    used = set()
    configs = list(FLOW_ROOT.glob("*/config.json"))
    if os.environ.get("FLOW_HOME"):  # a project on the older flow keeps its state there
        configs.append(Path(os.environ["FLOW_HOME"]) / "config.json")
    for path in configs:
        if not path.exists() or path.resolve() == mine:
            continue
        c = load_json(path, {})
        used.update(s.get("port", 0) for s in c.get("slots", {}).values())
        used.add(c.get("mainPort", 0))
    used.discard(0)
    return used


def cmd_register(a):
    """Reserve this project's main-place port (servePort) so no slot, here or in another project, shares it.
    Keeps the current servePort when nobody uses it, else moves it to the next free port."""
    cfg = config()
    path = main_checkout() / "default.project.json"
    text = path.read_text(encoding="utf-8")
    cur = main_port()
    taken = other_ports() | {s["port"] for s in cfg["slots"].values()}
    port = cur
    if cur in taken:
        port = max(taken | {cur, 37844}) + 1
        text = re.sub(r'("servePort"\s*:\s*)\d+', rf"\g<1>{port}", text, count=1)
        path.write_text(text, encoding="utf-8")
        print(f"servePort {cur} is already used: changed default.project.json to {port}")
    cfg["mainPort"] = port
    CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    print(f"main place port {port}")


def state():
    return load_json(STATE, {})


def save_state(s):
    STATE.write_text(json.dumps(s, indent=2), encoding="utf-8")


def main_checkout(cwd=None):
    p = run(["git", "worktree", "list", "--porcelain"], cwd=cwd)
    if p.returncode != 0:
        die("not inside a git repo")
    for line in p.stdout.splitlines():
        if line.startswith("worktree "):
            return Path(line[9:])
    die("no worktree found")


def toplevel(cwd=None):
    p = run(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
    if p.returncode != 0:
        die("not inside a git repo")
    return Path(p.stdout.strip())


def count(tree, rng):
    p = run(["git", "rev-list", "--count", rng], cwd=tree)
    return int(p.stdout.strip() or 0) if p.returncode == 0 else 0


def sync_with_main(tree):
    """Rebase a session worktree onto local main when it lacks main's commits.

    The app creates session worktrees from origin/main, so when local main has unpushed commits
    (cherry-picks) a new session starts on old code, and `flow push` would stage that old code.
    Returns (ok, message)."""
    n = count(tree, "HEAD..main")
    if n == 0:
        return True, "branch has everything in local main"
    before = run(["git", "rev-parse", "--short", "HEAD"], cwd=tree).stdout.strip()
    old = own_commits(tree)
    p = run(["git", "rebase", "--autostash", "main"], cwd=tree)
    out = p.stdout + p.stderr
    if p.returncode != 0:
        run(["git", "rebase", "--abort"], cwd=tree)  # no-op when the rebase never started
        return False, (f"branch is {n} commit(s) behind local main and `git rebase main` hit conflicts "
                       "(aborted, nothing changed)")
    after = run(["git", "rev-parse", "--short", "HEAD"], cwd=tree).stdout.strip()
    note = " (autostash had conflicts: check `git stash list`)" if "conflict" in out.lower() else ""
    new = {subj: h for h, subj in own_commits(tree)}
    hashes = "".join(f"\n       {h} -> {new.get(subj, 'already in main')}  {subj}" for h, subj in old)
    return True, f"branch was {n} commit(s) behind local main: rebased onto main ({before} -> {after}){note}{hashes}"


def own_commits(tree):
    """[(short hash, subject)] of this branch's commits that main lacks, oldest first."""
    p = run(["git", "log", "--reverse", "--format=%h %s", "main..HEAD"], cwd=tree)
    return [tuple(l.split(" ", 1)) if " " in l else (l, "") for l in p.stdout.splitlines() if l.strip()]


def notify(title, msg):
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null;"
        "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent('ToastText02');"
        "$n=$t.GetElementsByTagName('text');"
        f"$n.Item(0).AppendChild($t.CreateTextNode('{title}')) | Out-Null;"
        f"$n.Item(1).AppendChild($t.CreateTextNode('{msg}')) | Out-Null;"
        "$app='{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($t))"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                       capture_output=True, timeout=15, creationflags=NO_WINDOW)
    except Exception:
        pass


# ---------------------------------------------------------------- servers

def pid_alive(pid):
    p = run(["tasklist", "/FI", f"PID eq {pid}", "/NH"])
    return str(pid) in p.stdout


def kill_tree(pid):
    run(["taskkill", "/PID", str(pid), "/T", "/F"])


def netstat_lines(port):
    p = run(["netstat", "-ano", "-p", "TCP"])
    out = []
    for line in p.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0] == "TCP":
            out.append((parts[1], parts[2], parts[3], parts[4]))
    return [l for l in out if l[0].rsplit(":", 1)[-1] == str(port)]


def listening(port):
    return any(l[2] == "LISTENING" for l in netstat_lines(port))


def connections(port):
    return sum(1 for l in netstat_lines(port) if l[2] == "ESTABLISHED")


def rojo_up(port, timeout=25):
    end = time.time() + timeout
    with busy(f"Waiting for Rojo to answer on port {port}"):
        while time.time() < end:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/rojo", timeout=2) as r:
                    return r.status == 200
            except Exception:
                time.sleep(0.5)
    return None


def pick_slot(cfg, name):
    if not cfg["slots"]:
        die(f"no slots on this machine yet. add one: flow slot <name> <placeId>  (config: {CONFIG})")
    slot = name or cfg["default"] or next(iter(cfg["slots"]))
    if slot not in cfg["slots"]:
        die(f"unknown slot '{slot}'. slots: {', '.join(cfg['slots'])}")
    return slot, cfg["slots"][slot]


def stop_slot(slot):
    st = state()
    info = st.pop(slot, None)
    if info and pid_alive(info["pid"]):
        kill_tree(info["pid"])
        time.sleep(0.5)
    save_state(st)
    return bool(info)


def user_only(cmd):
    # Restarting the server forces the user to reconnect Studio, so agents may not. isatty() can't tell
    # (Windows reports NUL as a tty); agent shells carry these variables.
    agent = any(os.environ.get(k) for k in ("CLAUDECODE", "AI_AGENT"))
    if agent and not os.environ.get("FLOW_ALLOW_AGENT"):
        die(f"`flow {cmd}` is a manual command for the user. Agents: don't start or stop servers; "
            "use `flow where` / `flow status`, and if nothing is served ask the user to run `flow up`.", 4)


def check_origin(main):
    """Warn when local main is missing commits from origin/main (pushed from elsewhere)."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        p = subprocess.run(["git", "fetch", "--quiet", "origin", "main"], cwd=main, capture_output=True,
                           text=True, timeout=30, env=env, creationflags=NO_WINDOW)
        fetched = p.returncode == 0
    except subprocess.TimeoutExpired:
        fetched = False
    if not fetched:
        print("WARNING: could not fetch origin (offline or no access); comparing against the last fetched origin/main")
    behind = count(main, "main..origin/main")
    ahead = count(main, "origin/main..main")
    if behind:
        print(f"WARNING: local main is {behind} commit(s) behind origin/main: changes from another device or "
              f"person aren't here. Pull before starting sessions: git -C \"{main}\" pull --ff-only")
    else:
        print("local main has everything on origin/main")
    if ahead:
        print(f"note: local main has {ahead} unpushed commit(s). New sessions start from origin/main; "
              "`flow doctor` rebases them onto local main.")
    print()


def cmd_up(a):
    user_only("up")
    cfg = config()
    main = main_checkout(Path.cwd())
    check_origin(main)
    if a.all:
        if a.name:
            die("--all serves every slot's stage; it takes no worktree name")
        targets = list(cfg["slots"])
    else:
        targets = [pick_slot(cfg, a.slot)[0]]
    done = []
    for slot in targets:
        sinfo = cfg["slots"][slot]
        print(f"--- {slot} ({sinfo['label']})")
        if a.name:
            wt = main / ".claude" / "worktrees" / a.name
            if wt.exists():
                print(f"worktree exists, reusing: {wt}")
            else:
                branch = f"claude/{a.name}"
                have = run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=main).returncode == 0
                cmd = ["git", "worktree", "add", str(wt)] + ([branch] if have else ["-b", branch, a.base])
                run(cmd, cwd=main, check=True)
                print(f"worktree created: {wt} ({branch})")
        else:
            wt = main / ".claude" / "worktrees" / stage_name(slot)
            if not wt.exists():
                run(["git", "worktree", "add", "--detach", str(wt), a.base], cwd=main, check=True)
                print(f"stage created: {wt}")
            else:
                print(f"stage: {wt}")
        serve(slot, sinfo, wt, main)
        done.append(slot)
    print()
    print("sessions claim a free slot with `flow doctor` and `flow push` their own worktree onto its stage")
    if not a.quiet:
        names = ", ".join(cfg["slots"][s]["label"] for s in done)
        notify("flow: connect Studio", names)


PKG_DIRS = ("Packages", "ServerPackages", "DevPackages")


def wally_mark(tree):
    """Per-worktree file (in its git dir, never in Packages/) holding the wally hash it was installed from."""
    d = Path(run(["git", "rev-parse", "--git-dir"], cwd=tree).stdout.strip())
    return (d if d.is_absolute() else tree / d) / "flow-wally"


def wally_hash(tree):
    h = hashlib.sha1()
    for name in ("wally.toml", "wally.lock"):
        p = tree / name
        if p.exists():
            h.update(p.read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()


def packages_current(tree):
    pk = tree / "Packages"
    mark = wally_mark(tree)
    return (pk.is_dir() and not pk.is_junction() and mark.exists()
            and mark.read_text(encoding="utf-8").strip() == wally_hash(tree))


def setup(tree):
    """Real installs, no links: rokit tools, wally packages (only when wally.toml/lock changed),
    sourcemap (every time), then package types. Returns (ok, message).
    Never call it on a tree a running rojo serves: reinstalling breaks rojo's sync of the package folders."""
    remove_junctions(tree)
    notes = []
    r = run(["rokit", "install"], cwd=tree) if (tree / "rokit.toml").exists() else None
    if r and r.returncode != 0:
        return False, f"rokit install failed: {(r.stdout + r.stderr).strip()[-300:]}"
    want = wally_hash(tree)
    mark = wally_mark(tree)
    installed = False
    if (tree / "wally.toml").exists() and not packages_current(tree):
        r = run(["wally", "install"], cwd=tree)
        if r.returncode != 0:
            return False, f"wally install failed: {(r.stdout + r.stderr).strip()[-300:]}"
        installed = True
        notes.append("wally install")
    r = run(["rojo", "sourcemap", "default.project.json", "-o", "sourcemap.json"], cwd=tree)
    if r.returncode != 0:
        return False, f"rojo sourcemap failed: {(r.stdout + r.stderr).strip()[-300:]}"
    notes.append("sourcemap")
    if installed:
        dirs = [f"{d}/" for d in PKG_DIRS if (tree / d).is_dir()]
        r = run(["wally-package-types", "--sourcemap", "sourcemap.json"] + dirs, cwd=tree)
        if r.returncode != 0:
            return False, f"wally-package-types failed: {(r.stdout + r.stderr).strip()[-300:]}"
        mark.write_text(want, encoding="utf-8")  # only after every step, so a failed run retries next time
        notes.append("package types")
    return True, "setup: " + ", ".join(notes)


def serve(slot, sinfo, wt, main):
    if stop_slot(slot):  # before setup: rojo stops syncing package folders that are reinstalled under it
        print(f"stopped the previous server on {slot}")
    regen_blink(wt)
    ok, msg = setup(wt)
    print(msg if ok else f"setup failed (continuing): {msg}")

    proj = load_json(wt / "default.project.json", {})
    if sinfo["placeId"] not in proj.get("servePlaceIds", []):
        print(f"WARNING: place {sinfo['placeId']} is not in servePlaceIds, Studio will refuse to sync")

    port = sinfo["port"]
    if port == proj.get("servePort"):
        die(f"slot {slot} uses port {port}, the main place's servePort: they would sync the same server. "
            f"Give the slot another port: flow slot {slot} {sinfo['placeId']} --port <free port>")
    if listening(port):
        die(f"port {port} is used by something that is not a flow server. free it or change the slot port in {CONFIG}")

    log = HERE / f"rojo-{slot}.log"
    flags = NO_WINDOW | 0x00000200  # NO_WINDOW | NEW_PROCESS_GROUP (DETACHED conflicts with NO_WINDOW)
    with open(log, "w") as lf:
        proc = subprocess.Popen(
            ["rojo", "serve", "--port", str(port), "default.project.json"],
            cwd=wt, stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=flags,
        )
    info = rojo_up(port)
    if not info:
        kill_tree(proc.pid)
        die(f"rojo did not answer on {port}. log: {log}")
    st = state()
    st[slot] = {"pid": proc.pid, "port": port, "worktree": str(wt), "started": time.time()}
    save_state(st)

    print(f"setup ok, port {port}. Connect Studio ({sinfo['label']}, place {sinfo['placeId']}) to localhost:{port}.")
    print(f"FLOW_UP worktree={wt} port={port} slot={slot}")


def cmd_down(a):
    user_only("down")
    cfg = config()
    if a.all or a.slot == "all":
        if a.all and a.slot and a.slot != "all":
            die("--all stops every slot; it takes no slot name")
        names = list(dict.fromkeys(list(cfg["slots"]) + list(state())))  # also servers whose slot left the config
        stopped = [n for n in names if stop_slot(n)]
        print(f"stopped {', '.join(stopped)}" if stopped else "nothing was running")
        return
    slot, _ = pick_slot(cfg, a.slot)
    print(f"stopped {slot}" if stop_slot(slot) else f"{slot} was not running")


def pull_main(main):
    """Fast-forward local main to origin/main. Returns False (and changes nothing) when it can't."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        p = subprocess.run(["git", "fetch", "--quiet", "origin", "main"], cwd=main, capture_output=True,
                           text=True, timeout=60, env=env, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        p = None
    if not p or p.returncode != 0:
        print("could not fetch origin (offline or no access)")
        return False
    behind, ahead = count(main, "main..origin/main"), count(main, "origin/main..main")
    if not behind:
        print("local main already has everything on origin/main")
        return True
    if ahead:
        print(f"local main has {ahead} unpushed commit(s) and is {behind} behind origin/main: diverged, "
              f"so no fast-forward. Merge or rebase by hand in {main}, then run this again.")
        return False
    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=main).stdout.strip()
    if branch != "main":
        print(f"the main checkout is on '{branch}', not main: can't fast-forward. Switch to main first.")
        return False
    m = run(["git", "merge", "--ff-only", "origin/main"], cwd=main)
    if m.returncode != 0:
        print(f"fast-forward failed (uncommitted changes in the way?): {m.stdout}{m.stderr}")
        return False
    print(f"local main fast-forwarded by {behind} commit(s)")
    return True


def cmd_restart(a):
    user_only("update" if a.pull else "restart")
    cfg = config()
    main = main_checkout(Path.cwd())
    if a.pull and not pull_main(main):
        die("nothing was stopped. Fix main first, or use `flow restart` to restart without pulling.", 2)
    st = state()
    if a.all:
        targets = list(cfg["slots"])
    else:
        targets = [n for n, i in st.items() if n in cfg["slots"] and pid_alive(i["pid"])]
    if not targets:
        print("no servers running; nothing to restart. (`flow up --all` starts every slot)")
        return
    done = []
    for slot in targets:
        sinfo = cfg["slots"][slot]
        prev = st.get(slot, {}).get("worktree")
        wt = Path(prev) if prev and Path(prev).exists() else main / ".claude" / "worktrees" / stage_name(slot)
        if not wt.exists():
            run(["git", "worktree", "add", "--detach", str(wt), "main"], cwd=main, check=True)
            print(f"stage created: {wt}")
        print(f"--- {slot} ({sinfo['label']})")
        serve(slot, sinfo, wt, main)
        done.append(slot)
    print()
    print("reconnect Studio on each restarted slot; sessions then `flow push` again to refresh the stamp")
    if not a.quiet:
        notify("flow: reconnect Studio", ", ".join(cfg["slots"][s]["label"] for s in done))


def slot_report(cfg, slot):
    sinfo = cfg["slots"][slot]
    info = state().get(slot)
    running = bool(info and pid_alive(info["pid"]) and listening(sinfo["port"]))
    n = connections(sinfo["port"]) if running else 0
    return sinfo, info, running, n


# ---------------------------------------------------------------- leases: one session per slot

def leases():
    return load_json(LEASES, {})


def save_leases(l):
    LEASES.write_text(json.dumps(l, indent=2), encoding="utf-8")


def lease_live(lease):
    return Path(lease["tree"]).exists() and time.time() - lease["at"] < LEASE_HOURS * 3600


def lease_of(tree):
    for slot, lease in leases().items():
        if same_path(lease["tree"], tree) and lease_live(lease) and slot in config()["slots"]:
            return slot
    return None


def touch_lease(slot, tree):
    l = leases()
    l[slot] = {"tree": str(tree), "at": time.time()}
    save_leases(l)


def stage_served(cfg, slot):
    _, info, running, n = slot_report(cfg, slot)
    return running and is_stage(info["worktree"]), n


def claim(tree, want=None):
    """This worktree's slot: its live lease, else a free served stage (Studio-connected ones first)."""
    cfg = config()
    have = lease_of(tree)
    if have and (not want or want == have):
        touch_lease(have, tree)
        return have, False
    if want:
        pick_slot(cfg, want)
        lease = leases().get(want)
        if lease and lease_live(lease) and not same_path(lease["tree"], tree):
            die(f"{want} is held by {Path(lease['tree']).name}. Never take another session's Studio; "
                "run `flow doctor` without --slot to get a free one.", 2)
        if have:
            release(have)
        touch_lease(want, tree)
        return want, True
    free = []
    for slot in cfg["slots"]:
        lease = leases().get(slot)
        if lease and lease_live(lease):
            continue
        served, n = stage_served(cfg, slot)
        if served:
            free.append((0 if n else 1, slot != cfg["default"], slot))
    if not free:
        die("no free Studio slot: every served stage is held by another session (or nothing is served). "
            "Ask the user to open another scratch place and run `flow slot <name> <placeId>` + `flow up --slot <name>` "
            "(or end a finished session with `flow release` in its worktree). Run `flow status` to show them.", 2)
    slot = sorted(free)[0][2]
    touch_lease(slot, tree)
    return slot, True


def release(slot):
    l = leases()
    l.pop(slot, None)
    save_leases(l)


def cmd_release(a):
    if a.slot:
        user_only("release --slot")
        release(a.slot)
        print(f"released {a.slot}")
        return
    slot = lease_of(toplevel())
    if not slot:
        print("this worktree holds no slot")
        return
    release(slot)
    print(f"released {slot}; another session can claim it now")


def cmd_status(a):
    cfg = config()
    ls = leases()
    for slot in cfg["slots"]:
        sinfo, info, running, n = slot_report(cfg, slot)
        lease = ls.get(slot)
        held = (f", held by {Path(lease['tree']).name} ({(time.time() - lease['at']) / 60:.0f} min ago)"
                if lease and lease_live(lease) else ", free")
        if running:
            conn = "Studio connected" if n else "NO Studio connected"
            print(f"{slot} {sinfo['label']} port {sinfo['port']} place {sinfo['placeId']}: running, {conn}, "
                  f"tree {Path(info['worktree']).name}{held}")
        else:
            print(f"{slot} {sinfo['label']} port {sinfo['port']} place {sinfo['placeId']}: not running")
    print(f"main place port {main_port()} (slots must differ)")
    clash = [slot for slot, s in cfg["slots"].items() if s["port"] == main_port()]
    if clash:
        print(f"WARNING: {', '.join(clash)} use the main place's port; fix with flow slot <name> <placeId> --port <free port>")
    print("(connected = an open TCP connection to the port; heuristic)")


def cmd_slot(a):
    """Add or update a slot: a scratch place with its own port and stage."""
    cfg = config()
    slots = cfg["slots"]
    cur = slots.get(a.name, {})
    mp = main_port()
    mine = {n: s["port"] for n, s in slots.items() if n != a.name}
    used = set(mine.values()) | other_ports() | {mp}
    port = a.port or cur.get("port") or max(used | {37844}) + 1
    if port in used:
        die(f"port {port} is already taken (main place {mp}, other slots or other projects). "
            "Every slot needs its own port, different from the main place's servePort.")
    cfg["mainPort"] = mp
    slots[a.name] = {"port": port, "placeId": a.place, "label": a.label or cur.get("label") or a.name}
    cfg["default"] = cfg.get("default") or a.name
    CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    print(f"{a.name}: {slots[a.name]['label']}, place {a.place}, port {port}")
    proj = load_json(main_checkout() / "default.project.json", {})
    if a.place not in proj.get("servePlaceIds", []):
        print(f"WARNING: place {a.place} is not in default.project.json servePlaceIds; Studio will refuse to sync")
    print(f"next: flow up --slot {a.name}, then connect that place to localhost:{port} (once)")


def cmd_where(a):
    cfg = config()
    tree = toplevel()
    slot = lease_of(tree)
    if not slot:
        die("this worktree holds no slot yet; `flow doctor` claims one", 2)
    sinfo = cfg["slots"][slot]
    _, info, running, n = slot_report(cfg, slot)
    print(f"slot={slot} label={sinfo['label']} place={sinfo['placeId']} port={sinfo['port']} "
          f"studio={'connected' if n else 'NOT connected'} stage={info['worktree'] if running else '(not served)'}")
    print(f"use the roblox-studio studio_id whose placeId is {sinfo['placeId']}, never another one")


def cmd_doctor(a):
    """Session preflight: claims this worktree's slot, then checks everything flow can check."""
    cfg = config()
    tree = toplevel()
    if is_stage(tree) or same_path(tree, main_checkout()):
        die("run this in your session's own worktree, not the main checkout or a stage", 2)
    slot, new = claim(tree, a.slot)
    sinfo = cfg["slots"][slot]
    _, info, running, n = slot_report(cfg, slot)
    print(f"{'claimed' if new else 'holding'} {slot}: {sinfo['label']}, place {sinfo['placeId']}, port {sinfo['port']}")
    ok = True

    def line(good, msg, fix=""):
        nonlocal ok
        ok = ok and good
        print(f"{'ok  ' if good else 'FAIL'} {msg}" + (f"  -> {fix}" if fix and not good else ""))

    synced, msg = sync_with_main(tree)
    line(synced, msg, "resolve it: `git rebase main`, fix the conflicts, `git rebase --continue`, then doctor again")
    line(running, f"rojo server on {slot} (port {sinfo['port']})", f"ask the user to run `flow up --slot {slot}`")
    line(running and is_stage(info["worktree"]), f"{slot} serves its stage (the flow push target)",
         f"ask the user to run `flow up --slot {slot}` with no name")
    line(running and n > 0, f"Studio ({sinfo['label']}) connected to it",
         f"ask the user to press Connect in {sinfo['label']}; `flow wait` in the background")
    for tool in ("stylua", "rojo", "git", "blink", "rokit", "wally", "luau-lsp"):
        line(shutil.which(tool) is not None, f"{tool} on PATH", "run `rokit install` in the repo")
    good, msg = setup(tree)
    line(good, msg, "fix the install error above, then doctor again")
    print(f"agent must also check: list_roblox_studios answers and lists place {sinfo['placeId']} ({sinfo['label']}). "
          "Use that studio_id for every Studio call this session, never another Studio. If the MCP is down, tell the user now.")
    print("when the session's work is done: `flow release`")
    if not ok:
        sys.exit(1)


def cmd_wait(a):
    cfg = config()
    slot, sinfo = pick_slot(cfg, a.slot or lease_of(toplevel()))
    end = time.time() + a.timeout
    n = 0
    with busy(f"Waiting for Studio ({sinfo['label']}) to connect on port {sinfo['port']}"):
        while time.time() < end:
            _, _, running, n = slot_report(cfg, slot)
            if not running:
                die(f"{slot} server is not running. run: flow up", 2)
            if n:
                break
            time.sleep(2)
    if n:
        print(f"Studio connected on port {sinfo['port']}")
        return
    die(f"timed out after {a.timeout}s waiting for Studio on port {sinfo['port']}", 3)


# ---------------------------------------------------------------- prune

def worktrees(main):
    out, cur = [], None
    for line in run(["git", "worktree", "list", "--porcelain"], cwd=main).stdout.splitlines():
        if line.startswith("worktree "):
            cur = {"path": Path(line[9:]), "branch": None, "head": None, "prunable": False}
            out.append(cur)
        elif line.startswith("HEAD "):
            cur["head"] = line[5:]
        elif line.startswith("branch "):
            cur["branch"] = line[7:].replace("refs/heads/", "")
        elif line.startswith("prunable"):
            cur["prunable"] = True
    return out


def same_path(a, b):
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


def unmerged(main, ref):
    p = run(["git", "cherry", "main", ref], cwd=main)
    return sum(1 for l in p.stdout.splitlines() if l.startswith("+"))


def remove_junctions(wt):
    for name in ("Packages", "ServerPackages", "DevPackages"):
        p = wt / name
        try:
            if p.is_junction() or p.is_symlink():
                os.rmdir(p)  # removes the link only, never the target's contents
        except OSError:
            pass


def cmd_prune(a):
    main = main_checkout()
    served = [Path(v["worktree"]) for v in state().values() if "worktree" in v]
    held = [Path(l["tree"]) for l in leases().values() if lease_live(l)]
    keep = set(a.keep or [])
    rows = []
    for w in worktrees(main)[1:]:
        name = w["path"].name
        label = w["branch"] or f"detached {w['head'][:7]}"
        if name in keep or w["branch"] in keep:
            rows.append((w, "KEEP", "--keep", label, ""))
            continue
        if any(same_path(w["path"], s) for s in served):
            rows.append((w, "KEEP", "served by flow", label, ""))
            continue
        if name != "flow-stage" and is_stage(w["path"]):
            rows.append((w, "KEEP", "a slot's stage", label, ""))
            continue
        if any(same_path(w["path"], s) for s in held):
            rows.append((w, "KEEP", "holds a Studio slot (flow release frees it)", label, ""))
            continue
        if w["prunable"] or not w["path"].exists():
            rows.append((w, "GONE", "folder missing", label, ""))
            continue
        age = run(["git", "log", "-1", "--format=%cr", w["head"]], cwd=main).stdout.strip()
        committed = int(run(["git", "log", "-1", "--format=%ct", w["head"]], cwd=main).stdout.strip() or 0)
        # git status rewrites the index, so its mtime is no signal; edited files are
        dirty = [l[3:].strip('"') for l in run(["git", "status", "--porcelain"], cwd=w["path"]).stdout.splitlines()
                 if l[3:].strip('"') != stamp_path() and l[3:] != ".claude/"]
        touched = committed
        for f in dirty:
            try:
                touched = max(touched, (w["path"] / f).stat().st_mtime)
            except OSError:
                pass
        if time.time() - touched < a.older_than * 3600:
            rows.append((w, "KEEP", f"active in the last {a.older_than:g}h (a session may be using it)", label, age))
            continue
        n = unmerged(main, w["head"])
        if dirty:
            shown = ", ".join(dirty[:2]) + (" ..." if len(dirty) > 2 else "")
            rows.append((w, "DIRTY", f"{len(dirty)} uncommitted: {shown}", label, age))
        elif n and not w["branch"]:
            rows.append((w, "ARCHIVE", f"{n} commit(s) not in main, detached: saved as flow-archive/{name}", label, age))
        elif n:
            rows.append((w, "REMOVE", f"{n} commit(s) not in main, kept on branch", label, age))
        else:
            rows.append((w, "REMOVE", "everything is in main", label, age))

    order = {"KEEP": 0, "DIRTY": 1, "ARCHIVE": 2, "REMOVE": 3, "GONE": 4}
    for w, act, why, label, age in sorted(rows, key=lambda r: (order[r[1]], str(r[0]["path"]))):
        if act == "DIRTY" and a.dirty:
            act = "REMOVE!"
        print(f"{act:8} {w['path'].name:42} {label:44} {age:16} {why}")

    todo = [r for r in rows if r[1] in ("REMOVE", "ARCHIVE", "GONE") or (r[1] == "DIRTY" and a.dirty)]
    skipped = sum(1 for r in rows if r[1] == "DIRTY" and not a.dirty)
    print(f"\n{len(todo)} to remove, {skipped} dirty kept (pass --dirty to discard their changes), "
          f"{sum(1 for r in rows if r[1] == 'KEEP')} kept")
    if not a.yes:
        print("dry run: nothing changed. Run `flow prune --yes` to do it.")
        return
    user_only("prune --yes")

    removed_branches = []
    for w, act, *_ in todo:
        if act == "GONE":
            continue
        if act == "ARCHIVE":
            run(["git", "branch", "-f", f"flow-archive/{w['path'].name}", w["head"]], cwd=main, check=True)
        remove_junctions(w["path"])
        r = run(["git", "worktree", "remove", "--force", str(w["path"])], cwd=main)
        if r.returncode != 0:
            print(f"failed {w['path'].name}: {r.stderr.strip()}")
            continue
        print(f"removed {w['path'].name}")
        if a.branches and w["branch"] and unmerged(main, w["branch"]) == 0:
            if run(["git", "branch", "-D", w["branch"]], cwd=main).returncode == 0:
                removed_branches.append(w["branch"])
    run(["git", "worktree", "prune"], cwd=main)
    if removed_branches:
        print(f"deleted {len(removed_branches)} fully-merged branch(es): {', '.join(removed_branches)}")
    print("done. Branches with work not in main were kept.")


# ---------------------------------------------------------------- project config (flow.json)

PROJECT_DEFAULTS = {
    "stamp": {
        "file": "src/Modules/Shared/FlowStamp.txt",
        "instance": "ReplicatedStorage.Source.Modules.SharedModules.FlowStamp",
    },
    "generated": ["Packages/", "ServerPackages/", "DevPackages/", "sourcemap.json", "BlinkClient.luau", "BlinkServer.luau"],
    "builtin": {"output": "error", "unused": "error", "const": "warn", "server-only": "warn"},
    "rules": [],
}
_PROJECT = None


def project():
    """The project's settings: flow.json in the main checkout over PROJECT_DEFAULTS (one level deep).
    flow.rules.json is read instead when there is no flow.json."""
    global _PROJECT
    if _PROJECT is not None:
        return _PROJECT
    cfg = json.loads(json.dumps(PROJECT_DEFAULTS))
    root = main_checkout()
    for name in ("flow.json", "flow.rules.json"):
        path = root / name
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            die(f"can't read {name}: {e}")
        for k, v in raw.items():
            cfg[k] = {**cfg[k], **v} if isinstance(cfg.get(k), dict) and isinstance(v, dict) else v
        cfg["_file"] = name
        break
    _PROJECT = cfg
    return cfg


def stamp_path():
    return project()["stamp"]["file"]


def stamp_luau():
    parts = project()["stamp"]["instance"].split(".")
    expr = f'game:GetService("{parts[0]}")'
    for part in parts[1:]:
        expr = f'{expr}:FindFirstChild("{part}")'
    return f"local v = {expr} return v and v.Value"


# ---------------------------------------------------------------- stamp


def served_tree(slot=None):
    cfg = config()
    slot, _ = pick_slot(cfg, slot)
    info = state().get(slot)
    return Path(info["worktree"]) if info and pid_alive(info["pid"]) else None


def mirror(src, dst):
    """Make dst's tracked + untracked files equal src's working tree (HEAD plus uncommitted edits)."""
    head = run(["git", "rev-parse", "HEAD"], cwd=src, check=True).stdout.strip()
    run(["git", "checkout", "--detach", "--force", head], cwd=dst, check=True)
    run(["git", "clean", "-fdq"], cwd=dst)  # ignored files (Packages links, sourcemap, stamp) stay
    copied = deleted = 0
    for line in run(["git", "status", "--porcelain", "-uall"], cwd=src).stdout.splitlines():
        code, path = line[:2], line[3:].strip('"')
        if " -> " in path:
            old, path = path.split(" -> ")
            (dst / old.strip('"')).unlink(missing_ok=True)
            path = path.strip('"')
        if path.startswith(".claude/") or path == stamp_path():
            continue
        if "D" in code:
            (dst / path).unlink(missing_ok=True)
            deleted += 1
        else:
            (dst / path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src / path, dst / path)
            copied += 1
    return head, copied, deleted


def blink_outputs(tree):
    text = (tree / "network.blink").read_text(encoding="utf-8")
    return [tree / m for m in re.findall(r'option\s+\w+Output\s*=\s*"([^"]+)"', text)]


def regen_blink(tree):
    """Regenerate the Blink networking files from network.blink so they always match the schemas."""
    tree = Path(tree)
    if not (tree / "network.blink").exists():
        return
    if not shutil.which("blink"):
        print("WARNING: blink not on PATH (rokit install): networking files NOT regenerated, may be stale")
        return
    outs = blink_outputs(tree)

    def content(o):  # git checks files out as CRLF, blink writes LF: compare without line endings
        return o.read_bytes().replace(b"\r\n", b"\n") if o.exists() else None

    before = [content(o) for o in outs]
    r = run(["blink", "network.blink"], cwd=tree)
    if r.returncode != 0:
        print(f"WARNING: blink failed, networking files may be stale:\n{(r.stdout + r.stderr).strip()[:600]}")
        return
    changed = [o.name for o, b in zip(outs, before) if content(o) != b]
    print(f"blink: regenerated {', '.join(changed)}" if changed else "blink: networking files up to date")


def cmd_push(a):
    src = toplevel()
    if not same_path(src, main_checkout()) and not is_stage(src):
        synced, msg = sync_with_main(src)
        if not synced:
            die(f"{msg}. Studio would run old code: resolve the rebase first "
                "(`git rebase main`, fix conflicts, `git rebase --continue`), then push again.", 2)
        if "rebased" in msg:
            print(msg)
    slot, new = claim(src, getattr(a, "slot", None))
    sinfo = config()["slots"][slot]
    if new:
        print(f"claimed {slot} ({sinfo['label']}, place {sinfo['placeId']})")
    dst = served_tree(slot)
    if not dst:
        die(f"nothing is served on {slot}. Ask the user to run `flow up --slot {slot}` in their terminal.", 2)
    if same_path(src, dst):
        print("this tree is the served one; just stamping")
    elif not is_stage(dst):
        die(f"{slot} serves {dst.name}, a real worktree, not a stage: pushing would overwrite its files. "
            f"Ask the user to run `flow up --slot {slot}` (no name) so its stage is served.", 2)
    else:
        with busy(f"Syncing {src.name} onto the {dst.name} stage"):
            head, copied, deleted = mirror(src, dst)
        print(f"pushed {src.name} @ {head[:7]} to {dst.name} ({copied} uncommitted file(s) copied, {deleted} deleted)")
        # never reinstall under a running rojo: wally recreates the package folders and rojo stops syncing them
        if not packages_current(dst):
            die(f"the stage's packages don't match this push's wally.toml/wally.lock (or are still links). "
                f"Ask the user to run `flow up --slot {slot}` (installs, then restarts rojo), then push again.", 2)
    regen_blink(dst)
    print(f"Studio: {sinfo['label']}, placeId {sinfo['placeId']} (pick that studio_id)")
    write_stamp(dst, src)


def cmd_stamp(a):
    a.slot = None
    return cmd_push(a)


def write_stamp(root, src):
    common = Path(run(["git", "rev-parse", "--git-common-dir"], cwd=root).stdout.strip())
    if not common.is_absolute():
        common = root / common
    excl = common / "info" / "exclude"
    excl.parent.mkdir(parents=True, exist_ok=True)
    cur = excl.read_text(encoding="utf-8") if excl.exists() else ""
    if stamp_path() not in cur:
        excl.write_text(cur.rstrip("\n") + f"\n{stamp_path()}\n", encoding="utf-8")
    head = run(["git", "rev-parse", "--short", "HEAD"], cwd=src).stdout.strip()
    dirty = [l for l in run(["git", "status", "--porcelain"], cwd=src).stdout.splitlines()
             if l[3:].strip('"') != stamp_path() and l[3:] != ".claude/"]
    value = f"{src.name}@{head}{'+dirty' if dirty else ''}:{os.urandom(3).hex()}"
    (root / stamp_path()).parent.mkdir(parents=True, exist_ok=True)
    (root / stamp_path()).write_text(value, encoding="utf-8")
    print(value)
    print(f"1. Edit datamodel, must return exactly the stamp above (else Rojo isn't synced: stop, wait for reconnect): {stamp_luau()}")
    print("2. Only then test: results from an unsynced place don't count.")


# ---------------------------------------------------------------- check

SERVER_ONLY = r"\bPrivateServerId\b|\bPrivateServerOwnerId\b|GetService\(\"(DataStoreService|MessagingService)\"\)"
FIND = []


def luau_defs():
    """Roblox type definitions for luau-lsp: FLOW_LUAU_DEFS, else the newest file the editor extension keeps
    up to date (old ones stop parsing when luau-lsp updates)."""
    env = os.environ.get("FLOW_LUAU_DEFS")
    if env:
        return Path(env) if Path(env).exists() else None
    local = main_checkout() / ".luau" / "globalTypes.d.luau"  # e.g. downloaded by the project's setup script
    if local.exists():
        return local
    appdata = Path(os.environ.get("APPDATA") or Path.home())
    found = []
    for editor in ("Code", "Cursor", "Code - Insiders"):
        found += (appdata / editor / "User" / "globalStorage" / "johnnymorganz.luau-lsp").glob("globalTypes*.d.luau")
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


DIAG = re.compile(r"^(?P<path>.+?\.luau)(?: \[[^\]]*\])?\((?P<line>\d+),\d+\): (?P<kind>\w+): (?P<msg>.*)$")


def type_check(root, luau, added_by_file):
    """luau-lsp on the changed files; only diagnostics on added lines count. Returns a status line."""
    lsp = shutil.which("luau-lsp")
    if not lsp:
        return "type check skipped: luau-lsp not on PATH (rokit install)"
    defs = luau_defs()
    if not defs:
        return "type check skipped: no Roblox definitions (put them at .luau/globalTypes.d.luau in the main checkout, or set FLOW_LUAU_DEFS)"
    p = run([lsp, "analyze", "--sourcemap=sourcemap.json", f"--definitions={defs}", "--platform=roblox"] + luau, cwd=root)
    out = p.stdout + p.stderr
    if "Failed to read definitions file" in out:
        return f"type check skipped: {defs.name} doesn't parse with this luau-lsp (update the editor extension)"
    wanted = {f.replace("\\", "/"): f for f in luau}
    diags, cur = [], None
    for raw in out.splitlines():
        m = DIAG.match(raw)
        if m:
            path = m.group("path").replace("\\", "/")
            path = os.path.relpath(path, root).replace("\\", "/") if os.path.isabs(path) else path
            cur = [path, int(m.group("line")), m.group("kind"), m.group("msg")]
            diags.append(cur)
        elif cur and raw[:1] in ("\t", " "):
            cur[3] += " " + raw.strip()
        else:
            cur = None
    shown = 0
    for path, ln, kind, msg in diags:
        if path not in wanted or ln not in added_by_file.get(wanted[path], {}):
            continue
        finding("ERROR" if kind == "TypeError" else "WARN", path, ln, f"type:{kind}", msg[:300])
        shown += 1
    return f"type check: {len(luau)} file(s) analyzed, {shown} finding(s) on changed lines"


def finding(sev, path, line, rule, msg):
    FIND.append((sev, path, line, rule, msg))


# ---------------------------------------------------------------- project rules (flow.rules.json)

def glob_re(pattern):
    """Glob to regex: ** crosses folders, * stays inside one."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile("^" + out + "$")


def load_rules(root):
    """The project's own rules (flow.json "rules"), so the flow tool itself never needs editing.
    Line rule: {id, severity, pattern, message, paths?, except?, unless?}. File rule: {id, severity, mustMatch,
    message, paths?, except?}. Returns compiled rules; a broken rule is itself an error finding."""
    raw = project().get("rules", [])
    src = project().get("_file", "flow.json")
    rules = []
    for r in raw:
        rid = r.get("id", "?")
        if r.get("severity") == "off":
            continue
        try:
            rules.append({
                "id": rid,
                "sev": "ERROR" if r.get("severity", "error") == "error" else "WARN",
                "msg": r.get("message", rid),
                "paths": [glob_re(g) for g in r.get("paths", ["src/**/*.luau"])],
                "except": [glob_re(g) for g in r.get("except", [])],
                "pattern": re.compile(r["pattern"]) if "pattern" in r else None,
                "unless": re.compile(r["unless"]) if "unless" in r else None,
                "must": re.compile(r["mustMatch"], re.M) if "mustMatch" in r else None,
            })
        except Exception as e:
            finding("ERROR", src, 0, "rules", f"rule '{rid}' is invalid: {e}")
    return rules


def apply_rules(rules, path, text, added):
    for r in rules:
        if not any(g.match(path) for g in r["paths"]) or any(g.match(path) for g in r["except"]):
            continue
        if r["pattern"]:
            for ln, line in added.items():
                if is_comment(line) or not r["pattern"].search(line):
                    continue
                if r["unless"] and r["unless"].search(line):
                    continue
                finding(r["sev"], path, ln, r["id"], r["msg"])
        if r["must"] and added and not r["must"].search(text):
            finding(r["sev"], path, 0, r["id"], r["msg"])


def changed_files(root, base):
    names = set()
    for args in (["diff", "--name-only", "--diff-filter=AMR", base],
                 ["ls-files", "--others", "--exclude-standard"]):
        p = run(["git"] + args, cwd=root)
        names.update(l.strip() for l in p.stdout.splitlines() if l.strip())
    return sorted(names)


def added_lines(root, base, path):
    full = root / path
    tracked = run(["git", "cat-file", "-e", f"{base}:{path}"], cwd=root).returncode == 0
    if not tracked:
        text = full.read_text(encoding="utf-8", errors="replace").splitlines()
        return {i + 1: l for i, l in enumerate(text)}
    p = run(["git", "diff", "-U0", base, "--", path], cwd=root)
    out, cur = {}, 0
    for line in p.stdout.splitlines():
        m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", line)
        if m:
            cur = int(m.group(1))
        elif line.startswith("+") and not line.startswith("+++"):
            out[cur] = line[1:]
            cur += 1
    return out


def is_comment(line):
    return line.strip().startswith("--")


def builtin(rule):
    """Severity of a built-in rule from flow.json "builtin": "error", "warn" or "off"."""
    sev = project()["builtin"].get(rule, "off")
    return None if sev == "off" else ("ERROR" if sev == "error" else "WARN")


def scan_file(root, base, path, text, added, rules=()):
    lines = text.splitlines()
    sev = {k: builtin(k) for k in ("output", "unused", "const", "server-only")}
    logger = re.search(r"(?:const|local)\s+print\b[^\n]*=\s*prefixedlogger\(", text)
    names_re = re.compile(r"^\s*(?:local|const)\s+(?:function\s+)?([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\s*[=(:]")

    apply_rules(rules, path, text, added)

    for ln, line in added.items():
        if is_comment(line):
            continue
        if sev["output"] and not logger and re.search(r"(?<![\w.:])(print|warn)\(", line):
            finding(sev["output"], path, ln, "output", "use prefixedlogger, not print/warn")
        if sev["server-only"] and "/Shared/" in path and re.search(SERVER_ONLY, line) and not re.search(r"IsServer|isServer", line):
            finding(sev["server-only"], path, ln, "server-only",
                    "server-only API in a shared module; guard it with RunService:IsServer() (clients warn or error)")
        m = re.match(r"^\s*local\s+([A-Za-z_]\w*)\s*=", line)
        if sev["const"] and m and not re.match(r"^\s*local\s+\w+\s*=\s*function", line):
            name = m.group(1)
            rest = "\n".join(l for i, l in enumerate(lines, 1) if i != ln)
            if not re.search(rf"(?<![\w.:]){name}\s*(=|\+=|-=|\*=|/=|\.\.=)(?!=)", rest):
                finding(sev["const"], path, ln, "const", f"'{name}' is never reassigned, use const")
        m = names_re.match(line) if sev["unused"] else None
        if m:
            for name in re.split(r"\s*,\s*", m.group(1)):
                if name.startswith("_"):
                    continue
                if len(re.findall(rf"(?<![\w.]){re.escape(name)}(?!\w)", text)) <= 1:
                    finding(sev["unused"], path, ln, "unused", f"'{name}' is declared and never used")


def cmd_check(a):
    root = toplevel()
    if a.base:
        base = a.base
    else:
        mb = run(["git", "merge-base", "HEAD", "main"], cwd=root)
        base = mb.stdout.strip() if mb.returncode == 0 and mb.stdout.strip() else "HEAD"
    files = a.files or changed_files(root, base)
    files = [f.replace("\\", "/") for f in files]
    if not files:
        print("nothing changed against", base[:10])
        return
    luau = [f for f in files if f.endswith(".luau") and (root / f).exists()]

    for f in files:
        if any(f.startswith(g) or f.endswith(g) for g in project()["generated"]):
            finding("ERROR", f, 0, "generated", "generated file touched; leave it alone")

    rules = load_rules(root)
    added_by_file = {}
    for f in luau:
        text = (root / f).read_text(encoding="utf-8", errors="replace")
        added_by_file[f] = added_lines(root, base, f)
        scan_file(root, base, f, text, added_by_file[f], rules)

    types = "type check skipped: no luau files changed"
    if luau:
        ok, msg = setup(root)
        types = type_check(root, luau, added_by_file) if ok else f"type check skipped: {msg}"

    style = []
    if luau:
        p = run(["stylua", "--check"] + luau, cwd=root)
        if p.returncode != 0:
            out = re.sub(r"\x1b\[[0-9;]*m", "", p.stdout + p.stderr)
            style = sorted(set(re.findall(r"^Diff in (.+?):\s*$", out, re.M)))
            if not style:
                finding("ERROR", "", 0, "stylua", f"stylua failed: {out.strip()[:300]}")
            for f in style:
                f = os.path.relpath(f, root) if os.path.isabs(f) else f
                finding("ERROR", f.replace("\\", "/"), 0, "stylua", f'not formatted, run: stylua "{f}"')

    order = {"ERROR": 0, "WARN": 1}
    for sev, path, ln, rule, msg in sorted(FIND, key=lambda x: (order[x[0]], x[1], x[2])):
        loc = f"{path}:{ln}" if ln else path
        print(f"{sev} {loc} [{rule}] {msg}")
    errs = sum(1 for f in FIND if f[0] == "ERROR")
    warns = len(FIND) - errs
    print(f"checked {len(files)} files ({len(luau)} luau, {len(rules)} project rules) against {base[:10]}: {errs} errors, {warns} warnings")
    print(types)
    print("not covered: behavior, feel, VFX")
    if errs:
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser(prog="flow", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"flow {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("up")
    s.add_argument("name", nargs="?")
    s.add_argument("--slot")
    s.add_argument("--base", default="main")
    s.add_argument("--all", action="store_true", help="serve every slot's stage")
    s.add_argument("--quiet", action="store_true", help="no toast notification")
    s.set_defaults(fn=cmd_up)
    s = sub.add_parser("slot", help="add or update a slot (a scratch place with its own port and stage)")
    s.add_argument("name")
    s.add_argument("place", type=int)
    s.add_argument("--label")
    s.add_argument("--port", type=int)
    s.set_defaults(fn=cmd_slot)
    s = sub.add_parser("register", help="reserve this project's main-place port so slots never share it")
    s.set_defaults(fn=cmd_register)
    s = sub.add_parser("release", help="free this worktree's Studio slot for other sessions")
    s.add_argument("--slot", help="free a given slot (user only)")
    s.set_defaults(fn=cmd_release)
    s = sub.add_parser("down", help="stop a slot's server, or every server with `down all`")
    s.add_argument("slot", nargs="?")
    s.add_argument("--all", action="store_true", help="stop every slot's server")
    s.set_defaults(fn=cmd_down)

    for name, pull, text in (("restart", False, "restart the running servers (all slots with --all)"),
                             ("update", True, "pull origin/main into local main, then restart the running servers")):
        s = sub.add_parser(name, help=text)
        s.add_argument("--all", action="store_true", help="restart every slot, not only the running ones")
        s.add_argument("--quiet", action="store_true", help="no toast notification")
        s.set_defaults(pull=pull, fn=cmd_restart)
    s = sub.add_parser("status")
    s.set_defaults(fn=cmd_status)
    s = sub.add_parser("prune", help="list (default) or remove stale worktrees; the served one is always kept")
    s.add_argument("--yes", action="store_true", help="actually remove (user only)")
    s.add_argument("--dirty", action="store_true", help="also remove worktrees with uncommitted changes (loses them)")
    s.add_argument("--branches", action="store_true", help="also delete branches whose commits are all in main")
    s.add_argument("--keep", nargs="*", help="worktree folder or branch names to keep")
    s.add_argument("--older-than", type=float, default=3, help="keep worktrees active within this many hours (default 3)")
    s.set_defaults(fn=cmd_prune)
    s = sub.add_parser("doctor", help="session preflight: claim a free Studio slot, check server, Studio, tools")
    s.add_argument("--slot", help="claim this slot (only if the user said so)")
    s.set_defaults(fn=cmd_doctor)
    s = sub.add_parser("where", help="this worktree's slot, Studio place and stage")
    s.set_defaults(fn=cmd_where)
    s = sub.add_parser("wait")
    s.add_argument("slot", nargs="?")
    s.add_argument("--timeout", type=int, default=600)
    s.set_defaults(fn=cmd_wait)
    s = sub.add_parser("push", help="mirror this worktree (HEAD + uncommitted edits) onto the served stage, then stamp")
    s.add_argument("--slot")
    s.set_defaults(fn=cmd_push)
    s = sub.add_parser("stamp", help="write a fresh FlowStamp so Studio's synced copy can be verified")
    s.set_defaults(fn=cmd_stamp)
    s = sub.add_parser("check")
    s.add_argument("files", nargs="*")
    s.add_argument("--base")
    s.set_defaults(fn=cmd_check)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
