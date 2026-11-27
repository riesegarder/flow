"""Terminal styling for flow: colors, a braille spinner with a description per step, decorated output.

Same look as the tasks/dfiles tools. Plain text when output is piped, NO_COLOR is set or FLOW_NO_SPINNER=1
(FLOW_FORCE_COLOR=1 / FLOW_FORCE_SPINNER=1 force them on).
"""
import builtins
import contextlib
import os
import re
import shutil
import sys
import threading
import time

_VT = False


def _vt_once():
    global _VT
    if not _VT:
        _VT = True
        try:
            os.system("")  # turns on ANSI escapes in Windows consoles
        except Exception:
            pass


def use_color(stream=None):
    if os.environ.get("NO_COLOR"):
        return False
    return (stream or sys.stdout).isatty() or os.environ.get("FLOW_FORCE_COLOR") == "1"


SGR = {"bold": "1", "dim": "2", "red": "38;5;203", "green": "38;5;114", "yellow": "38;5;221", "blue": "38;5;75",
       "magenta": "38;5;177", "cyan": "38;5;117", "orange": "38;5;214", "gray": "38;5;245", "teal": "38;5;73"}


def paint(text, *styles, stream=None):
    if not styles or not text or not use_color(stream):
        return text
    _vt_once()
    return "\x1b[" + ";".join(SGR[s] for s in styles) + "m" + text + "\x1b[0m"


PREFIX = "flow:"


def prefix(stream=None):
    return paint(PREFIX, "bold", "red", stream=stream) if use_color(stream) else PREFIX


# ---------------------------------------------------------------- decorated output
#
# Every line gets a style from what it says (fmt), and the values people look for stand out inside it
# (hl): ports bold orange, slot names bold cyan, the stamp bold magenta, studio_ids magenta, place ids blue,
# commands in backticks teal, paths gray.

OK_PREFIXES = ("setup ok", "setup:", "pushed ", "claimed ", "stopped ", "blink:", "released", "deleted ",
               "done.", "main place port", "this tree", "Studio connected", "saved ", "added ", "removed ",
               "stage:", "stage created", "worktree created", "worktree exists", "pinned ", "opening ",
               "rokit install: done", "Studio synced", "Studio has ", "Studio reconnected", "local main has everything",
               "local main already", "local main fast-forwarded", "rebased ", "branch has everything", "slot ports already")
WARNISH = ("Studio isn't", "Ask the user", "reconnect Studio", "WARNING", "Studio's Rojo plugin")
GRAYISH = ("(", "FLOW_UP", "not covered", "type check", "state:", "sessions ", "dry run")
STATUS = re.compile(r"^(\S+) (.*?) port (\d+) place (\d+): (.*)$")
SLOT_LINE = re.compile(r"^(\S+) (.*?) port (\d+) place (\d+)( \(default\))?$")
PORTS_LINE = re.compile(r"^(\S+) (.*?): port (\d+)( -> \d+| \(unchanged\))$")
FINDING = re.compile(r"^(ERROR|WARN)\s+(\S*?)(:\d+)?\s+\[([^\]]+)\]\s+(.*)$")
DOCTOR = re.compile(r"^(ok  |FAIL) (.*?)(  -> (.*))?$")
TOKENS = re.compile(
    r"(?P<cmd>`[^`]+`)"
    r"|(?P<uuid>\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b)"
    r"|(?P<stamp>(?<![\w.-])[\w.-]+@[0-9a-f]{7,}(?:\+dirty)?:[0-9a-f]{6}\b)"
    r"|(?P<port>(?:(?<=port )|(?<=port=)|(?<=localhost:)|(?<=-> )|(?<=\[))\d{4,5}(?![\w:-])(?!\.\d))"
    r"|(?P<place>(?:(?<=place )|(?<=placeId )|(?<=place=)|(?<=place\(s\) ))\d{5,}\b)"
    r"|(?P<path>\b[A-Za-z]:\\[^\s,;:)]*)")
SLOT_NAMES = set()  # flow fills this with the project's slot names, so they stand out wherever they appear
_TOKENS = {}


def tokens():
    key = tuple(sorted(SLOT_NAMES))
    if key not in _TOKENS:
        names = "|".join(re.escape(n) for n in sorted(key, key=len, reverse=True))
        extra = rf"|(?P<slot>(?<![\w/\\.@-])(?:{names})(?![\w/\\@-])(?!\.\w))" if names else ""
        _TOKENS[key] = re.compile(TOKENS.pattern + extra)
    return _TOKENS[key]


TOKEN_STYLE = {"slot": ("bold", "cyan"), "cmd": ("teal",), "uuid": ("magenta",), "stamp": ("bold", "magenta"), "port": ("bold", "orange"),
               "place": ("blue",), "path": ("gray",)}


def sgr(*styles):
    return "\x1b[" + ";".join(SGR[s] for s in styles) + "m" if styles else ""


