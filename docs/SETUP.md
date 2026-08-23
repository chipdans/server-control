# Новая установка Server Control 1.0

Для обновления существующей версии `0.3.x` используйте не эту страницу, а
[единый сценарий обновления](UPGRADE-1.0.0.md).

## 1. Требования

- Cloudflare account with Workers, D1 and R2;
- public GitHub Releases repository (the source repository may remain private);
- Debian server with Python 3.11+, systemd, `sudo`, `unzip` and `visudo`;
- locally working Minecraft pack or permission to install Vanilla;
- Yandex Smart Home OAuth token and the device ID of the server power socket.

Never put `JWT_SECRET`, `BOOTSTRAP_KEY`, `AGENT_API_KEY`, Yandex OAuth, RCON
password or private credentials in Git, screenshots or the desktop config.

## 2. Deploy Control Hub

From `worker/` on an administrator computer:

```powershell
npx wrangler login
npx wrangler d1 create server-control
npx wrangler r2 bucket create server-control-files
npx wrangler r2 bucket create server-control-files-preview
```

Copy the D1 `database_id` into `worker/wrangler.toml`. Bucket names must match
the `FILES` binding already present in that file.

Generate three different random secrets of at least 32 bytes. For example:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Apply all migrations, enter secrets only into Wrangler prompts, and deploy:

```powershell
npx wrangler d1 migrations apply server-control --remote
npx wrangler secret put JWT_SECRET
npx wrangler secret put BOOTSTRAP_KEY
npx wrangler secret put AGENT_API_KEY
npx wrangler secret put YANDEX_OAUTH_TOKEN
npx wrangler secret put YANDEX_DEVICE_ID
npx wrangler deploy
```

Keep `BOOTSTRAP_KEY` until the first owner is created. `AGENT_API_KEY` must also
be placed in `/etc/server-control/agent-config.json`; it never goes into the
desktop client.

Verify the public, unauthenticated liveness endpoint:

```powershell
curl.exe https://YOUR-WORKER.workers.dev/health
```

It should return `{"ok":true,...}`. The detailed `/v1/health` endpoint requires
a signed-in user and filters fields according to permissions.

## 3. Prepare Minecraft and RCON

For each existing pack, enable local RCON in `server.properties`:

```properties
enable-rcon=true
rcon.port=25575
rcon.password=USE_A_LONG_RANDOM_PASSWORD
```

Use a unique RCON and game port for every concurrently running instance. Bind or
firewall RCON to localhost; never port-forward it. For Dragonfyre, set
`RESTART=false` so a normal stop is not immediately undone by its shell loop.

The 1.0 installer creates an unprivileged `minecraft` account and a managed
systemd template. Existing packs can continue using an allow-listed legacy
service until imported into the managed profile list.

## 4. Build or download the release

Push a semantic tag such as `v1.0.0`. The release workflow first validates
Python, JavaScript, shell syntax, migrations and tests on Ubuntu, then performs
the Windows PyInstaller build. It publishes:

- `ServerControl-Setup.zip` and SHA-256;
- `ServerControl-Update.zip` and SHA-256;
- `ServerControl-Agent.zip` and SHA-256.

If release assets are placed in another public repository, set GitHub Actions:

- variable `RELEASE_REPOSITORY=owner/public-release-repository`;
- secret `RELEASE_REPOSITORY_TOKEN`, fine-grained **Contents: read/write** only
  for that repository.

## 5. Install Debian Agent

Download the matching release assets on Debian and verify the archive before
extracting it:

```bash
cd /tmp
curl -fL https://github.com/OWNER/REPOSITORY/releases/download/v1.0.0/ServerControl-Agent.zip -o ServerControl-Agent.zip
curl -fL https://github.com/OWNER/REPOSITORY/releases/download/v1.0.0/ServerControl-Agent.zip.sha256 -o ServerControl-Agent.zip.sha256
sha256sum -c ServerControl-Agent.zip.sha256
install -d -m 0700 /tmp/server-control-agent-1.0.0
unzip -q ServerControl-Agent.zip -d /tmp/server-control-agent-1.0.0
sudo sh /tmp/server-control-agent-1.0.0/install-agent.sh
```

On a new installation the installer creates
`/etc/server-control/agent-config.json` from the example and intentionally does
not start with placeholder secrets successfully. Edit it as root:

```bash
sudoedit /etc/server-control/agent-config.json
```

Required values:

- `hub_url`: deployed HTTPS Worker URL;
- `agent_api_key`: exact Cloudflare `AGENT_API_KEY` value;
- legacy `minecraft.rcon_password`: current pack's local RCON password;
- `allowed_services`: only exact extra services the UI may control.

Do not broaden the sudoers wildcard. It points only to a root-owned helper that
revalidates exact action and unit names against local configuration/profiles.

Restart and verify:

```bash
sudo systemctl restart server-control-agent.service
sudo systemctl --no-pager --full status server-control-agent.service
sudo journalctl -u server-control-agent.service -n 100 --no-pager
```

## 6. Install Windows client

Extract `ServerControl-Setup.zip` into:

```text
%LOCALAPPDATA%\ServerControl
```

Edit only `server-control.json` next to `ServerControl.exe`:

```json
{
  "api_base_url": "https://YOUR-WORKER.workers.dev",
  "update": {
    "enabled": true,
    "install_automatically": true,
    "repository": "OWNER/PUBLIC-RELEASE-REPOSITORY",
    "asset_name": "ServerControl-Update.zip"
  }
}
```

The updater accepts only GitHub Release asset URLs and requires a matching
SHA-256. It keeps the previous executable and rolls back if the new client does
not write its health marker.

## 7. Create owner and users

1. Start `ServerControl.exe`.
2. Choose **Первоначальная настройка**.
3. Enter the saved `BOOTSTRAP_KEY`, owner login and a 12–128 character password.
4. Open **Пользователи** and assign an exact preset or granular permissions.
5. Revoke `BOOTSTRAP_KEY` from Cloudflare after setup if you do not need to keep
   it; the API also refuses a second owner bootstrap while an owner exists.

Suggested least-privilege presets:

| Preset | Typical access |
|---|---|
| Viewer | server/Minecraft/file read and logs |
| File Manager | Viewer plus confined file changes |
| Operator | start/stop/restart, console and players |
| Admin | most administration except owner-critical power/delete/user delegation |

## 8. Add or import instances

Open **Сборки → Добавить** and choose empty, Vanilla, ZIP, existing directory or
duplicate. For ZIP/import, review detected loader, Java, ports and startup
command. The first launch remains blocked until the startup command is marked as
reviewed. Never approve a command from an untrusted pack without inspecting its
files.

## 9. Post-install verification

Perform these checks before calling the installation production-ready:

1. `/health` responds and authenticated **Обновления** shows Worker/DB/Agent.
2. Agent status is `active (running)` and Agent protocol is `2`.
3. Dashboard values update and stop changing to fake values when Agent is off.
4. `list` returns an `[RCON]` response; console receives only new rows.
5. Start/stop/restart one test instance and confirm real log-driven stages.
6. Create and download a backup, then restore it only on a disposable test copy.
7. Upload/download a test file and compare SHA-256.
8. Create a Viewer and verify write/admin/server details are unavailable.
9. Disable that user and verify their existing session is rejected immediately.
10. Test desktop and Agent update rollback on a non-production release first.

See [Testing](TESTING.md) for the repository checks and [Security](SECURITY.md)
for hardening and incident actions.
