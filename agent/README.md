# Home-server agent

Install this agent on `ChipdanServer`. It contacts the Cloudflare Worker from
inside the home network, so no incoming SSH port needs to be exposed.

## Important Minecraft setup

The current server pack is `/opt/minecraft/dragonfyre`. For dependable console
commands, enable RCON in its `server.properties`:

```properties
enable-rcon=true
rcon.port=25575
rcon.password=use-a-long-random-password
```

Keep RCON reachable only from the local machine. Do **not** forward port 25575
in the router. If the pack has `RESTART=true` in `variables.txt`, set it to
`false`; otherwise a normal Minecraft `stop` may immediately start the pack
again.

## Security model

- The agent runs as a dedicated `servercontrol` account, not as root.
- It only receives commands from the Worker using `AGENT_API_KEY`.
- Linux terminal commands are not shell-executed. They must match the
  `allow_shell_prefixes` in the configuration and are passed to `subprocess`
  without `shell=True`.
- Its only privileged operations are the explicit commands in the sudoers file.

See `../docs/SETUP.md` for the exact deployment sequence.