def hl(text, *base):
    """text in the base style, with ports, stamps, ids, commands and paths highlighted in it. Assumes color."""
    _vt_once()
    out, pos, b = [], 0, sgr(*base)
    for m in tokens().finditer(text):
        out.append(text[pos:m.start()])
        out.append("\x1b[0m" + sgr(*TOKEN_STYLE[m.lastgroup]) + m.group(0) + "\x1b[0m" + b)
        pos = m.end()
    out.append(text[pos:])
    body = "".join(out)
    return (b + body + "\x1b[0m") if b else body


def slot_head(slot, label, port, place):
    return (paint(slot, "bold", "cyan") + " " + label + paint(" port ", "gray") + paint(port, "bold", "orange")
            + paint(" place ", "gray") + paint(place, "blue"))


def fmt(line):
    """Color one output line by what it says, highlighting the values in it."""
    if not use_color() or not line.strip():
        return line
    s = line.rstrip("\n")
    m = FINDING.match(s)
    if m:
        sev, path, ln, rule, msg = m.groups()
        bad = sev == "ERROR"
        mark = paint("✗", "bold", "red") if bad else paint("!", "bold", "yellow")
        where = (paint(path, "bold") + paint(ln or "", "gray") + " ") if path else ""
        return f"{mark} {where}{paint('[' + rule + ']', 'red' if bad else 'yellow')} {hl(msg)}"
    if s.startswith(("WARNING", "WARN ")):
        return paint("!", "bold", "yellow") + " " + hl(s, "yellow")
    if s.startswith("ERROR ") or s.lstrip().startswith("[ERROR"):
        return hl(s, "red")
    m = STATUS.match(s)
    if m:
        slot, label, port, place, rest = m.groups()
        parts = []
        for piece in rest.split(", "):
            if piece == "running":
                piece = paint(piece, "green")
            elif piece == "NO Studio connected":
                piece = paint(piece, "bold", "yellow")
            elif piece == "Studio connected":
                piece = paint(piece, "green")
            elif piece == "free":
                piece = paint(piece, "green")
            elif piece.startswith("not running"):
                piece = paint("not running", "gray") + hl(piece[11:])
            elif piece.startswith("held by "):
                piece = paint("held by ", "gray") + paint(piece[8:].split(" (")[0], "bold") + paint(
                    piece[8 + len(piece[8:].split(" (")[0]):], "gray")
            elif piece.startswith("Rojo crashed"):
                piece = hl(piece, "red")
            elif piece.startswith("tree "):
                piece = paint("tree ", "gray") + piece[5:]
            else:
                piece = hl(piece)
            parts.append(piece)
        return slot_head(slot, label, port, place) + ": " + ", ".join(parts)
    m = SLOT_LINE.match(s)
    if m:
        slot, label, port, place, default = m.groups()
        return slot_head(slot, label, port, place) + (paint(default, "green") if default else "")
    m = PORTS_LINE.match(s)
    if m:
        slot, label, port, change = m.groups()
        head = paint(slot, "bold", "cyan") + " " + label + paint(": port ", "gray")
        if change.startswith(" ->"):
            return head + paint(port, "gray") + paint(" -> ", "gray") + paint(change[4:], "bold", "orange")
        return head + paint(port, "bold", "orange") + paint(change, "gray")
    m = DOCTOR.match(s)
    if m:
        state, msg, _, fix = m.groups()
        if state == "FAIL":
            return (paint("✗ FAIL", "bold", "red") + " " + hl(msg) + (paint("  → ", "gray") + hl(fix, "yellow")
                                                                     if fix else ""))
        return paint("✓ ok  ", "green") + " " + hl(msg)
    m = re.match(r"^--- (\S+)( \(.*\))?$", s)
    if m:
        return paint("─── ", "gray") + paint(m.group(1), "bold", "cyan") + (m.group(2) or "")
    m = re.match(r"^checked (.*): (\d+) errors?, (\d+) warnings?$", s)
    if m:
        bad, warn = int(m.group(2)) > 0, int(m.group(3)) > 0
        color = "red" if bad else ("yellow" if warn else "green")
        return (paint("✗" if bad else "✓", "bold", color) + " " + paint(f"checked {m.group(1)}: ", color)
                + paint(f"{m.group(2)} error{'s' * (m.group(2) != '1')}", "bold", "red" if bad else "green") + paint(", ", color)
                + paint(f"{m.group(3)} warning{'s' * (m.group(3) != '1')}", "bold", "yellow" if warn else "green"))
    if TOKENS.fullmatch(s.strip()) and TOKENS.fullmatch(s.strip()).lastgroup == "stamp":
        return hl(s)
    if s.startswith(OK_PREFIXES):
        return paint("✓", "green") + " " + hl(s)
    if s.startswith(WARNISH):
        return paint("!", "bold", "yellow") + " " + hl(s, "yellow")
    if s.startswith(("next:", "note:")):
        return hl(s, "cyan")
    if s.startswith(("use studio_id", "studio_id=", "no studio_id", "when the session")):
        return hl(s, "gray") if s.startswith("when") else paint("→ ", "magenta") + hl(s)
    if s.startswith("slot=") and " port=" in s:
        return re.sub(r"(\w+)=", lambda k: paint(k.group(1) + "=", "gray"), hl(s))
    if s.startswith(GRAYISH):
        return hl(s, "gray")
    if re.match(r"^\d+\. ", s):
        return paint(s[:s.index(" ")], "bold", "yellow") + " " + hl(s[s.index(" ") + 1:], "yellow")
    return hl(s)


