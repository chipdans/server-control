# Minecraft-консоль, запуск и crash detection

## Живая консоль

Desktop receives console deltas in the shared one-second sync response and only
appends unseen rows. It never redraws the full remote log on each tick. Agent
tails `latest.log` incrementally with a partial-line buffer and uses one
persistent authenticated localhost RCON connection for status and commands.

Available UI behaviour:

- Enter sends immediately and shows `▶ /command` optimistically;
- `[RCON]` marks the server response;
- Up/Down navigate persisted per-instance history;
- Tab and the suggestion list complete known command roots, online player names,
  selectors and common arguments;
- filter `INFO/WARN/ERROR/DEBUG`, search, timestamps and color levels;
- selectable text, clipboard shortcuts, horizontal scroll, local clear and
  optional auto-scroll;
- duplicate rows are compacted and console memory is bounded.

Minecraft's normal `RCON Client started/shutting down/connection closed` lines
are transport bookkeeping, not gameplay errors. They are filtered in both Agent
and desktop, including old rows received during a rolling update. Repeated Agent
network failures are coalesced into one warning and one recovery message; real
Minecraft/Java errors remain visible.

## Tab completion limitation

RCON can execute a command and return text but does not expose the Brigadier
command tree/suggestion packet used by an in-game client. Server `help`, built-in
command knowledge and live player names cover common administration without a
server mod. Exact completion of every coordinate, registry value and mod-specific
argument requires a trusted local Forge/Fabric/NeoForge extension that exports
Brigadier suggestions to Agent. The current UI does not pretend otherwise.

## Log-driven startup stages

The progress bar is not timer-driven. Agent combines systemd/process state,
observed log evidence and final RCON readiness. A percentage is a coarse stage
position, not a measured fraction of every mod.

| Stage | Typical evidence |
|---|---|
| Preparation | start job accepted, profile/resources/Java checked |
| Java startup | JVM process/service becomes active, Java/ModLauncher banner |
| Loader | Vanilla server, Forge/FML, NeoForge or Fabric loader markers |
| Mod discovery | mod file scanning/discovery messages |
| Mod initialization | constructing mods/common setup/mod loading |
| Registries/datapacks | registries, recipes, tags and datapack reload |
| World loading | loading/preparing level |
| Spawn | `Preparing spawn/start region: N%` |
| Network services | game listener and RCON startup |
| Ready | `Done (...)!` and/or successful RCON readiness probe |

The tracker recognizes wording from common Vanilla, Forge, NeoForge and Fabric
logs. A pack with custom messages can skip stages; the UI shows the latest
proven stage instead of advancing by elapsed time.

## Unified states

- `OFFLINE`: no live process/service and no unacknowledged crash evidence.
- `STARTING`: process active but readiness not confirmed.
- `RUNNING`: actual `Done`/RCON evidence confirms readiness.
- `STOPPING`: graceful stop/restart is in progress.
- `CRASHED`: process ended unexpectedly with captured evidence.
- `UPDATING`, `BACKING_UP`, `RESTORING`: exclusive job controls the instance.
- `UNKNOWN`: available evidence is inconsistent or unavailable.

Systemd `active` alone never means `RUNNING`.

## Crash classification

Agent classifies only explicit evidence and keeps the matching log excerpt:

| Code | Evidence examples | Suggested direction |
|---|---|---|
| `out_of_memory` | `OutOfMemoryError`, allocation failure | reduce RAM/process load or increase available memory |
| `java_version` | unsupported class version, wrong Java markers | assign a compatible Java executable |
| `missing_dependency` | missing/required mod dependency | install the exact dependency/version named in log |
| `port_in_use` | bind/address already in use | choose free game/RCON ports |
| `permission_denied` | permission denied/access denied | repair ownership/group access under instance root |
| `missing_file` | missing JAR/args/file markers | review detected startup files and profile |
| `corrupted_world` | explicit world/region corruption markers | stop writes and restore a verified backup |
| `java_error` | JVM fatal error report | inspect `hs_err_pid*.log` and Java/native mods |
| `unknown` | process died without a known marker | no guessed cause; inspect captured crash/service log |

The console page can copy the summary, suggested action and evidence. The log
viewer reads only bounded tails/chunks from `latest.log`, crash reports,
systemd, Agent and updater logs.

## Safe start/stop/restart

Before start, Agent validates reviewed argv, directory, Java compatibility,
RAM availability and port conflicts. Optional `before_start` backup runs first.

Graceful stop sends `save-all flush` and `stop` through RCON when available,
waits for the managed service/process, then stops systemd. Force kill is a
separate permission and confirmation path.

Restart performs stop → wait for process/port release → start → progress
tracking → `RUNNING` or `CRASHED`. Exclusive job locks prevent a second start,
restart, restore or update from racing the first operation.
