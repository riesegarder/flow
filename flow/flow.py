#!/usr/bin/env python3
"""flow: parallel agent sessions for Rojo projects, one Studio scratch place each. Zero AI, zero tokens.

  One scratch place (slot) per session. Each slot has its own stage worktree served by its own rojo;
  a session claims a free slot and pushes its own worktree onto that slot's stage.

  flow slot <name> <placeId>   (user) add a scratch place as a slot (saved in flow.json) and serve it
  flow up [slot ...|--all]     (user) serve the slots' stages; connect each Studio once
  flow open [slot ...|--all]   (user) open the slots' places in Studio
  flow ports                   (user) renumber the slot ports from the main place port
  flow pin [version]           (user) pin this project's rokit.toml to the latest (or given) flow
  flow doctor [slot]           (session) claim a free slot + preflight
  flow push                    (session) mirror this worktree onto its slot's stage, stamp, check Studio has it
  flow verify                  (session) check Studio has the last stamp
  flow release                 (session) free the slot when done
  flow status / where / wait / log / port / prune / check
"""
import argparse
import contextlib
import hashlib
import json
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import flowui
from flowui import busy

print = flowui.say  # colored output, plain when piped

VERSION = "1.3.2"
WINDOWS = os.name == "nt"
NO_WINDOW = 0x08000000 if WINDOWS else 0  # CREATE_NO_WINDOW: no subprocess ever pops up a console window

if getattr(sys, "frozen", False) and WINDOWS:
    # the PyInstaller exe points the DLL search at its temp folder; rojo would inherit that, load its runtime
    # DLLs from there and keep the folder from being deleted when flow exits
    import ctypes

    ctypes.windll.kernel32.SetDllDirectoryW(None)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def git_out(args, cwd=None):
    p = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       creationflags=NO_WINDOW)
    return p.stdout.strip() if p.returncode == 0 else ""


def project_key():
    """Folder name of the main checkout: the name of the state folder flow <= 1.1 used."""
    out = git_out(["worktree", "list", "--porcelain"])
    first = next((l[9:] for l in out.splitlines() if l.startswith("worktree ")), "")
    return re.sub(r"[^\w.-]", "_", Path(first).name) if first else "default"


# This machine's state for a project lives in the repo's git folder (.git/flow): per project, shared by every
# worktree, never committed. Not in %LOCALAPPDATA% like flow <= 1.1: the Claude desktop app is a packaged app,
# and Windows gives everything it starts a private copy of %LOCALAPPDATA%, so flow run by sessions and flow run
# in a normal terminal read and wrote two different state files (a server "not running" with a dead pid).
_COMMON = git_out(["rev-parse", "--path-format=absolute", "--git-common-dir"])
if os.environ.get("FLOW_STATE_DIR"):
    HERE = Path(os.environ["FLOW_STATE_DIR"])
elif _COMMON:
    HERE = Path(_COMMON) / "flow"
else:
    HERE = Path.home() / ".flow" / "no-repo"
LEGACY_ROOT = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "flow"  # flow <= 1.1 kept its state here
REGISTRY = Path.home() / ".flow" / "ports.json"  # machine-wide: the ports every project uses
STATE = HERE / "state.json"  # running servers: {slot: {pid, port, worktree, started}}
LEASES = HERE / "leases.json"  # which session holds which slot
LOCAL = HERE / "machine.json"  # this machine's slot ports, default slot, machine-only slots
LEASE_HOURS = 8  # a lease nobody pushed under for this long is free again


def run(cmd, cwd=None, check=False, label=None):
    label = label or flowui.label_for(cmd)
    with busy(label) if label else contextlib.nullcontext():
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           creationflags=NO_WINDOW)
    if check and p.returncode != 0:
        die(f"{' '.join(cmd)} failed:\n{p.stdout}{p.stderr}")
    return p


def die(msg, code=1):
    sys.stdout.flush()  # what was printed before comes first, also when piped
    body = flowui.hl(msg) if flowui.use_color(sys.stderr) else msg
    sys.stderr.write(f"{flowui.prefix(sys.stderr)} {body}\n")
    sys.exit(code)


def is_agent():
    # isatty() can't tell (Windows reports NUL as a tty); agent shells carry these variables
    return any(os.environ.get(k) for k in ("CLAUDECODE", "AI_AGENT")) and not os.environ.get("FLOW_ALLOW_AGENT")


def user_only(cmd):
    # Starting or stopping a server forces the user to reconnect Studio, so agents may not.
    if is_agent():
        die(f"`flow {cmd}` is a manual command for the user. Agents: don't start or stop servers or change slots; "
            "use `flow where` / `flow status`, and if nothing is served ask the user to run `flow up <slot>`.", 4)


# ---------------------------------------------------------------- files

def load_json(path, default, strict=False):
    """A JSON file, or default when it doesn't exist. A BOM is fine (PowerShell 5.1 writes one). A broken state
    file is reported and treated as missing; a broken settings file (strict) stops flow."""
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return default
    try:
        return json.loads(text)
    except ValueError as e:
        if strict:
            die(f"can't read {path}: {e}")
        print(f"WARNING: {path} is not valid JSON ({e}); ignoring it")
        return default


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_raw(path):
    with open(path, encoding="utf-8-sig", newline="") as f:  # keeps the file's own line endings
        return f.read()


def write_raw(path, text):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


_WS = re.compile(r"\s*")


def json_members(text):
    """[(key, start, end)] of the top-level members of the JSON object in text (spans of their values), so a
    file can be edited without reformatting the rest of it."""
    dec = json.JSONDecoder()
    i = _WS.match(text, text.index("{") + 1).end()
    out = []
    while text[i] != "}":
        key, i = json.decoder.scanstring(text, i + 1)
        i = _WS.match(text, _WS.match(text, i).end() + 1).end()  # past the colon
        _, end = dec.raw_decode(text, i)
        out.append((key, i, end))
        i = _WS.match(text, end).end()
        if text[i] == ",":
            i = _WS.match(text, i + 1).end()
    return out


def newline_of(text):
    return "\r\n" if "\r\n" in text else "\n"


def indent_of(text):
    m = re.search(r"\n([ \t]+)\"", text)
    return m.group(1) if m else "\t"


def set_member(text, key, value):
    """text with its top-level member `key` set to the JSON text `value`: replaced in place, else appended."""
    members = json_members(text)
    for k, s, e in members:
        if k == key:
            return text[:s] + value + text[e:]
    nl, ind = newline_of(text), indent_of(text)
    if members:
        end = members[-1][2]
        return text[:end] + f",{nl}{ind}{json.dumps(key)}: {value}" + text[end:]
    close = text.rindex("}")
    return text[:close].rstrip() + f"{nl}{ind}{json.dumps(key)}: {value}{nl}" + text[close:]


def add_place_ids_text(text, ids):
    """(text with the ids appended to servePlaceIds, keeping the file's formatting, [the ids that were added])."""
    have = json.loads(text).get("servePlaceIds")
    missing = [i for i in dict.fromkeys(ids) if i not in (have if isinstance(have, list) else [])]
    if not missing:
        return text, []
    if not isinstance(have, list):
        return set_member(text, "servePlaceIds", "[" + ", ".join(map(str, missing)) + "]"), missing
    s, e = next((s, e) for k, s, e in json_members(text) if k == "servePlaceIds")
    arr = text[s:e]
    k = len(arr[:-1].rstrip())  # just after the last item
    if k <= 1:
        new = "[" + ", ".join(map(str, missing)) + "]"
    elif "\n" in arr:
        ind = re.match(r"[ \t]*", arr[arr.rfind("\n", 0, k) + 1:]).group()
        new = arr[:k] + "".join(f",{newline_of(text)}{ind}{i}" for i in missing) + arr[k:]
    else:
        new = arr[:k] + "".join(f", {i}" for i in missing) + arr[k:]
    return text[:s] + new + text[e:], missing


def add_place_ids(path, ids):
    if not Path(path).exists():
        return []
    text = read_raw(path)
    new, added = add_place_ids_text(text, ids)
    if added:
        write_raw(path, new)
    return added


# ---------------------------------------------------------------- repo

_MAIN = None


def main_checkout(cwd=None):
    global _MAIN
    if cwd is None and _MAIN:
        return _MAIN
    p = run(["git", "worktree", "list", "--porcelain"], cwd=cwd)
    if p.returncode != 0:
        die("not inside a git repo")
    for line in p.stdout.splitlines():
        if line.startswith("worktree "):
            path = Path(line[9:])
            if cwd is None:
                _MAIN = path
            return path
    die("no worktree found")


def toplevel(cwd=None):
    p = run(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
    if p.returncode != 0:
        die("not inside a git repo")
    return Path(p.stdout.strip())


def same_path(a, b):
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


def stage_name(slot):
    return f"flow-{slot}"


def stage_path(slot):
    return main_checkout() / ".claude" / "worktrees" / stage_name(slot)


def is_stage(path):
    name = Path(path).name
    return name == "flow-stage" or (name.startswith("flow-") and name[5:] in config()["slots"])


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
    if not WINDOWS:
        return
    q = lambda s: "'" + str(s).replace("'", "''") + "'"  # noqa: E731  (a label with an apostrophe broke the toast)
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null;"
        "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent('ToastText02');"
        "$n=$t.GetElementsByTagName('text');"
        f"$n.Item(0).AppendChild($t.CreateTextNode({q(title)})) | Out-Null;"
        f"$n.Item(1).AppendChild($t.CreateTextNode({q(msg)})) | Out-Null;"
        "$app='{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($t))"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                       capture_output=True, timeout=15, creationflags=NO_WINDOW)
    except Exception:
        pass


# ---------------------------------------------------------------- project settings (flow.json)

PROJECT_DEFAULTS = {
    "stamp": {
        "file": "src/Modules/Shared/FlowStamp.txt",
        "instance": "ReplicatedStorage.Source.Modules.SharedModules.FlowStamp",
    },
    "generated": ["Packages/", "ServerPackages/", "DevPackages/", "sourcemap.json", "BlinkClient.luau", "BlinkServer.luau"],
    "builtin": {"output": "error", "unused": "error", "const": "warn", "server-only": "warn"},
    "ignore": [".lune/**"],
    "reservedPorts": [],
    "rules": [],
    "checks": [],
    "commands": {},
}
_PROJECT = None


def project_file():
    root = main_checkout()
    for name in ("flow.json", "flow.rules.json"):
        if (root / name).exists():
            return root / name
    return root / "flow.json"


def project():
    """The project's settings: flow.json in the main checkout over PROJECT_DEFAULTS (one level deep).
    flow.rules.json is read instead when there is no flow.json."""
    global _PROJECT
    if _PROJECT is not None:
        return _PROJECT
    cfg = json.loads(json.dumps(PROJECT_DEFAULTS))
    path = project_file()
    if path.exists():
        raw = load_json(path, {}, strict=True)
        for k, v in raw.items():
            cfg[k] = {**cfg[k], **v} if isinstance(cfg.get(k), dict) and isinstance(v, dict) else v
        cfg["_file"] = path.name
    _PROJECT = cfg
    return cfg


def save_slots(slots):
    """Write the slots into the project's flow.json (main checkout), leaving the rest of the file as it is."""
    global _PROJECT, _CFG
    path = project_file()
    text = read_raw(path) if path.exists() else "{\n}\n"
    nl, ind = newline_of(text), indent_of(text)
    if slots:
        body = f",{nl}".join(f"{ind}{ind}{json.dumps(n)}: {json.dumps(s)}" for n, s in slots.items())
        value = "{" + nl + body + nl + ind + "}"
    else:
        value = "{}"
    write_raw(path, set_member(text, "slots", value))
    _PROJECT = _CFG = None


# ---------------------------------------------------------------- slots and ports

def host():
    return (os.environ.get("COMPUTERNAME") or platform.node() or "?").upper()


