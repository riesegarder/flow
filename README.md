# flow

Parallel agent sessions for Rojo projects, each with its own Roblox Studio scratch place. No AI, no tokens, no console windows. Windows only for now.

One session = one worktree = one scratch place. Each scratch place is a slot with its own port and its own
stage worktree (`.claude/worktrees/flow-<slot>`) that Rojo serves. A session claims a free slot and pushes
its own worktree onto that slot's stage, so Studio never has to reconnect between sessions.

## Install

Through [rokit](https://github.com/rojo-rbx/rokit), pinned per project like every other tool:

```toml
# rokit.toml
[tools]
flow = "riesegarder/flow@1.2.0"
```

`rokit install`, then `flow --version`. To update a project, bump the version and `rokit install` again.

Windows builds run everything; Linux and macOS builds run `flow check` and project commands (the server commands are Windows only).

Needs `git` and `rojo` on PATH; `wally`, `wally-package-types`, `blink`, `stylua` and `luau-lsp` are used when the project has them.

## Project settings: `flow.json`

Optional, in the project root. Everything has a default; give only what differs.

```json
{
  "stamp": {
    "file": "src/Modules/Shared/FlowStamp.txt",
    "instance": "ReplicatedStorage.Source.Modules.SharedModules.FlowStamp"
  },
  "generated": ["Packages/", "ServerPackages/", "sourcemap.json", "BlinkClient.luau", "BlinkServer.luau"],
  "slots": {
    "s1": { "placeId": 132127026711314, "label": "scratch 1" },
    "s2": { "placeId": 78112935310712, "label": "scratch 2" },
    "pc": { "placeId": 81087880746986, "hosts": ["DESKTOP-F6CQCB9"] }
  },
  "builtin": { "output": "error", "unused": "error", "const": "warn", "server-only": "warn" },
  "ignore": [".lune/**", "tests/**"],
  "reservedPorts": [37870],
  "rules": [
    {
      "id": "no-global-wait",
      "severity": "error",
      "pattern": "(?<![\w.])wait\(",
      "message": "use task.wait",
      "paths": ["src/**/*.luau"]
    }
  ]
}
```

- `slots`: the scratch places, written by `flow slot` (commit it: a new device gets every slot without typing place ids again). Order matters: ports count up from the main place port in this order. `hosts` limits a slot to those machines (`flow slot <name> <placeId> --here`). Ports are per machine and never stored here.
- `stamp.file` is where `flow push` writes the stamp (git-excluded); `stamp.instance` is where it shows up in the game once Rojo syncs it (a StringValue). `stamp.hint`: extra lines printed after a push, `{stamp}` is replaced by the new stamp (e.g. how to start the project's test harness).
- `generated`: paths `flow check` refuses edits to.
- `builtin`: the built-in Luau checks, each `"error"`, `"warn"` or `"off"`. `output` = print/warn without a `prefixedlogger`, `unused` = locals never used, `const` = `local` never reassigned, `server-only` = server APIs in `/Shared/` without an `IsServer` guard.
- `ignore`: globs the built-in checks and the type check skip (default `.lune/**`: Lune scripts aren't Roblox code). Project rules and StyLua still run on them.
- `reservedPorts`: ports the project's other tools listen on (a test listener, say); no slot ever gets one, and `flow status` warns when a slot has one (`flow ports` moves it).
- `rules`: the project's own non-negotiables, regex on added lines. Line rule: `id, severity (error|warn|off), pattern, message, paths?, except?, unless?, unlessPrev?` (`unless` skips the line when it matches, `unlessPrev` when the line before it matches). `multiline: true` runs `pattern` over the whole file and flags a match within 2 lines of an added line. File rule: `mustMatch` instead of `pattern` (the whole file must match when it changed). `paths`/`except` are globs, `**` crosses folders.
- `checks`: the project's own check scripts for what a regex can't do, `[{ "id": "movesets", "run": ["lune", "run", "tools/checks/movesets", "--"] }]`. flow appends the path of a JSON file `{ base, files: { path: [added line numbers] } }`; the script prints findings as `ERROR path:line [rule] message` or `WARN ...` and exits 0, or 1 when it found errors.
- `commands`: project commands that show up as `flow <name>`: `{ "overrides": { "run": ["lune", "run", "tools/x", "--"], "defaultArgs": ["--clip"], "help": "..." } }`. Arguments are passed through; `defaultArgs` are added when no positional argument is given.
- A project without `flow.json` but with `flow.rules.json` is read the same way.

## Commands

Slot names are plain arguments everywhere (`flow up s2`, `flow restart s1 s2`); `--slot s2` still works.

```
flow slot                              list this machine's slots and ports
flow slot <name> <placeId> [--label "scratch 3"] [--port N] [--here]
                                       (placeId may be a pasted roblox.com/games/<id>/... link)
                                       add/update a slot in flow.json, add the place to default.project.json
                                       servePlaceIds, then serve it (like `flow up <name>`). --no-up: don't serve,
                                       --no-add: don't touch servePlaceIds, --here: only this machine uses it,
                                       --port: this machine's port for it. A running slot whose port and place
                                       didn't change keeps running.
flow slot <name> --remove              stop its server and remove the slot
flow ports [--dry-run]                 renumber the slot ports from the main place port (main+1, main+2, ... in
                                       slot order, skipping ports other projects or programs use); running servers
                                       move to their new port and the toast lists the new ports
flow open [slot ...] [--all] [--force] open the slots' places in Studio (a new device: `flow open --all`); places a
                                       Studio already has open are skipped. Asks Roblox for each place's experience id
                                       once (cached in ~/.flow/universes.json)
flow pin [version]                     pin this project's rokit.toml to the latest flow release (or that version), then
                                       `rokit install`; commit rokit.toml. flow says once a day when a newer release is
                                       out (FLOW_NO_UPDATE_CHECK=1 turns that off)
flow register                          reserve the main place port (servePort in default.project.json)
flow up [slot ...] [--all] [--quiet]   fetch origin and warn if local main is behind origin/main (pull first), then
                                       serve each slot's stage: setup (below), blink network.blink, rojo serve, then
                                       "setup ok, port N. Connect Studio...". Once per slot; you connect once.
                                       No slot: the default one. --tree <name> serves a worktree instead of the stage
                                       (push then refuses).
flow doctor [slot]                     (sessions) claim a free slot (connected Studios first) + preflight + setup of the
                                       session worktree; rebases the session branch onto local main when it's behind
                                       (the app branches new worktrees from origin/main, so unpushed main commits would
                                       be missing) and prints each commit's old -> new hash. Finds the slot's Studio
                                       through the Studio MCP and prints its studio_id; warns when Studio's Rojo plugin
                                       doesn't fit the project's rojo (version, or upstream plugin with the fork pinned)
flow push                              (sessions) mirror this worktree, HEAD + uncommitted edits, onto its slot's stage + stamp,
                                       then read the stamp back from Studio (below); rebases onto local main first, refuses
                                       if that conflicts (never stages old code); regenerates the Blink networking files on
                                       the stage (blink network.blink). Restarts the slot's Rojo on the same port when
                                       Rojo died during the push, when default.project.json changed (Rojo doesn't apply
                                       Workspace/Lighting properties to a connected place) or when wally.toml/lock changed
                                       (reinstalls the stage's packages first), but only when nobody is connected or the
                                       plugin reconnects by itself; otherwise a package change refuses (`flow up s`) and a
                                       project change warns.
flow verify [slot] [--timeout 20]      (sessions) read the last push's stamp back from Studio: exit 0 synced, 5 Studio has
                                       another stamp (Rojo isn't syncing), 6 flow couldn't read it
flow release [slot]                    (sessions) free this worktree's slot; naming a slot frees any slot (yours only)
flow release --stale [--older-than 2]  free every slot whose session saw no push for 2 hours or whose worktree is gone
flow down [slot ... | all]             stop those slots' rojo servers; `down all` (or --all) stops every one
flow down --port N                     stop whatever rojo listens on port N (a stale server `flow status` warns about)
flow restart [slot ...] [--all]        restart the running servers (or the named ones) on their current stage;
                                       up, restart and update all regenerate Blink before serving
flow update [--all]                    fast-forward local main to origin/main, then restart the running servers;
                                       stops nothing if main can't fast-forward (diverged, dirty, off main)
                                       (up, down, restart, update, slot, ports, register, open, pin are yours only: they refuse
                                       inside agent shells)
flow prune [--yes] [--branches] [--dirty] [--keep n ...] [--older-than 3]
                                       list stale worktrees (dry run), --yes removes them (yours only).
                                       Never loses work: served + recently active ones are kept, dirty ones are
                                       kept unless --dirty, commits not in main stay on their branch (detached ones
                                       get a flow-archive/<name> branch). --branches also deletes fully-merged branches.
flow where [--json]                    this worktree's slot, place id, port, stage, Studio connected, studio_id
flow status [--json]                   every slot: server, Studio connected (heuristic), which session holds it and how
                                       long ago it pushed, the crash lines of the Rojo log when a server died, plugin warnings.
                                       status, where, doctor, wait and push first adopt servers flow lost track of
                                       (a rojo it started, on a port no slot has): the slot's port is moved to it, no restart
flow wait [slot] [--timeout 600]       block until a Studio connects, then check it has the last stamp; exit 3 on timeout,
                                       5 when Studio has another stamp (use as a background command)
flow log [slot] [-n 40] [-f]           the slot's Rojo log (this worktree's slot, else the default); -f follows it
flow port [slot | main]                print the slot's port (or the main place's), for scripts
flow stamp                             write a fresh stamp (flow.json stamp.file, git-excluded) and check Studio has it
flow check [files] [--base REF]        setup, then stylua --check (respecting .styluaignore), built-in checks, project rules
                                       and luau-lsp type check of added lines; exit 1 on errors (type errors on added lines
                                       are errors, lints are warnings). Blink output is regenerated first and never flagged.
```

A server is "running" when a rojo listens on its slot's port, whatever pid was recorded. Slot leases are taken under a lock, so sessions starting at the same moment never get the same slot.

**Auto-reconnecting plugin.** With the [riesegarder/rojo](https://github.com/riesegarder/rojo) fork (`-reconnect` releases, "Reconnect On Server Loss" on), Studio reconnects by itself when its server comes back on the same port. flow detects the fork from the installed plugin (`%LOCALAPPDATA%\Roblox\Plugins\RojoManagedPlugin.rbxm`) and its setting; then restarting a slot Studio was connected to (`restart`, `update`, `up`, `slot`, or push restarting a crashed server) toasts "reconnecting", waits, and toasts "reconnected" (or "connect Studio" if it doesn't come back within 30 s). `FLOW_ROJO_RECONNECT=1`/`0` overrides the detection. `flow ports` still needs a manual connect: the plugin keeps retrying the old port.

**Stamp check.** flow talks to Roblox's own `StudioMCP.exe` (the server behind the roblox-studio MCP; flow starts the newest one under `%LOCALAPPDATA%\Roblox\Versions`, `FLOW_STUDIO_MCP` overrides it). After a push it finds the Studio that has the slot's place open and reads `stamp.instance` in its Edit datamodel, read-only, until it equals the new stamp (20 s at most). Synced: it prints `Studio synced` and the studio_id to use. Another value: exit 5, test nothing. When it can't ask (Studio closed, its MCP server off), it prints the Luau to check by hand as before. Studio not connected: it says so; `flow wait` checks the stamp once Studio is back.

Studio's Rojo plugin remembers the port per place, so you connect each slot once and only press Connect after that.

**State.** Slots are in `flow.json` (shared). This machine's ports, leases, running servers and Rojo logs are in the repo's git folder, `.git/flow/` (per machine, per project, never committed; `FLOW_STATE_DIR` overrides it). Which ports each project uses is recorded machine-wide in `~/.flow/ports.json`. Nothing is kept in `%LOCALAPPDATA%` any more: the Claude desktop app is a packaged app, and Windows gives everything it starts a private copy of `%LOCALAPPDATA%`, so flow in a session and flow in a normal terminal saw two different state files. The first 1.2 run takes over a project's 1.1 state from `%LOCALAPPDATA%\flow\<project>`, and the first user command after that saves its slots in `flow.json`.

**Setup** (up, doctor, check; never under a running rojo) runs the real installs in that worktree, never links to another checkout: `rokit install`, `wally install` (only when `wally.toml`/`wally.lock` changed since the last install; the hash lives in the worktree's git dir as `flow-wally`), `rojo sourcemap` (every time), then `wally-package-types` after an install. Old `Packages`/`ServerPackages` junctions are removed first.

**Type check** uses luau-lsp with Roblox definitions from `FLOW_LUAU_DEFS`, else `.luau/globalTypes.d.luau` in the main checkout (a project setup script can download it), else the newest `globalTypes*.d.luau` the VS Code/Cursor luau-lsp extension keeps up to date. Only diagnostics on added lines count; the existing code has plenty elsewhere. If no definitions are found, or they don't parse, check says "type check skipped" and why.

`check` does not cover behavior, feel or VFX.

**Ports.** The main place syncs on `servePort` in `default.project.json`; a new slot gets main + its position in `flow.json` (or the next free port), never one another project on the machine uses. `flow ports` renumbers every slot that way again, e.g. after `servePort` changed. `flow register` keeps `servePort` if no one else uses it, otherwise moves it. `flow slot --port` refuses a port that is taken, `flow up` refuses a slot on the main place's port, and `flow status` warns about a clash.

**Output.** Colored results, a braille spinner with a description for every slow step (installs, Blink, type check, waiting for Rojo or Studio, syncing a push), and plain text when piped. `NO_COLOR=1` turns colors off, `FLOW_NO_SPINNER=1` the spinner, `FLOW_FORCE_COLOR=1` / `FLOW_FORCE_SPINNER=1` force them on. Colors and labels live in `flowui.py`.

## Releasing

Bump `VERSION` in `flow/flow.py`, commit, then tag and push:

```
git tag v1.2.0
git push origin main v1.2.0
```

The release workflow builds `flow.exe` with PyInstaller and attaches `flow-<version>-<windows|linux|macos>-<arch>.zip` for each platform; rokit picks the right one. Local build: `python -m pip install pyinstaller` then `python build.py` (writes `dist/flow.exe`).
