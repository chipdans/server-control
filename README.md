# Server Control

Server Control 1.0 is a self-hosted Windows panel for a Debian home server and
multiple Minecraft server packs. It keeps inbound ports closed: the Debian
Agent initiates outbound HTTPS requests to a Cloudflare Worker, while the
desktop client talks only to that authenticated public API.

## What is included

- A dashboard with Agent latency and last response, CPU/load/temperature,
  RAM/Swap, disk space and I/O, network speed, uptime and IP addresses.
- Multiple independent Minecraft instances with separate Java, RAM, ports,
  RCON, loader/version, startup command, notes, tags and backup policy.
- A guided empty/Vanilla/ZIP/import/duplicate workflow. Uploaded scripts are
  detected and displayed, but never executed until an administrator explicitly
  reviews the startup command.
- Live incremental Minecraft console, persistent RCON connection, history,
  filters, search, copy/paste and useful Tab completion for commands, players,
  selectors and common arguments.
- Log-driven startup stages, process/RCON readiness checks and classified crash
  reasons without inventing a diagnosis when evidence is insufficient.
- Root-confined paginated file manager, multi-file chunked transfers, SHA-256,
  transfer progress/speed/ETA/cancel/retry, text editor and a structured
  `server.properties` editor.
- Manual and scheduled backups, retention, safe world save, safety backup before
  restore/update and creation of a new instance from a backup.
- Process, storage, Java and allow-listed systemd service views; safe Linux
  shutdown/reboot and deliberately limited diagnostics instead of a remote shell.
- Persistent jobs, exclusive operation locks, idempotency, notifications,
  audit log, roles and granular permissions.
- Verified desktop and Agent updaters with staged replacement, health checks and
  rollback.

## Components

| Component | Runtime | Responsibility |
|---|---|---|
| Windows client | Python 3.12 / Tk/ttk | UI, local preferences, incremental sync, downloads/uploads and self-update |
| Control Hub | Cloudflare Worker + D1 + private R2 | Authentication, RBAC, jobs, audit, notifications, transfer metadata and temporary transfer bytes |
| Debian Agent | Python 3 | Host metrics, safe filesystem/backup operations, local RCON and allow-listed privileged actions |
| Minecraft runner | systemd template + Python | Starts a reviewed argument vector as the unprivileged `minecraft` user |

The Worker never stores worlds in D1, never receives SSH credentials and never
contains the RCON password. Large temporary files live in a private R2 bucket
and expire automatically. The desktop client stores only non-secret UI
preferences; access tokens remain in process memory.

## Repository layout

```text
desktop/                  Windows client and companion updater
agent/                    Debian Agent, modules, installer and systemd units
worker/                   Cloudflare Worker, D1 migrations and R2 binding
tests/                    Python and Worker regression/security tests
docs/                     Architecture, deployment, security and test guides
.github/workflows/        Validated Windows/Agent release build
```

## Realtime model

The desktop performs one delta-sync request per second. That single response can
contain only changed status, new console rows, changed jobs and new
notifications. It replaces the previous set of independent full-status and log
polls. This architecture preserves the outbound-only Agent security model and
does not require an inbound home-server WebSocket port.

## Security model in one minute

- Every protected request reloads the current user from D1, so disabling an
  account or revoking sessions invalidates an already-issued token immediately.
- Permissions are enforced by the Worker; the Agent additionally accepts only
  known job types and validates every path, filename, service and argument.
- Minecraft paths must remain below `/opt/minecraft`; `..`, absolute paths and
  symlink escapes are rejected. ZIP extraction is bounded and rejects traversal,
  symlinks and suspicious compression ratios.
- The Agent runs as `servercontrol`, Minecraft runs as `minecraft`, and root is
  reached only through two fixed helpers with local allow-list validation.
- Console commands, file contents, outputs and credentials are redacted from
  cross-user job/audit views and scrubbed from retained job results.

See [Security](docs/SECURITY.md) for boundaries and operational rules.

## Installation and upgrade

For a new deployment, follow [Setup](docs/SETUP.md). Existing `0.3.x`
installations should use the complete ordered procedure in
[Upgrade to 1.0](docs/UPGRADE-1.0.0.md); it preserves the current Hub URL,
Agent key, RCON password, users and existing Dragonfyre profile.

Additional references:

- [Architecture](docs/ARCHITECTURE.md)
- [Minecraft console and startup detection](docs/MINECRAFT_CONSOLE.md)
- [Testing and release verification](docs/TESTING.md)
- [Debian Agent details](agent/README.md)

## Version compatibility

Release `v1.0.4` contains desktop client `1.0.4`, Control Hub API `2` and
Agent `2.0.4` (protocol `2`). Agent JSON is compressed on the wire and polling
is bounded for the Workers Free daily
request allowance. The Worker retains compatibility routes during a
rolling upgrade. New management/file/job features stay disabled with a clear
message until Agent protocol 2 is online.

## Honest limitations

- Standard Minecraft RCON does not expose the full Brigadier suggestion graph.
  Exact completion for every mod-specific argument requires a small trusted
  server-side Forge/Fabric/NeoForge integration.
- TPS/MSPT are displayed only when the loader exposes a working performance
  command; unavailable metrics remain `—`.
- Pause is immediate in the active desktop transfer. Downloads resume from a
  `.part` file and chunks retry; resuming a partially uploaded file after the
  entire desktop application has exited is not yet persisted.
- Production Windows, Cloudflare and Debian behaviour must still be verified on
  their actual platforms after the release workflow and deployment complete.