def local():
    m = load_json(LOCAL, {})
    m.setdefault("ports", {})
    return m


def save_local(m):
    global _CFG
    write_json(LOCAL, m)
    _CFG = None


def migrate():
    """First run of 1.2 in a project: take over this machine's slots, ports and leases from flow <= 1.1."""
    if LOCAL.exists() or not _COMMON:
        return
    old = LEGACY_ROOT / project_key()
    cfg = load_json(old / "config.json", {})
    slots = cfg.get("slots") or {}
    m = {"ports": {n: s["port"] for n, s in slots.items() if s.get("port")}, "default": cfg.get("default")}
    if slots:
        m["legacySlots"] = {n: {"placeId": s["placeId"], "label": s.get("label") or n} for n, s in slots.items()}
        for name in ("state.json", "leases.json"):
            data = load_json(old / name, None)
            if data and not (HERE / name).exists():
                write_json(HERE / name, data)
        print(f"note: flow 1.2 keeps this machine's state in {HERE}; took over the slots, ports and leases from {old}")
    save_local(m)


def adopt_slots():
    """Save slots that only this machine knew (flow <= 1.1) in flow.json, so every device gets them."""
    m = local()
    legacy = m.get("legacySlots")
    if not legacy or is_agent():
        return
    shared = dict(project().get("slots") or {})
    added, clash = [], {}
    for n, s in legacy.items():
        cur = shared.get(n)
        if cur is None:
            shared[n] = s
            added.append(n)
        elif int(cur.get("placeId", 0)) != int(s["placeId"]):
            clash[n] = s
    if added:
        save_slots(shared)
        print(f"note: saved this machine's slots {', '.join(added)} in {project_file().name}: commit it so other "
              "devices get them")
    m = local()
    if clash:
        m.setdefault("slots", {}).update(clash)
        print(f"WARNING: {', '.join(clash)} use other places in {project_file().name}; this machine keeps its own "
              f"(in {LOCAL}). Give them other names with `flow slot <name> <placeId>` to share them.")
    m.pop("legacySlots", None)
    save_local(m)


def slot_defs():
    """{name: {placeId, label}} of the slots on this machine, in order: flow.json's (shared by every device; a
    slot with "hosts" only on those machines), then this machine's own."""
    out = {}
    for name, s in (project().get("slots") or {}).items():
        hosts = [h.upper() for h in s.get("hosts", [])]
        if not hosts or host() in hosts:
            out[name] = {"placeId": int(s["placeId"]), "label": s.get("label") or name}
    m = local()
    for name, s in (m.get("slots") or m.get("legacySlots") or {}).items():
        out[name] = {"placeId": int(s["placeId"]), "label": s.get("label") or name}
    return out


def main_port():
    """The port of the main place: servePort in the main checkout's default.project.json."""
    return load_json(main_checkout() / "default.project.json", {}).get("servePort", 34872)


_OTHER = None


def other_ports():
    """Ports slots must not get: other projects' on this machine (their main place and their slots), and the
    project's own reservedPorts (flow.json: ports its other tools listen on)."""
    global _OTHER
    if _OTHER is not None:
        return _OTHER
    mine = os.path.normcase(str(main_checkout()))
    used, names = {int(p) for p in project().get("reservedPorts") or []}, set()
    for path, e in load_json(REGISTRY, {}).items():
        if os.path.normcase(path) == mine or not Path(path).exists():
            continue
        names.add(re.sub(r"[^\w.-]", "_", Path(path).name))
        used.add(e.get("main", 0))
        used.update(e.get("slots", {}).values())
    for path in LEGACY_ROOT.glob("*/config.json"):  # projects still on flow <= 1.1
        if path.parent.name in names or path.parent.name == project_key():
            continue
        c = load_json(path, {})
        used.update(s.get("port", 0) for s in c.get("slots", {}).values())
        used.add(c.get("mainPort", 0))
    used.add(DISCOVERY_PORT)
    used.discard(0)
    used.discard(None)
    _OTHER = used
    return used


def note_ports(slots):
    """Record this project's ports machine-wide, so no other project hands them out."""
    reg = load_json(REGISTRY, {})
    gone = [p for p in reg if not Path(p).exists()]
    for p in gone:
        reg.pop(p)
    key = str(main_checkout())
    entry = {"main": main_port(), "slots": slots}
    if gone or reg.get(key) != entry:
        reg[key] = entry
        write_json(REGISTRY, reg)


_CFG = None


def config():
    """{"slots": {name: {placeId, label, port}}, "default": name} for this machine. A slot without a port gets
    the main place port + its position, or the next free port after that."""
    global _CFG
    if _CFG is None:
        defs = slot_defs()
        m = local()
        ports = m["ports"]
        used = other_ports() | {main_port()}
        changed = False
        for i, n in enumerate(defs, 1):
            if n not in ports:
                p = main_port() + i
                while p in used or p in ports.values():
                    p += 1
                ports[n] = p
                changed = True
        if changed:
            write_json(LOCAL, m)
        note_ports({n: ports[n] for n in defs})
        default = m.get("default") if m.get("default") in defs else next(iter(defs), None)
        _CFG = {"slots": {n: {**d, "port": ports[n]} for n, d in defs.items()}, "default": default}
        flowui.SLOT_NAMES = set(defs)
    return _CFG


def reconfig():
    global _CFG
    _CFG = None
    return config()


def pick_slot(cfg, name):
    if not cfg["slots"]:
        die(f"no slots on this machine yet. add one: flow slot <name> <placeId>  (saved in {project_file().name})")
    slot = name or cfg["default"] or next(iter(cfg["slots"]))
    if slot not in cfg["slots"]:
        die(f"unknown slot '{slot}'. slots: {', '.join(cfg['slots'])}")
    return slot, cfg["slots"][slot]


def check_slots(cfg, names):
    for n in names:
        if n not in cfg["slots"]:
            tree = main_checkout() / ".claude" / "worktrees" / n
            hint = f" To serve the worktree {n} instead of a stage: flow up <slot> --tree {n}" if tree.exists() else ""
            known = ", ".join(cfg["slots"]) or "none yet (flow slot <name> <placeId>)"
            die(f"unknown slot '{n}'. slots: {known}.{hint}")


def slot_args(a):
    names = list(getattr(a, "slots", None) or [])
    for k in ("slot", "slot_pos"):
        if getattr(a, k, None):
            names.append(getattr(a, k))
    return list(dict.fromkeys(names))


def one_slot(a):
    names = slot_args(a)
    if len(names) > 1:
        die(f"give one slot, not {', '.join(names)}")
    return names[0] if names else None


def port_list(slots):
    cfg = config()
    return ", ".join(f"{s} [{cfg['slots'][s]['port']}]" for s in slots)


# ---------------------------------------------------------------- servers

def in_job():
    """True when this process runs inside a Windows job object. rokit runs every tool in a job that is killed
    when the tool exits, which would take any server started from here down with it."""
    if not WINDOWS:
        return False
    import ctypes

    result = ctypes.c_int(0)
    k32 = ctypes.windll.kernel32
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    ok = k32.IsProcessInJob(ctypes.c_void_p(k32.GetCurrentProcess()), None, ctypes.byref(result))
    return bool(ok and result.value)


def spawn_detached(cmd, cwd, log):
    """Start a long-lived server writing to log and return its (launcher) pid. Outside a job a plain Popen is
    enough; inside one (rokit) the process is created by WMI instead, so it is not part of the job and outlives
    flow. The WMI process gets the user's normal environment, not this one."""
    Path(log).parent.mkdir(parents=True, exist_ok=True)
    if not in_job():
        with open(log, "w") as lf:
            return subprocess.Popen(cmd, cwd=cwd, stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    creationflags=NO_WINDOW | 0x00000200).pid  # NO_WINDOW | NEW_PROCESS_GROUP
    inner = f'{subprocess.list2cmdline(cmd)} > "{log}" 2>&1'
    return create_outside_job(f'cmd.exe /d /s /c "{inner}"', cwd, cmd[0])


def create_outside_job(line, cwd, what, show=False):
    """Start a command line through WMI and return its pid. WMI's process is not part of rokit's job, which kills
    everything in it when flow exits. It gets the user's normal environment, not this one."""
    q = lambda s: "'" + str(s).replace("'", "''") + "'"  # noqa: E731
    ps = (
        # ShowWindow: 1 shows the window (SW_SHOWNORMAL), 0 hides it (SW_HIDE)
        f"$s = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{{ShowWindow=[uint16]{int(show)}}};"
        f"$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{{CommandLine={q(line)};"
        f"CurrentDirectory={q(cwd)}; ProcessStartupInformation=$s}};"
        "if ($r.ReturnValue -ne 0) { exit [int]$r.ReturnValue }; $r.ProcessId"
    )
    p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,
                       text=True, creationflags=NO_WINDOW)
    pid = p.stdout.strip().splitlines()[-1] if p.stdout.strip() else ""
    if p.returncode != 0 or not pid.isdigit():
        die(f"could not start {what} outside rokit's job (WMI): {(p.stdout + p.stderr).strip()[-300:]}")
    return int(pid)


_SNAP = None


def snapshot(fresh=False):
    """(listening {port: pid}, established connections {port: count}, process names {pid: exe}): one netstat
    and one tasklist, reused for 2 seconds."""
    global _SNAP
    if fresh or _SNAP is None or time.time() - _SNAP[0] > 2:
        listen, est = {}, {}
        for line in run(["netstat", "-ano", "-p", "TCP"]).stdout.splitlines():
            parts = line.split()
            if len(parts) < 5 or parts[0] != "TCP" or not parts[4].isdigit():
                continue
            port = parts[1].rsplit(":", 1)[-1]
            if not port.isdigit():
                continue
            if parts[3] == "LISTENING":
                listen[int(port)] = int(parts[4])
            elif parts[3] == "ESTABLISHED":
                est[int(port)] = est.get(int(port), 0) + 1
        names = {}
        for line in run(["tasklist", "/FO", "CSV", "/NH"]).stdout.splitlines():
            cells = [c.strip('"') for c in line.split('","')]
            if len(cells) >= 2 and cells[1].isdigit():
                names[int(cells[1])] = cells[0].lower()
        _SNAP = (time.time(), listen, est, names)
    return _SNAP[1:]


def forget_snapshot():
    global _SNAP
    _SNAP = None


def rojo_listeners():
    """{port: pid} of every rojo process listening on this machine."""
    listen, _, names = snapshot()
    return {port: pid for port, pid in listen.items() if names.get(pid, "").startswith("rojo")}


def listening(port):
    return port in snapshot()[0]


def connections(port):
    return snapshot()[1].get(port, 0)


def pid_alive(pid):
    return pid in snapshot()[2]


def kill_tree(pid):
    run(["taskkill", "/PID", str(pid), "/T", "/F"])
    forget_snapshot()


def log_path(slot):
    return HERE / f"rojo-{slot}.log"