def say(*args, **kwargs):
    if kwargs or len(args) != 1 or not isinstance(args[0], str):
        return builtins.print(*args, **kwargs)
    return builtins.print("\n".join(fmt(l) for l in args[0].split("\n")))


# ---------------------------------------------------------------- spinner

class _Busy:
    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    HUES = [51, 45, 39, 75, 111, 147, 183, 219, 213, 207, 171, 135, 99, 63, 33, 38]

    def __init__(self):
        self.lock = threading.Lock()
        self.entries = []
        self.thread = None
        self.ev = None
        self.drawn = False
        force = os.environ.get("FLOW_FORCE_SPINNER") == "1"
        self.on = (sys.stderr.isatty() or force) and not os.environ.get("FLOW_NO_SPINNER")
        self.color = self.on and not os.environ.get("NO_COLOR")

    def push(self, e):
        with self.lock:
            self.entries.append(e)
            if self.on and self.thread is None:
                _vt_once()
                self.ev = threading.Event()
                self.thread = threading.Thread(target=self.loop, args=(self.ev,), daemon=True)
                self.thread.start()

    def pop(self, e):
        with self.lock:
            self.entries.remove(e)
            th, ev = (self.thread, self.ev) if not self.entries else (None, None)
            if th:
                self.thread = None
        if th:
            ev.set()
            th.join()
            if self.drawn:
                sys.stderr.write("\r\x1b[2K\x1b[?25h" if self.color else "\r\x1b[2K")
                sys.stderr.flush()
                self.drawn = False

    def loop(self, ev):
        start, i = time.time(), 0
        while not ev.wait(0.08):
            if time.time() - start < 0.25:  # quick steps never flash a spinner
                continue
            with self.lock:
                es = list(self.entries)
            sys.stderr.write(("\x1b[?25l" if self.color else "") + "\r\x1b[2K" + self.compose(es, time.time() - start, i))
            sys.stderr.flush()
            self.drawn = True
            i += 1

    def compose(self, es, elapsed, i):
        width = max(40, shutil.get_terminal_size((80, 20)).columns - 1)
        main = es[0]["msg"] if es else "Working"
        sub = es[-1]["msg"] if len(es) > 1 else ""
        text = main[: width - 24]
        frame = self.FRAMES[i % len(self.FRAMES)]
        secs = f"{elapsed:.0f}s" if elapsed >= 1.5 else ""
        if not self.color:
            return f"{frame} {text}" + (f" - {sub[:36]}" if sub else "") + (f" {secs}" if secs else "")
        hue = self.HUES[(i // 2) % len(self.HUES)]
        line = f"\x1b[38;5;{hue}m{frame}\x1b[0m \x1b[1m{text}\x1b[0m"
        if sub:
            line += f" \x1b[38;5;244m· {sub[:36]}\x1b[0m"
        if secs:
            line += f" \x1b[38;5;240m{secs}\x1b[0m"
        return line


_busy = _Busy()


@contextlib.contextmanager
def busy(msg):
    """Show a spinner with `msg` while the block runs (after 0.25 s, on a terminal only)."""
    e = {"msg": msg}
    _busy.push(e)
    try:
        yield e
    finally:
        _busy.pop(e)


# what a command is doing, in words. Matched on the longest prefix of the command line.
LABELS = {
    ("rokit", "install"): "Checking tools with rokit",
    ("wally", "install"): "Installing Wally packages",
    ("wally-package-types",): "Generating package types",
    ("rojo", "sourcemap"): "Generating the sourcemap",
    ("blink",): "Generating Blink networking code",
    ("stylua",): "Checking formatting with StyLua",
    ("luau-lsp",): "Type-checking with luau-lsp",
    ("git", "fetch"): "Fetching origin",
    ("git", "rebase"): "Rebasing onto main",
    ("git", "pull"): "Pulling origin",
    ("git", "worktree", "add"): "Creating the stage worktree",
    ("git", "worktree", "remove"): "Removing a worktree",
    ("git", "clean"): "Cleaning the stage",
    ("git", "checkout"): "Checking out the commit",
    ("git", "merge"): "Merging",
}


def label_for(cmd):
    parts = tuple(os.path.basename(str(c)).replace(".exe", "") if i == 0 else str(c) for i, c in enumerate(cmd))
    for n in range(min(3, len(parts)), 0, -1):
        if parts[:n] in LABELS:
            return LABELS[parts[:n]]
    return None
