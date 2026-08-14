# Server Control

Server Control is a self-hosted control system for a home server and a Minecraft
server. It has three components:

- **Desktop client**: Windows app for sign-in, server status, power, Linux
  commands, Minecraft console and user administration.
- **Cloudflare Worker**: a free, small public API that authenticates users,
  enforces blocks and permissions, and calls the Yandex Smart Home API for the
  `Питание сервера` socket.
- **Home agent**: runs on `ChipdanServer`, executes only authorised server and
  Minecraft commands, and streams console output to signed-in users.

The Worker is intentionally small. It does not store Minecraft worlds, server
logs indefinitely, or SSH passwords. The agent makes outbound HTTPS requests,
so no SSH port or router port-forwarding is needed for the control system.

## What blocking means

Every protected request is checked against the current user record in the
Worker database. When the owner disables a user, their current session is
invalidated immediately and all later power, console and Minecraft requests are
rejected. The desktop client does not contain the Yandex token or home-server
credentials.

## Repository layout

```
worker/          Cloudflare Worker and D1 database migration
agent/           Python agent for the Debian home server
desktop/         Tkinter desktop client and GitHub-release updater
.github/         release workflow for a Windows build
docs/            installation and security notes
```

## Current defaults

- Home server: `ChipdanServer`
- Minecraft directory: `/opt/minecraft/dragonfyre`
- Minecraft launcher: `start.sh`

These values are only defaults in the agent template and can be changed during
installation.

## Security boundaries

- Only the initial owner can create, edit, disable or reset other users.
- User permissions are checked by the Worker and again before the agent runs a
  command.
- Arbitrary Linux commands are restricted to users with `server_command` and
  are run by a low-privilege system account. Configure the allow-list before
  enabling this permission for anyone else.
- A normal socket-off request asks the agent to stop Minecraft and sync disk
  writes first. A forced cut is available only to the owner.

Read [the setup guide](docs/SETUP.md) before deployment.