def log_tail(slot, n=6):
    try:
        text = re.sub(r"\x1b\[[0-9;]*m", "", log_path(slot).read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ""
    lines = [l for l in text.strip().splitlines() if l.strip() and l.strip() != "[ERROR rojo]"]
    crash = next((i for i, l in enumerate(lines) if "Rojo crashed" in l), None)
    lines = [lines[crash]] + [l for l in lines[crash + 1:] if "Details" in l or "in file" in l] if crash is not None else lines
    return "\n".join("   " + l for l in lines[-n:])


def crashed(slot):
    try:
        return "Rojo crashed" in log_path(slot).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


ROBLOX_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Roblox"
FLOW_PLUGIN = ROBLOX_DIR / "Plugins" / "RojoFlowPlugin.rbxm"  # where `rojo-flow plugin install` puts it
FLOW_PLUGIN_STAMP = Path.home() / ".flow" / "rojo-flow-plugin.json"  # which rojo-flow installed that file
PLACES = Path.home() / ".flow" / "places.json"  # {placeId: {port, slot, project}}: rojo-flow serves it to Studio
DISCOVERY_PORT = 34870  # rojo-flow servers serve PLACES here; slots never get it


def rojo_flow():
    """The rojo-flow executable: FLOW_ROJO_FLOW (a path, for testing a build), else rojo-flow on PATH."""
    exe = os.environ.get("FLOW_ROJO_FLOW") or shutil.which("rojo-flow")
    if not exe:
        die("rojo-flow is not installed. flow serves slots with it (https://github.com/riesegarder/rojo-flow): "
            "`rokit add --global riesegarder/rojo-flow`, then `rokit trust riesegarder/rojo-flow`")
    return exe


def ensure_flow_plugin(exe):
    """Install rojo-flow's Studio plugin when it's missing, or was installed by another rojo-flow version or
    changed since. Studio loads the new file by itself, no restart."""
    version = run([exe, "--version"]).stdout.strip()
    try:
        st = FLOW_PLUGIN.stat()
        key = [st.st_mtime, st.st_size]
    except OSError:
        key = None
    if key and load_json(FLOW_PLUGIN_STAMP, {}) == {"version": version, "key": key}:
        return
    r = run([exe, "plugin", "install"], label="Installing the rojo-flow Studio plugin")
    if r.returncode != 0:
        print(f"WARNING: `rojo-flow plugin install` failed, Studio won't sync by itself:\n{(r.stdout + r.stderr).strip()}")
        return
    st = FLOW_PLUGIN.stat()
    write_json(FLOW_PLUGIN_STAMP, {"version": version, "key": [st.st_mtime, st.st_size]})
    print(f"installed the rojo-flow Studio plugin ({version}); open Studios load it by themselves")


def places_set(slot, sinfo, port):
    """Tell rojo-flow's plugin which port serves this slot's place."""
    places = load_json(PLACES, {})
    places[str(sinfo["placeId"])] = {"port": port, "slot": slot, "project": str(main_checkout())}
    write_json(PLACES, places)


def places_drop(slot=None, port=None):
    """Forget this project's entry for slot, or any entry on port: the plugin stops looking for it."""
    places = load_json(PLACES, {})
    mine = os.path.normcase(str(main_checkout())) if slot else None
    keep = {pid: e for pid, e in places.items()
            if not ((slot and e.get("slot") == slot and os.path.normcase(e.get("project", "")) == mine)
                    or (port and e.get("port") == port))}
    if keep != places:
        write_json(PLACES, keep)


def semver(text):
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(x) for x in m.groups()) if m else None


def plugin_problems():
    """Warnings about Studio's side of the sync."""
    if FLOW_PLUGIN.exists():
        return []
    return [f"note: the rojo-flow Studio plugin isn't in {FLOW_PLUGIN.parent} yet; `flow up` installs it"]


def plugin_reconnects():
    """True when Studio syncs a slot by itself after its server (re)starts: the rojo-flow plugin is installed.
    FLOW_ROJO_RECONNECT=1/0 overrides the detection."""
    env = os.environ.get("FLOW_ROJO_RECONNECT")
    if env in ("0", "1"):
        return env == "1"
    return FLOW_PLUGIN.exists()


def studio_connected(port):
    forget_snapshot()
    return bool(rojo_listeners().get(port)) and connections(port) > 0


def await_reconnect(slots, quiet=False, timeout=30):
    """Studio's plugin reconnects to these slots by itself: say so, wait until it has, say that too.
    Returns the slots that didn't come back (they need a manual Connect)."""
    cfg = config()
    if not quiet:
        notify("flow: reconnecting", port_list(slots))
    left = list(slots)
    end = time.time() + timeout
    with busy(f"Waiting for Studio to reconnect to {', '.join(slots)}"):
        while left and time.time() < end:
            time.sleep(1)
            left = [s for s in left if not studio_connected(cfg["slots"][s]["port"])]
    back = [s for s in slots if s not in left]
    if back:
        print(f"Studio reconnected by itself: {port_list(back)}")
        if not quiet:
            notify("flow: reconnected", port_list(back))
    if left:
        print(f"WARNING: Studio didn't reconnect within {timeout}s: {port_list(left)}. "
              + ("Is the place still open in Studio?" if plugin_reconnects() else "Press Connect there."))
        if not quiet:
            notify("flow: connect Studio", port_list(left))
    return left


def await_open(slots, quiet=False, timeout=8, hint=True):
    """Slots Studio wasn't connected to: rojo-flow's plugin syncs each one by itself once its place is open, so
    wait briefly for places that already are, and say which ones still need opening."""
    cfg = config()
    left = list(slots)
    end = time.time() + timeout
    with busy(f"Waiting for Studio to sync {', '.join(slots)}"):
        while left and time.time() < end:
            time.sleep(1)
            left = [s for s in left if not studio_connected(cfg["slots"][s]["port"])]
    back = [s for s in slots if s not in left]
    if back:
        print(f"Studio synced by itself: {port_list(back)}")
    if left and hint:
        print("open these places in Studio (`flow open <slot>`); they sync by themselves: "
              + ", ".join(f"{s} ({cfg['slots'][s]['label']})" for s in left))
        if not quiet:
            notify("flow: open in Studio", port_list(left))
    return left


def rojo_up(port, timeout=25):
    end = time.time() + timeout
    with busy(f"Waiting for Rojo to answer on port {port}"):
        while time.time() < end:
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{port}/api/rojo", headers={"Rojo-Flow": "1"})
                with urllib.request.urlopen(req, timeout=2) as r:
                    return r.status == 200
            except Exception:
                time.sleep(0.5)
    return None


# ---------------------------------------------------------------- Studio MCP: flow reads Studio itself

class StudioError(Exception):
    pass


class StudioMCP:
    """Roblox's StudioMCP.exe over stdio, the server behind Claude's roblox-studio MCP. flow uses it to find the
    Studio a place is open in and to run read-only Luau there (reading the FlowStamp back)."""

    def __init__(self):
        self.p = None
        self.lines = queue.Queue()
        self.n = 0

    @staticmethod
    def exe():
        env = os.environ.get("FLOW_STUDIO_MCP")
        if env:
            return env if Path(env).exists() else None
        found = []
        for p in (ROBLOX_DIR / "Versions").glob("*/StudioMCP.exe"):
            try:
                found.append((p.stat().st_mtime, str(p)))
            except OSError:
                pass
        return max(found)[1] if found else None

    def __enter__(self):
        exe = self.exe()
        if not exe:
            raise StudioError(f"StudioMCP.exe not found under {ROBLOX_DIR / 'Versions'}")
        self.p = subprocess.Popen([exe], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  creationflags=NO_WINDOW)
        threading.Thread(target=self._read, daemon=True).start()
        try:
            self.request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                        "clientInfo": {"name": "flow", "version": VERSION}}, timeout=10)
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except Exception:
            self.__exit__()
            raise
        return self

    def __exit__(self, *exc):
        try:
            self.p.kill()
        except Exception:
            pass

    def _read(self):
        for line in self.p.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def _send(self, msg):
        self.p.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        self.p.stdin.flush()

    def request(self, method, params, timeout=15):
        self.n += 1
        mid = self.n
        try:
            self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        except OSError as e:
            raise StudioError(f"Studio MCP exited ({e})")
        end = time.time() + timeout
        while True:
            try:
                line = self.lines.get(timeout=max(0.05, end - time.time()))
            except queue.Empty:
                if time.time() >= end:
                    raise StudioError(f"Studio MCP didn't answer {method} within {timeout}s")
                continue
            if line is None:
                raise StudioError("Studio MCP exited")
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == mid:
                if "error" in msg:
                    raise StudioError(str(msg["error"].get("message", msg["error"])))
                return msg.get("result") or {}

    def tool(self, name, args, timeout=15):
        r = self.request("tools/call", {"name": name, "arguments": args}, timeout)
        text = "\n".join(c.get("text", "") for c in r.get("content", []) if isinstance(c, dict))
        if r.get("isError"):
            raise StudioError(text.strip()[-300:] or f"{name} failed")
        return text

    def studios(self):
        return json.loads(self.tool("list_roblox_studios", {}) or "{}").get("studios", [])

    def studios_for(self, place_id):
        return [s["id"] for s in self.studios() if int(s.get("placeId") or 0) == int(place_id)]

    def luau(self, studio_id, code, datamodel="Edit", timeout=15):
        return self.tool("execute_luau", {"studio_id": studio_id, "datamodel_type": datamodel, "code": code}, timeout)


def find_studio(place_id):
    """(studio_id, None) of the Studio that has this place open, or (None, why not)."""
    try:
        with StudioMCP() as mcp:
            ids = mcp.studios_for(place_id)
    except (StudioError, OSError, ValueError) as e:
        return None, f"the Studio MCP didn't answer: {e}"
    if not ids:
        return None, f"no open Studio has place {place_id} (or Studio's MCP server is off)"
    return ids[0], None


STAMP_TAG = "flowstamp="


def stamp_check(place_id, want, timeout=20):
    """Read the FlowStamp back from the Studio that has place_id open (Edit datamodel, read-only) until it equals
    want. Returns (status, studio_id, detail): "synced" (detail: how long it took), "stale" (detail: Studio's
    value) or "unavailable" (detail: why flow couldn't read it)."""
    code = f'{stamp_lookup()} return "{STAMP_TAG}" .. tostring(v and v.Value)'
    start = time.time()
    try:
        with StudioMCP() as mcp, busy("Reading the stamp back from Studio"):
            ids = mcp.studios_for(place_id)
            if not ids:
                return "unavailable", None, f"no open Studio has place {place_id} (or Studio's MCP server is off)"
            have = None
            while True:
                for sid in ids:
                    out = mcp.luau(sid, code).strip()
                    have = out.split(STAMP_TAG, 1)[1] if STAMP_TAG in out else out
                    if have == want:
                        return "synced", sid, f"{time.time() - start:.1f}s"
                if time.time() - start > timeout:
                    return "stale", ids[0], have
                time.sleep(0.5)
    except (StudioError, OSError, ValueError) as e:
        return "unavailable", None, str(e)


def state():
    return load_json(STATE, {})


def save_state(s):
    write_json(STATE, s)


def slot_report(cfg, slot):
    """(slot settings, state entry, running, Studio connections). Running means a rojo listens on the slot's
    port, whatever pid state.json recorded; the entry is refreshed when it's off."""
    sinfo = cfg["slots"][slot]
    pid = rojo_listeners().get(sinfo["port"])
    st = state()
    info = st.get(slot)
    if pid and (not info or info.get("pid") != pid or info.get("port") != sinfo["port"]):
        info = {"pid": pid, "port": sinfo["port"], "worktree": (info or {}).get("worktree") or str(stage_path(slot)),
                "started": (info or {}).get("started", time.time())}
        st[slot] = info
        save_state(st)
    running = bool(pid)
    return sinfo, info, running, connections(sinfo["port"]) if running else 0


def stop_slot(slot):
    """Stop the slot's server: any rojo on the slot's port (or the port state.json recorded), and the launcher
    state.json recorded if it's still there."""
    places_drop(slot=slot)
    st = state()
    info = st.pop(slot, None) or {}
    ports = {config()["slots"].get(slot, {}).get("port"), info.get("port")} - {None}
    forget_snapshot()
    stopped = False
    for port in ports:
        pid = rojo_listeners().get(port)
        if pid:
            kill_tree(pid)
            stopped = True
    pid = info.get("pid")
    if pid and snapshot()[2].get(pid) in ("cmd.exe", "rojo.exe", "rojo-flow.exe"):
        kill_tree(pid)
        stopped = True
    if stopped:
        time.sleep(0.5)
        forget_snapshot()
    save_state(st)
    return stopped


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
    names = slot_args(a)
    if a.all:
        if names or a.tree:
            die("--all serves every slot's stage; give no slot names and no --tree")
        targets = list(cfg["slots"])
    else:
        check_slots(cfg, names)
        targets = names or [pick_slot(cfg, None)[0]]
    if a.tree and len(targets) > 1:
        die("--tree serves one worktree: give one slot")
    check_origin(main_checkout())
    up_slots(targets, tree=a.tree, base=a.base, quiet=a.quiet, open_studio=not a.no_open)


