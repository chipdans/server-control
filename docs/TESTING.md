# Проверка и выпуск

## Локальная автоматическая проверка

From repository root:

```bash
python -m compileall -q agent desktop tests
node --check worker/src/index.js
node --check worker/src/control_plane.js
sh -n agent/install-agent.sh
python -m unittest discover -s tests -p 'test_*.py' -v
node --test tests/worker_auth.test.mjs
git diff --check
```

The Python suite applies every D1 migration to an in-memory SQLite database and
tests metrics, RCON packets/reuse, console filtering, Agent backoff, safe paths,
symlinks, ZIP traversal, pagination/lost-update protection, profile secret
separation, pack detection, Java compatibility, backup recursion/retention,
crash classification, bounded logs/HTTP and updater manifests/archives.

The Worker suite tests immediate account revocation, idempotent safe power-off,
Yandex cache fallback, path/service validation, empty transfers, event bounds,
server-side permission filtering, sensitive job redaction, legacy API filtering,
oversized input, consolidated Agent sync and delegated-permission escalation.

## CI release gates

Every release tag has two ordered GitHub Actions jobs:

1. `validate` on Ubuntu: syntax, migrations, Python/Worker tests and shell parse.
2. `build` on Windows, only after `validate`: repeats tests, builds both EXEs,
   packages desktop/setup and verified Agent bundle, calculates SHA-256 and
   creates/updates the release.

A red job is a failed release. Do not manually upload a binary to bypass it.

## Manual integration matrix

Automated tests cannot emulate Cloudflare, Windows file locking, Yandex, systemd
and a real modpack together. Before declaring a release stable, record results
for this matrix:

| Area | Positive case | Negative/recovery case |
|---|---|---|
| Auth/RBAC | each preset sees intended pages/actions | disabled/revoked token rejected immediately |
| Feeds | core status, power, console and notices update independently | one route failing preserves the other last-known values |
| Power | socket on and safe off | Yandex timeout returns stale cache; duplicate off is reused |
| Minecraft | start → stages → Running; list/RCON | crash classification, stop timeout then explicit force |
| Instances | empty/Vanilla/import/ZIP/duplicate | bad ports/Java/RAM, unreviewed script, traversal ZIP |
| Files | paginate/edit/search/multi-upload/download | symlink/traversal, conflict mtime, cancel/retry/hash mismatch |
| Backups | save/create/download/test restore | low disk, cancel, corrupt archive, safety rollback |
| Linux | diagnostics/services/reboot confirmation | arbitrary command/unit rejected, Agent offline blocked |
| Desktop update | replace/restart/health | corrupt hash/ZIP and failed health roll back |
| Agent update | stage/restart/three health markers | bad manifest/hash and failed Hub sync roll back |

Use a disposable Minecraft instance for destructive restore/update/kill cases.
Never run first-time negative tests on the only copy of a world.

## Performance observations

Measure on the actual deployment while a large modpack runs:

- Worker request count: desktop status every 5 seconds, power/notifications
  every 15 seconds, console every 2 seconds only while visible, plus one Agent
  poll per 3 seconds and one heartbeat per 15 seconds;
- independent response sizes and latency when idle versus during log bursts;
- desktop process CPU/memory after an hour of console use;
- Agent memory while Hub is offline (event buffer must remain bounded);
- directory/search/log latency at configured caps;
- upload/download throughput and retry behaviour;
- D1 query duration and R2 storage expiry.

Do not use fake TPS/MSPT or a timed startup bar as a performance shortcut.

## Release checklist

1. Versions are coherent (`APP_VERSION`, `AGENT_VERSION`, API/protocol docs).
2. No secrets or user config were added (`git diff` and secret scan).
3. All local checks above pass.
4. Commit is pushed; annotated tag points to that exact commit.
5. Both GitHub jobs are green and all six release assets exist.
6. SHA-256 files verify.
7. Deploy Worker migration/code, then Agent, then desktop.
8. Complete the integration matrix and retain the previous known-good release.
