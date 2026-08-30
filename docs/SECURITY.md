# Безопасность Server Control 1.0

Server Control intentionally has the power to modify server files and stop
processes. Treat the owner account, Cloudflare account and Debian root access as
administrative security boundaries.

## Trust boundaries

| Boundary | Trusted data | Never trusted directly |
|---|---|---|
| Desktop → Worker | signed-in user identity after D1 revalidation | UI permission checks, paths, unit names, commands |
| Agent → Worker | exact constant-time checked `AGENT_API_KEY` | arbitrary Internet requests or user tokens |
| Worker → Agent | known job type and normalized payload | shell text, absolute paths, symlink targets |
| Agent → root | fixed root-owned helper + exact local allow-list | raw `systemctl` arguments from a client |
| Agent → Minecraft | localhost RCON and reviewed startup argv | scripts discovered inside an uploaded pack |

## Accounts and sessions

- Passwords use PBKDF2-HMAC-SHA-256 with a random per-user salt and 100,000
  iterations. Password length is 6–128 characters.
- Access tokens are signed, time-limited and contain a token version. Every
  request reloads the user from D1 and checks `enabled` and token version.
- Disable, password reset, revoke-all or permission update increments the token
  version, invalidating existing sessions immediately.
- Only the owner may create administrators, delegate owner-critical permissions
  or delete accounts with administrative reach.
- Login failures are rate-limited with temporary lockout. Dangerous operations
  also have short server-side replay limits and persistent exclusive locks.

Use a unique owner password and enable MFA for GitHub, Cloudflare and Yandex.
Assign the smallest granular permission set needed; do not grant
`minecraft.files.write`, `updates.manage`, `server.power` or `minecraft.kill` to
ordinary players.

## Secrets

| Secret | Location |
|---|---|
| `JWT_SECRET` | Cloudflare encrypted secret only |
| `BOOTSTRAP_KEY` | Cloudflare secret and temporary owner setup note |
| `AGENT_API_KEY` | Cloudflare secret + root-owned Agent config |
| Yandex OAuth/device ID | Cloudflare secrets only |
| RCON password | root-readable Agent profile/config + Minecraft properties |

The desktop config contains only the Worker URL and public GitHub release
coordinates. Tokens are not written to UI preferences. Agent's full instance
store uses mode `0600`; the runner receives a separate profile without RCON
credentials.

Job lists, cross-user audit views and notifications redact keys such as
password/token/secret/content/command/output. Detailed sensitive results are
visible only to the requesting user and are scrubbed after one hour; normal
retention later deletes the job.

## Filesystem and archive safety

- File operations begin at the selected instance root, normally below
  `/opt/minecraft`.
- Both lexical normalization and canonical containment are checked. Absolute
  paths, drive letters, `..`, NUL and symlink components are rejected.
- Writes use a temporary same-directory file plus `fsync`/atomic replacement.
  Important configuration files receive a bounded safety-history copy.
- ZIP extraction rejects absolute/traversal paths, symlinks, path collisions,
  excessive member counts, excessive expanded size and suspicious compression
  ratios.
- Restore extracts into staging, validates the tree, creates a safety backup,
  then swaps directories. Backup roots/history are excluded to prevent recursive
  copies.
- Text reads, directory pages, searches, log tails and JSON responses are
  bounded. Large files use private chunked transfers and SHA-256.

Do not change `/opt/minecraft` to a directory containing unrelated system data.
Do not add broad roots such as `/`, `/etc` or `/home` to instance profiles.

## Command and service safety

There is no arbitrary Linux terminal. Diagnostics must exactly match
`commands.allow_shell_commands` and run with `shell=False`; separators,
redirects and expansions are rejected.

The sudoers entry may visually contain `*`, but it invokes only
`/usr/local/sbin/server-control-service-control`. That root-owned helper accepts
exactly `start|stop|restart|kill|status` plus one syntactically valid unit,
reloads the local config and instance store, and rejects units outside the
allow-list. `kill` is allowed only for managed Minecraft instance services.

Uploaded `.sh`/`.bat` files are never auto-executed. A managed instance starts
only an argv list saved with `startup_reviewed=true`; the runner uses
`shell=False` as the unprivileged `minecraft` account.

## Network safety

- Use HTTPS Worker URLs only.
- Never expose RCON to the Internet. Keep it on `127.0.0.1` or firewall it to the
  local host.
- No router port-forward is required for Agent; all Agent communication is
  outbound.
- R2 buckets are private. Every transfer route rechecks ownership/permission and
  uses unguessable object keys with expiry.
- Request bodies, responses, event batches and update downloads have explicit
  limits and timeouts. Offline reconnect uses backoff rather than a tight loop.

## Updates

Desktop update requirements:

- public GitHub Release URL;
- published SHA-256;
- bounded ZIP with no traversal/symlink/case collision;
- separate updater process, previous EXE, health marker and rollback.

Agent update requirements:

- public GitHub Release URL and SHA-256 asset;
- exact manifest covering every shipped file;
- staging on the same filesystem;
- validated sudoers, atomic `current` link, service restart;
- three consecutive current-version + Hub-sync health markers;
- automatic rollback of link and system files on failure.

Repository publication does not cryptographically sign a release beyond GitHub
transport and SHA-256 assets generated by the same workflow. For a higher threat
model, add an offline signing key and verify signatures in both updaters.

## Operational hardening

1. Keep Debian, Java, Minecraft and loaders patched.
2. Enable Cloudflare/GitHub/Yandex MFA and review their active sessions.
3. Keep RCON and game administration ports off the public Internet.
4. Back up worlds to a different physical device; an on-host backup is not a
   disaster-recovery copy.
5. Test restore and update rollback on a disposable instance.
6. Review **Аудит** after permission, file, power, update and delete operations.
7. Monitor disk warnings; zero free space can corrupt worlds and backups.
8. Keep `allowed_services` minimal and never grant a general shell through a
   custom wrapper.

## Incident response

- Lost desktop or suspect user: disable the user and revoke all sessions.
- Owner password exposure: change it, revoke sessions and review audit/IP/device.
- Agent key exposure: replace Cloudflare `AGENT_API_KEY` and the root-owned Agent
  config, then restart Agent.
- Yandex token exposure: revoke/reissue it and replace the Cloudflare secret.
- RCON password exposure: change `server.properties` and Agent profile/config,
  then restart that Minecraft instance and Agent.
- GitHub/Cloudflare compromise: revoke sessions/tokens, rotate all related
  secrets, inspect releases/deployments and reinstall from a known-good commit.