def up_slots(targets, tree=None, base="main", quiet=False, open_studio=True):
    cfg = config()
    main = main_checkout()
    done, auto = [], []
    for slot in targets:
        sinfo = cfg["slots"][slot]
        print(f"--- {slot} ({sinfo['label']})")
        if tree:
            wt = main / ".claude" / "worktrees" / tree
            if wt.exists():
                print(f"worktree exists, reusing: {wt}")
            else:
                branch = f"claude/{tree}"
                have = run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=main).returncode == 0
                cmd = ["git", "worktree", "add", str(wt)] + ([branch] if have else ["-b", branch, base])
                run(cmd, cwd=main, check=True)
                print(f"worktree created: {wt} ({branch})")
        else:
            wt = stage_path(slot)
            if not wt.exists():
                run(["git", "worktree", "add", "--detach", str(wt), base], cwd=main, check=True)
                print(f"stage created: {wt}")
            else:
                print(f"stage: {wt}")
        if serve(slot, sinfo, wt, main):
            auto.append(slot)
        else:
            done.append(slot)
    print()
    if done and plugin_reconnects():
        left = await_open(done, quiet, hint=not open_studio)
        if left and open_studio:
            open_places(cfg, left)
    elif not quiet and done:
        notify("flow: connect Studio", port_list(done))
    if auto:
        await_reconnect(auto, quiet)
    print("sessions claim a free slot with `flow doctor` and `flow push` their own worktree onto its stage")


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
    if not (tree / "wally.toml").exists():
        return True  # no Wally packages in this project
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


def slot_place_ids():
    return [s["placeId"] for s in config()["slots"].values()]


def serve(slot, sinfo, wt, main):
    """Serve wt on the slot's port with rojo-flow. Returns True when Studio was connected to the old server on
    this port and its plugin reconnects by itself (rojo-flow's plugin), so nobody has to press Connect."""
    was = studio_connected(sinfo["port"])
    if stop_slot(slot):  # before setup: rojo stops syncing package folders that are reinstalled under it
        print(f"stopped the previous server on {slot}")
    regen_blink(wt)
    ok, msg = setup(wt)
    print(msg if ok else f"setup failed (continuing): {msg}")

    if is_stage(wt):  # a stage is main's code: it lacks places added since the last commit
        added = add_place_ids(wt / "default.project.json", slot_place_ids())
        if added:
            print(f"stage: added place(s) {', '.join(map(str, added))} to servePlaceIds")
    proj = load_json(wt / "default.project.json", {})
    if sinfo["placeId"] not in proj.get("servePlaceIds", []):
        print(f"WARNING: place {sinfo['placeId']} is not in servePlaceIds, Studio will refuse to sync")

    port = sinfo["port"]
    if port == proj.get("servePort"):
        die(f"slot {slot} uses port {port}, the main place's servePort: they would sync the same server. "
            f"Renumber the slots with `flow ports`.")
    forget_snapshot()
    if listening(port):
        pid = snapshot()[0][port]
        die(f"port {port} is used by {snapshot()[2].get(pid, 'another program')} (pid {pid}), not a flow server. "
            "Free it, or move the slots with `flow ports`.")

    exe = rojo_flow()
    ensure_flow_plugin(exe)
    log = log_path(slot)
    launcher = spawn_detached([exe, "serve", "--port", str(port), "default.project.json"], wt, log)
    if not rojo_up(port):
        kill_tree(launcher)
        die(f"rojo did not answer on {port}. log: {log}\n{log_tail(slot)}")
    forget_snapshot()
    pid = rojo_listeners().get(port, launcher)  # the real rojo-flow.exe, behind cmd and rokit's shim
    st = state()
    st[slot] = {"pid": pid, "port": port, "worktree": str(wt), "started": time.time()}
    save_state(st)
    places_set(slot, sinfo, port)

    print(f"setup ok, port {port}. Studio syncs {sinfo['label']} (place {sinfo['placeId']}) by itself once it's open.")
    print(f"FLOW_UP worktree={wt} port={port} slot={slot}")
    return was and plugin_reconnects()


def cmd_down(a):
    user_only("down")
    if a.port:
        pid = rojo_listeners().get(a.port)
        if not pid:
            die(f"no rojo is listening on port {a.port}")
        places_drop(port=a.port)
        kill_tree(pid)
        print(f"stopped rojo on port {a.port} (pid {pid})")
        return
    cfg = config()
    names = slot_args(a)
    if a.all or "all" in names:
        if set(names) - {"all"}:
            die("`down all` stops every slot; it takes no slot names")
        names = list(dict.fromkeys(list(cfg["slots"]) + list(state())))  # also servers whose slot left the config
        stopped = [n for n in names if stop_slot(n)]
        print(f"stopped {', '.join(stopped)}" if stopped else "nothing was running")
        return
    check_slots(cfg, names)
    for slot in names or [pick_slot(cfg, None)[0]]:
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


def served_worktree(slot, main):
    prev = state().get(slot, {}).get("worktree")
    return Path(prev) if prev and Path(prev).exists() else stage_path(slot)


def cmd_restart(a):
    user_only("update" if a.pull else "restart")
    cfg = config()
    main = main_checkout()
    names = slot_args(a)
    check_slots(cfg, names)
    if a.pull and not pull_main(main):
        die("nothing was stopped. Fix main first, or use `flow restart` to restart without pulling.", 2)
    if a.all:
        targets = list(cfg["slots"])
    elif names:
        targets = names
    else:
        targets = [n for n in cfg["slots"] if slot_report(cfg, n)[2]]
    if not targets:
        print("no servers running; nothing to restart. (`flow up --all` starts every slot)")
        return
    manual, auto = [], []
    for slot in targets:
        sinfo = cfg["slots"][slot]
        wt = served_worktree(slot, main)
        if not wt.exists():
            run(["git", "worktree", "add", "--detach", str(wt), "main"], cwd=main, check=True)
            print(f"stage created: {wt}")
        print(f"--- {slot} ({sinfo['label']})")
        (auto if serve(slot, sinfo, wt, main) else manual).append(slot)
    print()
    if manual and plugin_reconnects():
        await_open(manual, a.quiet)
    elif manual:
        print(f"reconnect Studio on {', '.join(manual)}")
        if not a.quiet:
            notify("flow: reconnect Studio", port_list(manual))
    if auto:
        await_reconnect(auto, a.quiet)
    print("sessions then `flow push` again to refresh the stamp")


def cmd_ports(a):
    """Renumber the slots from the main place port: main + 1, main + 2, ... in slot order, skipping ports other
    projects or other programs use. Running servers move to their new port."""
    cfg = config()
    mp = main_port()
    mine = {s["port"] for s in cfg["slots"].values()}
    forget_snapshot()
    used = other_ports() | {mp} | {p for p in snapshot()[0] if p not in mine}
    plan, nxt = {}, mp + 1
    for n in cfg["slots"]:
        while nxt in used:
            nxt += 1
        plan[n] = nxt
        nxt += 1
    moves = {n: p for n, p in plan.items() if cfg["slots"][n]["port"] != p}
    for n, p in plan.items():
        old = cfg["slots"][n]["port"]
        print(f"{n} {cfg['slots'][n]['label']}: port {old}" + (f" -> {p}" if old != p else " (unchanged)"))
    if not moves:
        print(f"slot ports already count up from the main place port {mp}")
        return
    if a.dry_run:
        print("dry run: nothing changed. Run `flow ports` to renumber.")
        return
    user_only("ports")
    running = {n: served_worktree(n, main_checkout()) for n in moves if slot_report(cfg, n)[2]}
    for n in running:  # all first: a new port may be one another slot still has
        stop_slot(n)
    m = local()
    m["ports"].update(moves)
    save_local(m)
    cfg = config()
    for n, wt in running.items():
        print(f"--- {n} ({cfg['slots'][n]['label']})")
        serve(n, cfg["slots"][n], wt, main_checkout())
    print()
    if plugin_reconnects():
        print("rojo-flow's plugin looks the new ports up by itself; nothing to do in Studio")
        return
    print("Studio's Rojo plugin remembers the old port per place: enter the new port once in each of these places")
    for n in moves:
        print(f"  {cfg['slots'][n]['label']} (place {cfg['slots'][n]['placeId']}): localhost:{moves[n]}")
    if not a.quiet:
        notify("flow: connect Studio on the new ports", port_list(moves))


def process_table():
    """{pid: (parent pid, command line)} for every process, via CIM."""
    ps = ("Get-CimInstance Win32_Process | ForEach-Object { \"$($_.ProcessId)`t$($_.ParentProcessId)`t$($_.CommandLine)\" }")
    p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    table = {}
    for line in p.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = (int(parts[1]), parts[2])
    return table


def heal():
    """Adopt servers this project started on a port no slot has (their launcher writes this project's
    `rojo-<slot>.log`, here or in flow 1.1's folder): the slot's port moves to it, no restart, so Studio stays
    connected. A server on the slot's own port needs nothing: running is decided by the port."""
    cfg = config()
    known = {s["port"] for s in cfg["slots"].values()} | other_ports() | {main_port()}
    strays = {port: pid for port, pid in rojo_listeners().items() if port not in known}
    if not strays:
        return
    table = process_table()
    dirs = "|".join(re.escape(str(d) + os.sep) for d in (HERE, LEGACY_ROOT / project_key()))
    log_re = re.compile(rf"(?:{dirs})rojo-([^\\/\"]+)\.log", re.I)
    m, st, changed = local(), state(), False
    for port, pid in strays.items():
        cur, slot = pid, None
        for _ in range(5):
            if cur not in table:
                break
            parent, line = table[cur]
            hit = log_re.search(line)
            if hit:
                slot = hit.group(1)
                break
            cur = parent
        if not slot or slot not in cfg["slots"] or rojo_listeners().get(cfg["slots"][slot]["port"]):
            continue
        old = cfg["slots"][slot]["port"]
        m["ports"][slot] = port
        st[slot] = {"pid": pid, "port": port, "worktree": str(stage_path(slot)), "started": time.time()}
        changed = True
        print(f"WARNING: {slot}'s server runs on port {port}, not {old}: flow lost track of it. Adopted it: "
              f"{slot} now uses port {port} (connect its Studio to localhost:{port}).")
    if changed:
        save_local(m)
        save_state(st)
        reconfig()


# ---------------------------------------------------------------- leases: one session per slot

def leases():
    return load_json(LEASES, {})


def save_leases(l):
    write_json(LEASES, l)


@contextlib.contextmanager
def lease_lock():
    """One claim at a time: three sessions running `flow doctor` at once all claimed the same slot."""
    path = HERE / "leases.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    end = time.time() + 20
    while True:
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > 30:  # left by a flow that was killed
                    path.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.time() > end:
                die(f"the slot leases are locked by another flow ({path}); try again")
            time.sleep(0.1)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


STALE_HOURS = 2  # `flow release --stale` frees slots that saw no push for this long


def ago(t):
    s = time.time() - t
    return f"{s / 60:.0f} min ago" if s < 5400 else f"{s / 3600:.1f}h ago"


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
    with lease_lock():
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
                    "run `flow doctor` without a slot to get a free one.", 2)
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
                "Ask the user to open another scratch place and run `flow slot <name> <placeId>` (it serves it too), "
                "or end a finished session with `flow release` in its worktree. Run `flow status` to show them.", 2)
        slot = sorted(free)[0][2]
        touch_lease(slot, tree)
        return slot, True


