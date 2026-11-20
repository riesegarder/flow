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
flow = "riesegarder/flow@1.0.0"
```

`rokit install`, then `flow --version`. To update a project, bump the version and `rokit install` again.

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
  "builtin": { "output": "error", "unused": "error", "const": "warn", "server-only": "warn" },
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

- `stamp.file` is where `flow push` writes the stamp (git-excluded); `stamp.instance` is where it shows up in the game once Rojo syncs it (a StringValue). `stamp.hint`: extra lines printed after a push, `{stamp}` is replaced by the new stamp (e.g. how to start the project's test harness).
- `generated`: paths `flow check` refuses edits to.
- `builtin`: the built-in Luau checks, each `"error"`, `"warn"` or `"off"`. `output` = print/warn without a `prefixedlogger`, `unused` = locals never used, `const` = `local` never reassigned, `server-only` = server APIs in `/Shared/` without an `IsServer` guard.
- `rules`: the project's own non-negotiables, regex on added lines. Line rule: `id, severity (error|warn|off), pattern, message, paths?, except?, unless?, unlessPrev?` (`unless` skips the line when it matches, `unlessPrev` when the line before it matches). `multiline: true` runs `pattern` over the whole file and flags a match within 2 lines of an added line. File rule: `mustMatch` instead of `pattern` (the whole file must match when it changed). `paths`/`except` are globs, `**` crosses folders.
- `checks`: the project's own check scripts for what a regex can't do, `[{ "id": "movesets", "run": ["lune", "run", "tools/checks/movesets", "--"] }]`. flow appends the path of a JSON file `{ base, files: { path: [added line numbers] } }`; the script prints findings as `ERROR path:line [rule] message` or `WARN ...` and exits 0, or 1 when it found errors.
- `commands`: project commands that show up as `flow <name>`: `{ "overrides": { "run": ["lune", "run", "tools/x", "--"], "defaultArgs": ["--clip"], "help": "..." } }`. Arguments are passed through; `defaultArgs` are added when no positional argument is given.
- A project without `flow.json` but with `flow.rules.json` is read the same way.

## Commands

```
flow register                          reserve the main place port (servePort in default.project.json)
flow slot <name> <placeId> [--label "scratch 3"] [--port N]
                                       add/update a slot (port defaults to the next free one, always different from the main place port and from every other project's)
flow up [--slot s2 | --all] [--quiet]  fetch origin and warn if local main is behind origin/main (pull first), then
                                       serve the slot's stage: setup (below), blink network.blink, rojo serve, then
                                       "setup ok, port N. Connect Studio...". Once per slot; you connect once.
                                       (flow up <name> serves a specific worktree instead; push then refuses.)
flow doctor [--slot s]                 (sessions) claim a free slot (connected Studios first) + preflight + setup of the
                                       session worktree; rebases the session branch onto local main when it's behind
                                       (the app branches new worktrees from origin/main, so unpushed main commits would
                                       be missing) and prints each commit's old -> new hash
flow push                              (sessions) mirror this worktree, HEAD + uncommitted edits, onto its slot's stage + stamp;
                                       rebases onto local main first, refuses if that conflicts (never stages old code);
                                       regenerates the Blink networking files on the stage (blink network.blink);
                                       refuses when the stage's packages don't match the pushed wally.toml/lock (run
                                       `flow up --slot s` then: reinstalling under a running rojo breaks its sync)
flow release [--slot s]                (sessions) free this worktree's slot; --slot frees any slot (yours only)
flow down [slot | all]                 stop that slot's rojo server; `down all` (or --all) stops every one
flow restart [--all]                   restart the running servers on their current stage (--all: every slot);
                                       up, restart and update all regenerate Blink before serving
flow update [--all]                    fast-forward local main to origin/main, then restart the running servers;
                                       stops nothing if main can't fast-forward (diverged, dirty, off main)
                                       (up, down, restart, update are yours only: they refuse inside agent shells)
flow prune [--yes] [--branches] [--dirty] [--keep n ...] [--older-than 3]
                                       list stale worktrees (dry run), --yes removes them (yours only).
                                       Never loses work: served + recently active ones are kept, dirty ones are
                                       kept unless --dirty, commits not in main stay on their branch (detached ones
                                       get a flow-archive/<name> branch). --branches also deletes fully-merged branches.
flow where                             this worktree's slot, place id, port, stage, Studio connected
flow status                            every slot: server, Studio connected (heuristic), which session holds it
flow wait [slot] [--timeout 600]       block until a Studio connects; exit 3 on timeout (use as a background command)
flow stamp                             write a fresh stamp (flow.json stamp.file, git-excluded); reading the
                                       StringValue back in Studio proves the Rojo sync is live, not stale
flow check [files] [--base REF]        setup, then stylua --check, built-in checks, project rules and luau-lsp type check of added lines;
                                       exit 1 on errors (type errors on added lines are errors, lints are warnings)
```


Studio's Rojo plugin remembers the port per place, so you connect each slot once and only press Connect after that.

State (slots, leases, rojo logs) is per machine and per project in `%LOCALAPPDATA%\flow\<main checkout folder name>`; `FLOW_STATE_DIR` overrides it. `FLOW_HOME` is ignored (older per-project copies of flow set it user-wide) except that its slots' ports are avoided. Give each device its own scratch places (`flow slot s3 <placeId>`) and they never step on each other.

**Setup** (up, doctor, check; never under a running rojo) runs the real installs in that worktree, never links to another checkout: `rokit install`, `wally install` (only when `wally.toml`/`wally.lock` changed since the last install; the hash lives in the worktree's git dir as `flow-wally`), `rojo sourcemap` (every time), then `wally-package-types` after an install. Old `Packages`/`ServerPackages` junctions are removed first.

**Type check** uses luau-lsp with Roblox definitions from `FLOW_LUAU_DEFS`, else `.luau/globalTypes.d.luau` in the main checkout (a project setup script can download it), else the newest `globalTypes*.d.luau` the VS Code/Cursor luau-lsp extension keeps up to date. Only diagnostics on added lines count; the existing code has plenty elsewhere. If no definitions are found, or they don't parse, check says "type check skipped" and why.

`check` does not cover behavior, feel or VFX.

**Ports.** The main place syncs on `servePort` in `default.project.json`; every slot gets its own higher port (main, then main+1, main+2, ...), allocated across all projects on the machine. `flow register` keeps `servePort` if no one else uses it, otherwise moves it. `flow slot` refuses a port that is taken, `flow up` refuses a slot on the main place's port, and `flow status` warns about a clash.

**Output.** Colored results, a braille spinner with a description for every slow step (installs, Blink, type check, waiting for Rojo or Studio, syncing a push), and plain text when piped. `NO_COLOR=1` turns colors off, `FLOW_NO_SPINNER=1` the spinner, `FLOW_FORCE_COLOR=1` / `FLOW_FORCE_SPINNER=1` force them on. Colors and labels live in `flowui.py`.

## Releasing

Bump `VERSION` in `flow/flow.py`, commit, then tag and push:

```
git tag v1.0.1
git push origin main v1.0.1
```

The release workflow builds `flow.exe` with PyInstaller and attaches `flow-<version>-windows-x86_64.zip`, which rokit installs. Local build: `python -m pip install pyinstaller` then `python build.py` (writes `dist/flow.exe`).
