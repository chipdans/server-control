# Debian Agent 2.0

Agent provides outbound-only control of the Debian host and Minecraft instances.
It runs as `servercontrol`; managed Minecraft processes run as `minecraft`.

## Package contents

- `server_control_agent.py`: loop, heartbeat, events, metrics and RCON runtime;
- `sc_agent/`: safe instances, jobs, files, backups, inventory and security;
- `instance_runner.py`: reviewed argv runner for one managed systemd instance;
- `service_control_helper.py`: exact root action/unit allow-list;
- `agent_update_helper.py`: verified staged updater with rollback;
- `server-control-agent.service`: Agent service;
- `server-control-minecraft@.service`: managed instance template;
- `install-agent.sh`: first-install/0.3.x migration installer.

Release workflow adds `manifest.json` containing Agent version, release tag and
SHA-256 of every shipped file. Installer and updater reject missing, extra,
modified, symlinked or unsafe-path files.

## Runtime locations

| Path | Purpose / owner |
|---|---|
| `/etc/server-control/agent-config.json` | secrets/config, `root:servercontrol 0640` |
| `/var/lib/server-control/instances.json` | full profiles including RCON, `servercontrol 0600` |
| `/var/lib/server-control/runner-instances.json` | secret-free runner profiles, group-readable by Minecraft |
| `/var/lib/server-control/agent-health.json` | current version/Hub-sync health marker |
| `/opt/server-control/releases/` | immutable verified releases |
| `/opt/server-control/current` | atomic active-release symlink |
| `/opt/minecraft/` | confined instance roots, owned by `minecraft` |
| `/srv/server-control/backups/` | backups, owned by `servercontrol` |
| `/var/log/server-control-updater.log` | Agent updater log |

## Configuration

Start from `config.example.json`. Required values are `hub_url`, matching
`agent_api_key` and the current legacy instance RCON password. Keep
`allowed_services` exact and minimal. `allow_shell_commands` are complete exact
diagnostic strings, not prefixes.

Old 0.3.x configuration remains valid. On first start Agent creates a profile
for the legacy `minecraft` section and then stores additional instances in its
state directory.

## Security properties

- no incoming network listener;
- no `shell=True` and no arbitrary terminal;
- canonical root confinement and symlink rejection for files;
- bounded logs, events, payloads, workers and process inventory;
- local-only RCON connection reuse;
- exact privileged helper validation;
- safe ZIP staging and atomic file/profile writes;
- Agent update health requires the expected version and successful Hub sync.

## Operations

```bash
sudo systemctl --no-pager --full status server-control-agent.service
sudo journalctl -u server-control-agent.service -n 100 --no-pager
sudo systemctl restart server-control-agent.service
```

After Agent 2.0 is installed, an authorized owner/admin can request later Agent
versions from the desktop **Обновления** page. The updater stages the release,
schedules activation outside the running Agent process and rolls back if health
is not confirmed.

See [new setup](../docs/SETUP.md), [0.3.x upgrade](../docs/UPGRADE-1.0.0.md) and
[security](../docs/SECURITY.md).