def release(slot):
    l = leases()
    l.pop(slot, None)
    save_leases(l)


def cmd_release(a):
    slot = one_slot(a)
    if a.stale:
        user_only("release --stale")
        config()  # slot names for the output
        with lease_lock():
            l = leases()
            gone = [s for s, lease in l.items()
                    if not Path(lease["tree"]).exists() or time.time() - lease["at"] > a.older_than * 3600]
            for s in gone:
                l.pop(s)
            save_leases(l)
        print(f"released {', '.join(gone)}" if gone else f"no slot is idle for over {a.older_than:g}h")
        return
    with lease_lock():
        if slot:
            user_only("release <slot>")
            release(slot)
            print(f"released {slot}")
            return
        slot = lease_of(toplevel())
        if not slot:
            print("this worktree holds no slot")
            return
        release(slot)
    print(f"released {slot}; another session can claim it now")


def cmd_status(a):
    heal()
    cfg = config()
    ls = leases()
    if a.json:
        rows = []
        for slot in cfg["slots"]:
            sinfo, info, running, n = slot_report(cfg, slot)
            lease = ls.get(slot)
            live = bool(lease and lease_live(lease))
            rows.append({"slot": slot, "label": sinfo["label"], "placeId": sinfo["placeId"], "port": sinfo["port"],
                         "running": running, "studioConnected": bool(n),
                         "stage": info["worktree"] if running else None, "crashed": not running and crashed(slot),
                         "heldBy": Path(lease["tree"]).name if live else None,
                         "idleMinutes": round((time.time() - lease["at"]) / 60) if live else None})
        sys.stdout.write(json.dumps({"mainPort": main_port(), "default": cfg["default"], "slots": rows}) + "\n")
        return
    if not cfg["slots"]:
        print("no slots on this machine yet: flow slot <name> <placeId>")
    idle = []
    for slot in cfg["slots"]:
        sinfo, info, running, n = slot_report(cfg, slot)
        lease = ls.get(slot)
        held = ", free"
        if lease and lease_live(lease):
            held = f", held by {Path(lease['tree']).name} (last push {ago(lease['at'])})"
            if time.time() - lease["at"] > STALE_HOURS * 3600:
                idle.append(slot)
        head = f"{slot} {sinfo['label']} port {sinfo['port']} place {sinfo['placeId']}"
        if running:
            conn = "Studio connected" if n else "NO Studio connected"
            print(f"{head}: running, {conn}, tree {Path(info['worktree']).name}{held}")
        elif listening(sinfo["port"]):
            pid = snapshot()[0][sinfo["port"]]
            print(f"{head}: not running; the port is taken by {snapshot()[2].get(pid, '?')} (pid {pid}). "
                  "Move the slots with `flow ports`")
        elif crashed(slot):
            print(f"{head}: not running, Rojo crashed ({log_path(slot)}):\n{log_tail(slot)}")
        else:
            print(f"{head}: not running")
    print(f"main place port {main_port()} (slots must differ)")
    known = {s["port"] for s in cfg["slots"].values()} | other_ports() | {main_port()}
    for port, pid in sorted(rojo_listeners().items()):
        if port not in known:
            print(f"WARNING: rojo on port {port} (pid {pid}) belongs to no slot of any project: a stale server. "
                  f"Stop it with `flow down --port {port}`")
    print(f"state: {HERE}")
    clash = [slot for slot, s in cfg["slots"].items() if s["port"] == main_port()]
    if clash:
        print(f"WARNING: {', '.join(clash)} use the main place's port; renumber with `flow ports`")
    reserved = {int(p) for p in project().get("reservedPorts") or []}
    clash = [slot for slot, s in cfg["slots"].items() if s["port"] in reserved]
    if clash:
        print(f"WARNING: {', '.join(clash)} use a port in reservedPorts ({project_file().name}); renumber with `flow ports`")
    if idle:
        print(f"note: {', '.join(idle)} saw no push for over {STALE_HOURS}h; `flow release --stale` frees them")
    for p in plugin_problems():
        print(p)
    print("(connected = an open TCP connection to the port; heuristic)")


def list_slots():
    cfg = config()
    if not cfg["slots"]:
        print("no slots on this machine yet: flow slot <name> <placeId>")
    for n, s in cfg["slots"].items():
        print(f"{n} {s['label']} port {s['port']} place {s['placeId']}" + (" (default)" if n == cfg["default"] else ""))
    elsewhere = [n for n in (project().get("slots") or {}) if n not in cfg["slots"]]
    if elsewhere:
        print(f"(other devices only: {', '.join(elsewhere)})")
    print(f"(slots: {project_file().name}; ports: {LOCAL})")


def place_id(text):
    """A place id from a number or a pasted link (roblox.com/games/<id>/..., ...?placeId=<id>)."""
    if text is None:
        return None
    text = str(text).strip()
    if text.isdigit():
        return int(text)
    m = re.search(r"/games/(\d+)", text) or re.search(r"[?&]placeId=(\d+)", text, re.I)
    if not m:
        die(f"'{text}' is neither a place id nor a roblox.com/games/<id> link")
    return int(m.group(1))


def cmd_slot(a):
    """Add, update or remove a slot. Slots are saved in flow.json, so every device gets them; ports stay per
    machine. Adding one also puts its place in servePlaceIds and serves it."""
    if not a.name:
        return list_slots()
    user_only("slot")
    shared = dict(project().get("slots") or {})
    m = local()
    own = dict(m.get("slots") or {})
    if a.remove:
        if a.name not in shared and a.name not in own:
            die(f"no slot '{a.name}'")
        if a.name in config()["slots"] and stop_slot(a.name):
            print(f"stopped {a.name}'s server")
        if shared.pop(a.name, None) is not None:
            save_slots(shared)
        m = local()
        own.pop(a.name, None)
        m["slots"] = own
        m["ports"].pop(a.name, None)
        save_local(m)
        with lease_lock():
            release(a.name)
        print(f"removed slot {a.name}")
        return
    cur = own.get(a.name) or shared.get(a.name) or {}
    a.place = place_id(a.place)
    if a.place is None and not cur:
        die(f"give its place id: flow slot {a.name} <placeId>")
    before = config()["slots"].get(a.name)
    entry = dict(cur)
    if a.place:
        entry["placeId"] = a.place
    entry["label"] = a.label or cur.get("label") or a.name
    if a.here:
        entry["hosts"] = sorted(set(entry.get("hosts", [])) | {host()})
    machine_only = a.name in own
    if machine_only:
        own[a.name] = entry
    else:
        shared[a.name] = entry
        save_slots(shared)
    m = local()
    m["slots"] = own
    if a.port:
        others = {p for n, p in m["ports"].items() if n != a.name and n in config()["slots"]}
        if a.port in other_ports() | {main_port()} | others:
            die(f"port {a.port} is already taken (main place {main_port()}, other slots, reservedPorts or other projects)")
        m["ports"][a.name] = a.port
    save_local(m)
    cfg = config()
    if a.name not in cfg["slots"]:
        print(f"saved {a.name} in {project_file().name}; it's for {', '.join(entry.get('hosts', []))}, not this machine")
        return
    s = cfg["slots"][a.name]
    where = f"{LOCAL} (this machine only)" if machine_only else f"{project_file().name}: commit it so other devices get it"
    print(f"saved slot {a.name}: {s['label']}, place {s['placeId']}, port {s['port']} (in {where})")

    proj = main_checkout() / "default.project.json"
    if a.no_add:
        if s["placeId"] not in load_json(proj, {}).get("servePlaceIds", []):
            print(f"WARNING: place {s['placeId']} is not in default.project.json servePlaceIds; Studio will refuse to sync")
    elif add_place_ids(proj, [s["placeId"]]):
        print(f"added place {s['placeId']} to default.project.json servePlaceIds (commit it)")

    if a.no_up:
        print(f"next: flow up {a.name}, then connect that place to localhost:{s['port']} (once)")
        return
    if slot_report(cfg, a.name)[2] and before and (before["port"], before["placeId"]) == (s["port"], s["placeId"]):
        print(f"{a.name} is already served on port {s['port']}; nothing to restart")
        return
    check_origin(main_checkout())
    up_slots([a.name], quiet=a.quiet, open_studio=not a.no_open)


def cmd_register(a):
    """Reserve this project's main-place port (servePort) so no slot, here or in another project, shares it.
    Keeps the current servePort when nobody uses it, else moves it to the next free port."""
    user_only("register")
    path = main_checkout() / "default.project.json"
    cur = main_port()
    taken = other_ports() | {s["port"] for s in config()["slots"].values()}
    port = cur
    if cur in taken:
        port = max(taken | {cur, 37844}) + 1
        text = re.sub(r'("servePort"\s*:\s*)\d+', rf"\g<1>{port}", read_raw(path), count=1)
        write_raw(path, text)
        print(f"servePort {cur} is already used: changed default.project.json to {port}")
    cfg = reconfig()
    print(f"main place port {port}")
    if [s["port"] for s in cfg["slots"].values()] != list(range(port + 1, port + 1 + len(cfg["slots"]))):
        print(f"note: `flow ports` renumbers the slots from {port}")


def cmd_where(a):
    heal()
    cfg = config()
    tree = toplevel()
    slot = lease_of(tree)
    if not slot:
        die("this worktree holds no slot yet; `flow doctor` claims one", 2)
    sinfo = cfg["slots"][slot]
    _, info, running, n = slot_report(cfg, slot)
    sid, why = find_studio(sinfo["placeId"])
    if a.json:
        out = {"slot": slot, "label": sinfo["label"], "placeId": sinfo["placeId"], "port": sinfo["port"],
               "studioConnected": bool(n), "studioId": sid, "stage": info["worktree"] if running else None}
        sys.stdout.write(json.dumps(out) + "\n")
        return
    print(f"slot={slot} label={sinfo['label']} place={sinfo['placeId']} port={sinfo['port']} "
          f"studio={'connected' if n else 'NOT connected'} stage={info['worktree'] if running else '(not served)'}")
    if sid:
        print(f"studio_id={sid}: use it for every Studio call, never another one")
    else:
        print(f"no studio_id: {why}. Use the roblox-studio studio_id whose placeId is {sinfo['placeId']}, never another one")


def cmd_doctor(a):
    """Session preflight: claims this worktree's slot, then checks everything flow can check."""
    heal()
    tree = toplevel()
    if is_stage(tree) or same_path(tree, main_checkout()):
        die("run this in your session's own worktree, not the main checkout or a stage", 2)
    slot, new = claim(tree, one_slot(a))
    cfg = config()
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
    crash = " (it crashed: `flow status` shows the log)" if not running and crashed(slot) else ""
    line(running, f"rojo server on {slot} (port {sinfo['port']}){crash}", f"ask the user to run `flow up {slot}`")
    line(running and is_stage(info["worktree"]), f"{slot} serves its stage (the flow push target)",
         f"ask the user to run `flow up {slot}` (no --tree)")
    line(running and n > 0, f"Studio ({sinfo['label']}) connected to it",
         f"ask the user to open {sinfo['label']} in Studio (`flow open {slot}`): it syncs by itself; `flow wait` in "
         "the background" if plugin_reconnects() else
         f"ask the user to press Connect in {sinfo['label']} (port {sinfo['port']}); `flow wait` in the background")
    for tool in ("stylua", "rojo", "rojo-flow", "git", "blink", "rokit", "wally", "luau-lsp"):
        line(shutil.which(tool) is not None, f"{tool} on PATH", "run `rokit install` in the repo")
    good, msg = setup(tree)
    line(good, msg, "fix the install error above, then doctor again")
    sid, why = find_studio(sinfo["placeId"])
    line(bool(sid), f"Studio has {sinfo['label']} open (studio_id {sid})" if sid else f"Studio has {sinfo['label']} "
         f"open: {why}", f"ask the user to open {sinfo['label']} in Studio (`flow open {slot}`)")
    for p in plugin_problems():
        print(p)
    if sid:
        print(f"use studio_id {sid} for every Studio call this session, never another Studio. If your roblox-studio "
              "MCP doesn't list it, tell the user now.")
    print("when the session's work is done: `flow release`")
    if not ok:
        sys.exit(1)


def cmd_wait(a):
    heal()
    cfg = config()
    slot, sinfo = pick_slot(cfg, a.slot or lease_of(toplevel()))
    end = time.time() + a.timeout
    n = 0
    with busy(f"Waiting for Studio ({sinfo['label']}) to connect on port {sinfo['port']}"):
        while time.time() < end:
            _, _, running, n = slot_report(cfg, slot)
            if not running:
                die(f"{slot} server is not running. Ask the user to run: flow up {slot}", 2)
            if n:
                break
            time.sleep(2)
    if not n:
        die(f"timed out after {a.timeout}s waiting for Studio on port {sinfo['port']}", 3)
    print(f"Studio connected on port {sinfo['port']}")
    dst = served_tree(slot)
    path = dst / stamp_path() if dst else None
    if not path or not path.exists():
        return
    value = path.read_text(encoding="utf-8").strip()
    status, sid, detail = stamp_check(sinfo["placeId"], value)
    if status == "synced":
        print(f"Studio synced: {sinfo['label']} has the last stamp {value} ({detail}). studio_id {sid}")
    elif status == "stale":
        die(f"Studio ({sinfo['label']}) has stamp {detail!r}, not the last push's {value}: Rojo isn't syncing. "
            "Test nothing; `flow verify` checks again.", 5)
    else:
        print(f"note: flow couldn't read the stamp from Studio ({detail}); check {value} yourself")


def cmd_log(a):
    """Show a slot's Rojo log (this worktree's slot, else the default one); -f keeps following it."""
    cfg = config()
    slot, _ = pick_slot(cfg, a.slot or lease_of(toplevel()))
    path = log_path(slot)
    if not path.exists():
        die(f"{slot} has no Rojo log yet ({path})")
    clean = lambda t: re.sub(r"\x1b\[[0-9;]*m", "", t)  # noqa: E731
    with open(path, encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()
        sys.stdout.write("\n".join(clean(l) for l in lines[-a.lines:]) + ("\n" if lines else ""))
        if not a.follow:
            return
        sys.stderr.write(f"(following {path}; Ctrl+C stops)\n")
        try:
            while True:
                chunk = fh.read()
                if chunk:
                    sys.stdout.write(clean(chunk))
                    sys.stdout.flush()
                else:
                    time.sleep(0.5)
        except KeyboardInterrupt:
            pass


def cmd_port(a):
    """Print a slot's port (or the main place's with `main`), for scripts."""
    if a.slot == "main":
        sys.stdout.write(f"{main_port()}\n")
        return
    cfg = config()
    slot, sinfo = pick_slot(cfg, a.slot or lease_of(toplevel()))
    sys.stdout.write(f"{sinfo['port']}\n")


UNIVERSES = Path.home() / ".flow" / "universes.json"


def universe_of(place):
    """The universe (experience) id of a place, which Studio needs to open it. Asked from Roblox once, cached."""
    cache = load_json(UNIVERSES, {})
    if str(place) in cache:
        return cache[str(place)]
    req = urllib.request.Request(f"https://apis.roblox.com/universes/v1/places/{place}/universe",
                                 headers={"User-Agent": "flow"})
    with urllib.request.urlopen(req, timeout=10) as r:
        uid = json.load(r).get("universeId")
    if uid:
        cache[str(place)] = uid
        write_json(UNIVERSES, cache)
    return uid


def cmd_open(a):
    """Open the slots' places in Studio (a new device: every scratch place in one go). Already open ones are
    skipped."""
    user_only("open")
    cfg = config()
    names = slot_args(a)
    if a.all:
        targets = list(cfg["slots"])
    else:
        check_slots(cfg, names)
        targets = names or [pick_slot(cfg, None)[0]]
    open_places(cfg, targets, force=a.force)


def studio_handler():
    """The exe Windows runs for roblox-studio: links (Studio's launcher), or None."""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"roblox-studio\shell\open\command") as k:
            cmd = winreg.QueryValueEx(k, "")[0]
    except OSError:
        return None
    m = re.match(r'\s*"([^"]+)"|\s*(\S+)', cmd)
    exe = m and (m.group(1) or m.group(2))
    return exe if exe and Path(exe).exists() else None


def launch_studio(uri):
    """Open a roblox-studio: link with Studio's launcher. Under rokit, flow runs in a job that kills everything in
    it when flow exits, Studio included (it started and died at once), so there Studio is created outside it."""
    exe = studio_handler()
    if exe and in_job():
        try:
            create_outside_job(subprocess.list2cmdline([exe, uri]), Path(exe).parent, "Studio", show=True)
            return True
        except SystemExit:  # die() already said why
            return False
    try:
        if not exe:
            os.startfile(uri)
            return True
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("_PYI", "_MEI"))}
        subprocess.Popen([exe, uri], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True,
                         creationflags=0x00000008 | 0x00000200)  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        return True
    except OSError as e:
        print(f"WARNING: couldn't start Studio ({exe or uri}): {e}")
        return False


def open_places(cfg, targets, force=False):
    """Open the slots' places in Studio, skipping ones a Studio already has. Returns the slots it opened."""
    try:
        with StudioMCP() as mcp:
            already = {int(s.get("placeId") or 0) for s in mcp.studios()}
    except (StudioError, OSError, ValueError):
        already = set()
    opened = []
    for slot in targets:
        s = cfg["slots"][slot]
        if s["placeId"] in already and not force:
            print(f"{slot} {s['label']}: already open in Studio (--force opens it again)")
            continue
        try:
            uid = universe_of(s["placeId"])
        except Exception as e:
            print(f"WARNING: couldn't look up {slot}'s experience (place {s['placeId']}): {e}")
            continue
        if not uid:
            print(f"WARNING: Roblox knows no experience for {slot}'s place {s['placeId']}: wrong place id?")
            continue
        if opened:
            time.sleep(2)  # one Studio launch at a time
        if not launch_studio(f"roblox-studio:1+launchmode:edit+task:EditPlace+placeId:{s['placeId']}+universeId:{uid}"):
            continue
        opened.append(slot)
        running = slot_report(cfg, slot)[2]
        if not running:
            after = f"; `flow up {slot}` serves it"
        elif plugin_reconnects():
            after = "; it syncs by itself once loaded"
        else:
            after = f"; then connect Rojo to localhost:{s['port']}"
        print(f"opening {slot} {s['label']} (place {s['placeId']}) in Studio{after}")
    return opened


PIN_LINE = re.compile(r'^(?P<pre>[ \t]*flow[ \t]*=[ \t]*")(?P<repo>[\w.-]+/flow)@(?P<ver>[^"]+)"', re.M)


def latest_release(repo, timeout=10):
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases/latest",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "flow"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)["tag_name"].lstrip("v")


def cmd_pin(a):
    """Pin the project's rokit.toml to the latest flow release (or the given version), then rokit install."""
    user_only("pin")
    main = main_checkout()
    path = main / "rokit.toml"
    text = read_raw(path) if path.exists() else "[tools]\n"
    m = PIN_LINE.search(text)
    repo = m.group("repo") if m else "riesegarder/flow"
    if a.version:
        ver = a.version.lstrip("v")
    else:
        try:
            ver = latest_release(repo)
        except Exception as e:
            die(f"couldn't ask GitHub for the latest {repo} release ({e}); give the version: flow pin 1.2.0")
    if m and m.group("ver") == ver:
        print(f"rokit.toml already pins flow {ver}")
        return
    nl = newline_of(text)
    if m:
        text = text[:m.start("ver")] + ver + text[m.end("ver"):]
    else:
        tools = re.search(r"^\[tools\][^\r\n]*\r?\n", text, re.M)
        line = f'flow = "{repo}@{ver}"{nl}'
        text = text[:tools.end()] + line + text[tools.end():] if tools else text.rstrip() + f"{nl}{nl}[tools]{nl}{line}"
    write_raw(path, text)
    print(f"pinned flow {m.group('ver') + ' -> ' if m else ''}{ver} in rokit.toml (commit it)")
    r = run(["rokit", "install"], cwd=main)
    print("rokit install: done" if r.returncode == 0 else f"WARNING: rokit install failed: {(r.stdout + r.stderr).strip()[-300:]}")


UPDATE_CACHE = Path.home() / ".flow" / "update.json"


def update_notice():
    """Once a day, ask GitHub for the latest flow release; say so when it's newer than this one."""
    if is_agent() or os.environ.get("FLOW_NO_UPDATE_CHECK"):
        return
    cache = load_json(UPDATE_CACHE, {})
    if time.time() - cache.get("checked", 0) > 86400:
        try:
            cache = {"checked": time.time(), "latest": latest_release("riesegarder/flow", timeout=3)}
        except Exception:
            cache = {"checked": time.time(), "latest": cache.get("latest")}
        try:
            write_json(UPDATE_CACHE, cache)
        except OSError:
            pass
    latest = cache.get("latest")
    if latest and semver(latest) and semver(latest) > semver(VERSION):
        sys.stderr.write(flowui.fmt(f"note: flow {latest} is out (this is {VERSION}): `flow pin` updates this "
                                    "project's rokit.toml") + "\n")


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


# ---------------------------------------------------------------- stamp + push

def stamp_path():
    return project()["stamp"]["file"]


def stamp_lookup():
    parts = project()["stamp"]["instance"].split(".")
    steps = "".join(f' v = v and v:FindFirstChild("{part}")' for part in parts[1:])
    return f'local v = game:GetService("{parts[0]}"){steps}'


def stamp_luau():
    return f"{stamp_lookup()} return v and v.Value"


def served_tree(slot):
    cfg = config()
    slot, _ = pick_slot(cfg, slot)
    _, info, running, _ = slot_report(cfg, slot)
    return Path(info["worktree"]) if running else None


def tree_files(tree):
    """Tracked + untracked (not ignored) files that exist in tree's working copy, minus flow's own."""
    p = run(["git", "ls-files", "-z", "-co", "--exclude-standard"], cwd=tree)
    skip = lambda f: f.startswith(".claude/") or f == stamp_path()  # noqa: E731
    return {f for f in p.stdout.split("\0") if f and not skip(f) and (tree / f).is_file()}


def parents(path):
    parts = path.split("/")[:-1]
    return {"/".join(parts[:i]) for i in range(1, len(parts) + 1)}


def mirror(src, dst, place_ids=()):
    """Make dst's files equal src's working tree (HEAD plus uncommitted edits), changing only what differs.

    Rojo 7.7.1 panics (change_processor.rs:179 NotFound) when a folder disappears while it still handles the
    removal of the files in it, which `git checkout --force` + `git clean` did whenever the next session's branch
    lacked folders the last one had. So: delete the files, give Rojo a second, then remove the folders nothing
    needs any more one depth level at a time (deepest first), and write changed files in place (never temp file +
    rename). The stage's default.project.json keeps every slot's place in servePlaceIds."""
    head = run(["git", "rev-parse", "HEAD"], cwd=src, check=True).stdout.strip()
    want, have = tree_files(src), tree_files(dst)
    gone = sorted(have - want)
    for f in gone:
        (dst / f).unlink(missing_ok=True)
    needed = set().union(*(parents(f) for f in want)) if want else set()
    dirs = (set().union(*(parents(f) for f in gone)) - needed) if gone else set()
    for depth in sorted({d.count("/") for d in dirs}, reverse=True):
        time.sleep(1.0 if depth == max(d.count("/") for d in dirs) else 0.5)
        for d in (d for d in dirs if d.count("/") == depth):
            try:
                (dst / d).rmdir()  # only when empty: ignored files (packages, sourcemap) stay
            except OSError:
                pass
    changed, project_changed = 0, False
    for f in sorted(want):
        data = (src / f).read_bytes()
        if f == "default.project.json" and place_ids:
            try:
                text = data.decode("utf-8-sig")
                data = add_place_ids_text(text, place_ids)[0].encode("utf-8")
            except ValueError:
                pass
        t = dst / f
        if t.is_file():
            old = t.read_bytes()
            if old == data:
                continue
            if f == "default.project.json":
                project_changed = project_differs(old, data)
            with open(t, "r+b") as fh:
                fh.write(data)
                fh.truncate()
        else:
            t.parent.mkdir(parents=True, exist_ok=True)
            t.write_bytes(data)
        changed += 1
    run(["git", "reset", "-q", head], cwd=dst)  # HEAD and index follow; the files are already right
    return head, changed, len(gone), project_changed


def project_differs(old, new):
    """True when two default.project.json versions differ in more than formatting, servePort and servePlaceIds
    (flow serves on the slot's port and keeps every slot's place in the list itself)."""
    try:
        a, b = (json.loads(x.decode("utf-8-sig")) for x in (old, new))
    except ValueError:
        return True
    for d in (a, b):
        if isinstance(d, dict):
            d.pop("servePort", None)
            d.pop("servePlaceIds", None)
    return a != b


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


def restart_slot(slot, sinfo, dst, main, was_connected, why):
    """Serve the stage again on the same port. With the reconnecting plugin, wait until Studio is back; else
    tell the user to press Connect. Returns True when Studio is connected again."""
    print(why)
    print(f"restarting {slot}'s Rojo on port {sinfo['port']}")
    serve(slot, sinfo, dst, main)
    if not was_connected:
        return False
    if plugin_reconnects():
        return not await_reconnect([slot])
    notify("flow: reconnect Studio", f"{slot} [{sinfo['port']}]: Rojo restarted by a push")
    print(f"Ask the user to press Connect in {sinfo['label']} (port {sinfo['port']}); run `flow wait` in the "
          "background: it checks the stamp once Studio is back.")
    return False


def cmd_push(a):
    heal()
    src = toplevel()
    main = main_checkout()
    if not same_path(src, main) and not is_stage(src):
        synced, msg = sync_with_main(src)
        if not synced:
            die(f"{msg}. Studio would run old code: resolve the rebase first "
                "(`git rebase main`, fix conflicts, `git rebase --continue`), then push again.", 2)
        if "rebased" in msg:
            print(msg)
    slot, new = claim(src, one_slot(a))
    cfg = config()
    sinfo = cfg["slots"][slot]
    if new:
        print(f"claimed {slot} ({sinfo['label']}, place {sinfo['placeId']})")
    dst = served_tree(slot)
    if not dst:
        crash = f" (its Rojo crashed:\n{log_tail(slot)}\n)" if crashed(slot) else ""
        die(f"nothing is served on {slot}{crash}. Ask the user to run `flow up {slot}` in their terminal.", 2)
    touched, restart = 0, None
    was_connected = studio_connected(sinfo["port"])
    if same_path(src, dst):
        print("this tree is the served one; just stamping")
    elif not is_stage(dst):
        die(f"{slot} serves {dst.name}, a real worktree, not a stage: pushing would overwrite its files. "
            f"Ask the user to run `flow up {slot}` (no --tree) so its stage is served.", 2)
    else:
        with busy(f"Syncing {src.name} onto the {dst.name} stage"):
            head, changed, deleted, project_changed = mirror(src, dst, slot_place_ids())
        touched = changed + deleted
        print(f"pushed {src.name} @ {head[:7]} to {dst.name} ({changed} file(s) changed, {deleted} deleted)")
        # a restart costs nothing when nobody is connected or the plugin reconnects by itself (riesegarder/rojo)
        restart_ok = not was_connected or plugin_reconnects()
        # never reinstall under a running rojo: wally recreates the package folders and rojo stops syncing them
        if not packages_current(dst):
            if not restart_ok:
                die(f"the stage's packages don't match this push's wally.toml/wally.lock (or are still links). "
                    f"Ask the user to run `flow up {slot}` (installs, then restarts rojo), then push again.", 2)
            restart = "the packages changed (wally.toml/wally.lock): reinstalling them on the stage"
        elif project_changed:
            if restart_ok:
                restart = ("default.project.json changed: Rojo doesn't apply every project change (Workspace or "
                           "Lighting properties) to a connected place")
            else:
                print(f"WARNING: default.project.json changed. Rojo doesn't apply every project change (Workspace or "
                      f"Lighting properties) to a connected place: if Studio misses one, ask the user to run "
                      f"`flow restart {slot}` and reconnect.")
    regen_blink(dst)
    if touched:
        time.sleep(1.0)  # a panic comes while rojo processes the change events
        forget_snapshot()
        if not rojo_listeners().get(sinfo["port"]):
            tail = log_tail(slot)
            restart = f"WARNING: {slot}'s Rojo stopped during the push" + (f":\n{tail}" if tail else "")
    if restart:
        restart_slot(slot, sinfo, dst, main, was_connected, restart)
    value = write_stamp(dst, src)
    report_stamp(slot, sinfo, value)


def cmd_stamp(a):
    a.slot = a.slot_pos = None
    return cmd_push(a)


def report_stamp(slot, sinfo, value, timeout=20):
    """Check Studio has this stamp: flow reads it back through the Studio MCP. Exits 5 when Studio has another
    one (Rojo isn't syncing). When flow can't read it, prints how to check by hand."""
    hints = [h.replace("{stamp}", value) for h in
             project()["stamp"].get("hint", ["2. Only then test: results from an unsynced place don't count."])]
    manual = (f"1. Edit datamodel, must return exactly the stamp above (else Rojo isn't synced: stop, wait for "
              f"reconnect): {stamp_luau()}")
    if not studio_connected(sinfo["port"]):
        ask = (f"ask the user to open {sinfo['label']} in Studio (`flow open {slot}`; it syncs by itself)"
               if plugin_reconnects() else f"ask the user to press Connect in {sinfo['label']} (port {sinfo['port']})")
        print(f"Studio isn't connected to {slot}: {ask} and run `flow wait` in the background; it checks the stamp "
              "once Studio is back.")
        print("\n".join([manual] + hints))
        return
    status, sid, detail = stamp_check(sinfo["placeId"], value, timeout)
    if status == "synced":
        print(f"Studio synced: {sinfo['label']} has this stamp ({detail}). studio_id {sid}: use it for every Studio "
              "call, never another one")
        print("\n".join(hints))
    elif status == "stale":
        die(f"Studio ({sinfo['label']}, studio_id {sid}) still has stamp {detail!r} after {timeout}s, not {value}: "
            f"Rojo isn't syncing it. Test nothing. If Studio shows Rojo disconnected, ask the user to press Connect "
            f"(port {sinfo['port']}), then run `flow verify`.", 5)
    else:
        print(f"note: flow couldn't read the stamp from Studio ({detail}). Pick the studio_id whose placeId is "
              f"{sinfo['placeId']} and check it yourself:")
        print("\n".join([manual] + hints))


def cmd_verify(a):
    """Check Studio has the stamp the last push wrote (after a reconnect, say)."""
    heal()
    cfg = config()
    slot, sinfo = pick_slot(cfg, one_slot(a) or lease_of(toplevel()))
    dst = served_tree(slot)
    if not dst:
        die(f"nothing is served on {slot}. Ask the user to run `flow up {slot}`.", 2)
    path = dst / stamp_path()
    if not path.exists():
        die(f"{slot}'s stage has no stamp yet: `flow push` writes one", 2)
    value = path.read_text(encoding="utf-8").strip()
    print(value)
    if not studio_connected(sinfo["port"]):
        die(f"Studio isn't connected to {slot} (port {sinfo['port']}). Ask the user to press Connect in "
            f"{sinfo['label']}; `flow wait` in the background.", 2)
    status, sid, detail = stamp_check(sinfo["placeId"], value, a.timeout)
    if status == "synced":
        print(f"Studio synced: {sinfo['label']} has this stamp ({detail}). studio_id {sid}")
    elif status == "stale":
        die(f"Studio ({sinfo['label']}) has stamp {detail!r}, not {value}: Rojo isn't syncing. Test nothing.", 5)
    else:
        die(f"flow couldn't read the stamp from Studio: {detail}", 6)


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
    return value


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
    Line rule: {id, severity, pattern, message, paths?, except?, unless?, unlessPrev?}; with multiline: true the
    pattern runs over the whole file and a match counts when an added line is within 2 lines of it.
    File rule: {id, severity, mustMatch, message, paths?, except?}.
    Returns compiled rules; a broken rule is itself an error finding."""
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
                "unlessPrev": re.compile(r["unlessPrev"]) if "unlessPrev" in r else None,
                "multiline": bool(r.get("multiline")),
                "must": re.compile(r["mustMatch"], re.M) if "mustMatch" in r else None,
            })
        except Exception as e:
            finding("ERROR", src, 0, "rules", f"rule '{rid}' is invalid: {e}")
    return rules


def apply_rules(rules, path, text, added):
    for r in rules:
        if not any(g.match(path) for g in r["paths"]) or any(g.match(path) for g in r["except"]):
            continue
        if r["pattern"] and r["multiline"]:
            for m in r["pattern"].finditer(text):
                first = text[:m.start()].count("\n") + 1
                last = text[:m.end()].count("\n") + 1
                if any(first - 2 <= k <= last + 2 for k in added):
                    finding(r["sev"], path, first, r["id"], r["msg"])
        elif r["pattern"]:
            lines = text.splitlines()
            for ln, line in added.items():
                if is_comment(line) or not r["pattern"].search(line):
                    continue
                if r["unless"] and r["unless"].search(line):
                    continue
                prev = lines[ln - 2] if 2 <= ln <= len(lines) + 1 else ""
                if r["unlessPrev"] and r["unlessPrev"].search(prev):
                    continue
                finding(r["sev"], path, ln, r["id"], r["msg"])
        if r["must"] and added and not r["must"].search(text):
            finding(r["sev"], path, 0, r["id"], r["msg"])


FINDING_LINE = re.compile(r"^(ERROR|WARN)\s+(\S*?)(?::(\d+))?\s+\[([^\]]+)\]\s+(.*)$")


def run_checks(root, base, added_by_file):
    """The project's own check scripts (flow.json "checks": [{id, run: [cmd, ...]}]). Each gets a JSON file
    path as its last argument: {base, files: {path: [added line numbers]}}, and prints findings as
    `ERROR path:line [rule] message` or `WARN ...`. Other output is ignored."""
    checks = project().get("checks", [])
    if not checks:
        return
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump({"base": base, "files": {k: sorted(v) for k, v in added_by_file.items()}}, f)
        tmp = f.name
    try:
        for c in checks:
            cid = c.get("id", "check")
            cmd = list(c.get("run", []))
            exe = shutil.which(cmd[0]) if cmd else None
            if not exe:
                finding("ERROR", "", 0, cid, f"check '{cid}' can't run: {cmd[0] if cmd else 'no run'} not found")
                continue
            p = run([exe] + cmd[1:] + [tmp], cwd=root, label=f"Running the {cid} check")
            found = 0
            for line in (p.stdout or "").splitlines():
                m = FINDING_LINE.match(re.sub(r"\x1b\[[0-9;]*m", "", line).strip())
                if m:
                    finding(m.group(1), m.group(2), int(m.group(3) or 0), m.group(4), m.group(5))
                    found += 1
            if p.returncode not in (0, 1) or (p.returncode == 1 and not found):
                finding("ERROR", "", 0, cid, f"check '{cid}' failed: {(p.stdout + p.stderr).strip()[-300:]}")
    finally:
        os.unlink(tmp)


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


def ignored(path):
    """flow.json "ignore": files the built-in checks and the type check skip (Lune scripts aren't Roblox code)."""
    return any(glob_re(g).match(path) for g in project().get("ignore", []))


def scan_file(root, base, path, text, added, rules=()):
    lines = text.splitlines()
    sev = {k: builtin(k) for k in ("output", "unused", "const", "server-only")}
    logger = re.search(r"(?:const|local)\s+print\b[^\n]*=\s*prefixedlogger\(", text)
    names_re = re.compile(r"^\s*(?:local|const)\s+(?:function\s+)?([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\s*[=(:]")

    apply_rules(rules, path, text, added)
    if ignored(path):
        return

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
    # Blink output is regenerated from network.blink (as up and push do), so it only differs from the base when
    # the schema changed or the committed copy was stale: never an edit to flag
    blink = set()
    if (root / "network.blink").exists():
        regen_blink(root)
        blink = {os.path.relpath(o, root).replace("\\", "/") for o in blink_outputs(root)}
    files = a.files or changed_files(root, base)
    files = [f.replace("\\", "/") for f in files]
    if not files:
        print("nothing changed against", base[:10])
        return
    luau = [f for f in files if f.endswith(".luau") and (root / f).exists() and f not in blink]

    for f in files:
        if f in blink:
            continue
        if any(f.startswith(g) or f.endswith(g) for g in project()["generated"]):
            finding("ERROR", f, 0, "generated", "generated file touched; leave it alone")

    rules = load_rules(root)
    added_by_file = {}
    for f in luau:
        text = (root / f).read_text(encoding="utf-8", errors="replace")
        added_by_file[f] = added_lines(root, base, f)
        scan_file(root, base, f, text, added_by_file[f], rules)

    run_checks(root, base, added_by_file)

    types = "type check skipped: no luau files changed"
    if luau:
        ok, msg = setup(root)
        typed = [f for f in luau if not ignored(f)]
        if not typed:
            types = "type check skipped: only ignored files changed"
        else:
            types = type_check(root, typed, added_by_file) if ok else f"type check skipped: {msg}"

    style = []
    if luau:
        # files named on the command line skip .styluaignore unless told otherwise
        p = run(["stylua", "--check", "--respect-ignores"] + luau, cwd=root)
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
    print(f"checked {len(files)} files ({len(luau)} luau, {len(rules)} project rules, {len(project().get('checks', []))} project checks) against {base[:10]}: {errs} errors, {warns} warnings")
    print(types)
    print("not covered: behavior, feel, VFX")
    if errs:
        sys.exit(1)


def cmd_project(a):
    """A project command from flow.json "commands": {name: {run: [cmd, ...], help?, defaultArgs?}}.
    defaultArgs are added when no positional argument is given."""
    spec = project()["commands"][a.cmd]
    args = list(a.args)
    if not any(not x.startswith("-") for x in args):
        args = list(spec.get("defaultArgs", [])) + args
    cmd = list(spec["run"])
    exe = shutil.which(cmd[0]) or die(f"{cmd[0]} not found on PATH (rokit install)")
    sys.exit(subprocess.call([exe] + cmd[1:] + args, cwd=main_checkout() if spec.get("inMain") else toplevel()))


def project_commands():
    try:
        return project().get("commands", {}) if run(["git", "rev-parse", "--git-dir"]).returncode == 0 else {}
    except SystemExit:
        return {}


# commands that run or look at Rojo servers (Windows only for now) and need the repo's slot state
SERVER_CMDS = {"up", "down", "restart", "update", "status", "doctor", "where", "wait", "push", "stamp", "release",
               "ports", "slot", "register", "prune", "verify", "open", "log", "port"}
# commands after which this machine's old slots are saved in flow.json (never from an agent)
ADOPT_CMDS = {"up", "slot", "status", "ports", "register", "restart", "update", "down", "open"}


def main():
    ap = argparse.ArgumentParser(prog="flow", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"flow {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def slot_option(s, text="this slot"):
        s.add_argument("slot_pos", nargs="?", metavar="slot", help=text)
        s.add_argument("--slot", help=argparse.SUPPRESS)  # flow <= 1.1 spelling

    s = sub.add_parser("up", help="serve slots' stages (the default slot when none is given)")
    s.add_argument("slots", nargs="*", help="slot names")
    s.add_argument("--slot", help=argparse.SUPPRESS)
    s.add_argument("--tree", help="serve this worktree (.claude/worktrees/<name>) instead of the stage; push then refuses")
    s.add_argument("--base", default="main")
    s.add_argument("--all", action="store_true", help="serve every slot's stage")
    s.add_argument("--no-open", action="store_true", help="don't open places Studio doesn't have open yet")
    s.add_argument("--quiet", action="store_true", help="no toast notification")
    s.set_defaults(fn=cmd_up)
    s = sub.add_parser("slot", help="list slots, or add/update one (saved in flow.json) and serve it")
    s.add_argument("name", nargs="?")
    s.add_argument("place", nargs="?", help="place id, or a roblox.com/games/<id> link")
    s.add_argument("--label")
    s.add_argument("--port", type=int, help="this machine's port for the slot")
    s.add_argument("--here", action="store_true", help="only this machine uses the slot (adds its name to hosts)")
    s.add_argument("--remove", action="store_true", help="remove the slot (stops its server)")
    s.add_argument("--no-up", action="store_true", help="don't serve it now")
    s.add_argument("--no-add", action="store_true", help="don't add the place to default.project.json servePlaceIds")
    s.add_argument("--no-open", action="store_true", help="don't open the place in Studio after serving it")
    s.add_argument("--quiet", action="store_true", help="no toast notification")
    s.set_defaults(fn=cmd_slot)
    s = sub.add_parser("ports", help="renumber the slot ports from the main place port (moves running servers)")
    s.add_argument("--dry-run", action="store_true", help="only show the new ports")
    s.add_argument("--quiet", action="store_true", help="no toast notification")
    s.set_defaults(fn=cmd_ports)
    s = sub.add_parser("register", help="reserve this project's main-place port so slots never share it")
    s.set_defaults(fn=cmd_register)
    s = sub.add_parser("release", help="free this worktree's Studio slot for other sessions")
    slot_option(s, "free this slot instead (user only)")
    s.add_argument("--stale", action="store_true", help="free every slot that saw no push for a while (user only)")
    s.add_argument("--older-than", type=float, default=STALE_HOURS, help=f"hours, for --stale (default {STALE_HOURS})")
    s.set_defaults(fn=cmd_release)
    s = sub.add_parser("open", help="open the slots' places in Studio (the default slot when none is given)")
    s.add_argument("slots", nargs="*", help="slot names")
    s.add_argument("--all", action="store_true", help="every slot's place")
    s.add_argument("--force", action="store_true", help="open it even when a Studio already has it")
    s.set_defaults(fn=cmd_open)
    s = sub.add_parser("pin", help="pin this project's rokit.toml to the latest flow release (or VERSION)")
    s.add_argument("version", nargs="?")
    s.set_defaults(fn=cmd_pin)
    s = sub.add_parser("log", help="a slot's Rojo log (this worktree's slot, else the default)")
    s.add_argument("slot", nargs="?")
    s.add_argument("-n", "--lines", type=int, default=40)
    s.add_argument("-f", "--follow", action="store_true", help="keep printing new lines")
    s.set_defaults(fn=cmd_log)
    s = sub.add_parser("port", help="print a slot's port (`main`: the main place's), for scripts")
    s.add_argument("slot", nargs="?")
    s.set_defaults(fn=cmd_port)
    s = sub.add_parser("down", help="stop slots' servers, or every server with `down all`")
    s.add_argument("slots", nargs="*", help="slot names, or all")
    s.add_argument("--slot", help=argparse.SUPPRESS)
    s.add_argument("--all", action="store_true", help="stop every slot's server")
    s.add_argument("--port", type=int, help="stop whatever rojo listens on this port (a stale server)")
    s.set_defaults(fn=cmd_down)

    for name, pull, text in (("restart", False, "restart the running servers (or the given slots, or all with --all)"),
                             ("update", True, "pull origin/main into local main, then restart the running servers")):
        s = sub.add_parser(name, help=text)
        s.add_argument("slots", nargs="*", help="slot names")
        s.add_argument("--all", action="store_true", help="restart every slot, not only the running ones")
        s.add_argument("--quiet", action="store_true", help="no toast notification")
        s.set_defaults(pull=pull, fn=cmd_restart)
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true", help="machine-readable")
    s.set_defaults(fn=cmd_status)
    s = sub.add_parser("prune", help="list (default) or remove stale worktrees; the served one is always kept")
    s.add_argument("--yes", action="store_true", help="actually remove (user only)")
    s.add_argument("--dirty", action="store_true", help="also remove worktrees with uncommitted changes (loses them)")
    s.add_argument("--branches", action="store_true", help="also delete branches whose commits are all in main")
    s.add_argument("--keep", nargs="*", help="worktree folder or branch names to keep")
    s.add_argument("--older-than", type=float, default=3, help="keep worktrees active within this many hours (default 3)")
    s.set_defaults(fn=cmd_prune)
    s = sub.add_parser("doctor", help="session preflight: claim a free Studio slot, check server, Studio, tools")
    slot_option(s, "claim this slot (only if the user said so)")
    s.set_defaults(fn=cmd_doctor)
    s = sub.add_parser("where", help="this worktree's slot, Studio place, studio_id and stage")
    s.add_argument("--json", action="store_true", help="machine-readable")
    s.set_defaults(fn=cmd_where)
    s = sub.add_parser("verify", help="check Studio has the stamp the last push wrote")
    slot_option(s, "this slot instead of this worktree's")
    s.add_argument("--timeout", type=int, default=20)
    s.set_defaults(fn=cmd_verify)
    s = sub.add_parser("wait")
    s.add_argument("slot", nargs="?")
    s.add_argument("--timeout", type=int, default=600)
    s.set_defaults(fn=cmd_wait)
    s = sub.add_parser("push", help="mirror this worktree (HEAD + uncommitted edits) onto the served stage, then stamp")
    slot_option(s, "claim this slot first (only if the user said so)")
    s.set_defaults(fn=cmd_push)
    s = sub.add_parser("stamp", help="write a fresh FlowStamp so Studio's synced copy can be verified")
    s.set_defaults(fn=cmd_stamp)
    s = sub.add_parser("check")
    s.add_argument("files", nargs="*")
    s.add_argument("--base")
    s.set_defaults(fn=cmd_check)
    for name, spec in project_commands().items():
        if name in sub.choices:
            continue
        s = sub.add_parser(name, help=spec.get("help", "project command (flow.json)"))
        s.add_argument("args", nargs=argparse.REMAINDER)
        s.set_defaults(fn=cmd_project)
    a = ap.parse_args()
    if getattr(a, "json", False):  # stdout carries only the JSON
        globals()["print"] = lambda *x, **k: flowui.builtins.print(*x, file=sys.stderr, **k)
    if a.cmd == "pin" and not _COMMON:
        die("not inside a git repo")
    if a.cmd in SERVER_CMDS:
        if not WINDOWS:
            die(f"`flow {a.cmd}` runs Rojo servers and is Windows only for now; `flow check` works everywhere")
        if not _COMMON:
            die("not inside a git repo")
        migrate()
        if a.cmd in ADOPT_CMDS:
            adopt_slots()
    a.fn(a)
    if a.cmd not in ("port", "pin") and not getattr(a, "json", False):
        update_notice()


if __name__ == "__main__":
    main()
