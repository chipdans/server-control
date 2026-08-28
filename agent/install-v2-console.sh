#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "Запустите установщик через sudo." >&2
  exit 1
fi

SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
for required in minecraft_tmux_runner.py minecraft_tmux_payload.py dragonfyre-tmux.service servercontrol-admin.sudoers servercontrol-minecraft.sudoers servercontrol-admin-sshd.conf; do
  if [ ! -f "$SOURCE_DIR/$required" ]; then
    echo "Не найден файл $SOURCE_DIR/$required" >&2
    exit 1
  fi
done

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y openssh-server tmux sudo

install -d -m 0755 /opt/server-control/current
install -m 0755 "$SOURCE_DIR/minecraft_tmux_runner.py" /opt/server-control/current/minecraft_tmux_runner.py
install -m 0755 "$SOURCE_DIR/minecraft_tmux_payload.py" /opt/server-control/current/minecraft_tmux_payload.py

if ! id servercontrol-admin >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash servercontrol-admin
fi
if ! id servercontrol-minecraft >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash servercontrol-minecraft
fi
install -d -m 0700 -o servercontrol-admin -g servercontrol-admin /home/servercontrol-admin/.ssh
install -d -m 0700 -o servercontrol-minecraft -g servercontrol-minecraft /home/servercontrol-minecraft/.ssh
install -d -m 0700 /etc/server-control/ssh

ADMIN_KEY_PATH=/etc/server-control/ssh/servercontrol_admin
MINECRAFT_KEY_PATH=/etc/server-control/ssh/servercontrol_minecraft
if [ ! -f "$ADMIN_KEY_PATH" ]; then
  ssh-keygen -q -t rsa -b 3072 -m PEM -N "" -C "server-control-v2-linux" -f "$ADMIN_KEY_PATH"
fi
if [ ! -f "$MINECRAFT_KEY_PATH" ]; then
  ssh-keygen -q -t rsa -b 3072 -m PEM -N "" -C "server-control-v2-minecraft" -f "$MINECRAFT_KEY_PATH"
fi
chmod 0600 "$ADMIN_KEY_PATH" "$MINECRAFT_KEY_PATH"
chmod 0644 "$ADMIN_KEY_PATH.pub" "$MINECRAFT_KEY_PATH.pub"

ADMIN_AUTHORIZED_KEYS=/home/servercontrol-admin/.ssh/authorized_keys
touch "$ADMIN_AUTHORIZED_KEYS"
ADMIN_PUBLIC_KEY=$(cat "$ADMIN_KEY_PATH.pub")
if ! grep -qF "$ADMIN_PUBLIC_KEY" "$ADMIN_AUTHORIZED_KEYS"; then
  printf '%s %s\n' "no-agent-forwarding,no-port-forwarding,no-X11-forwarding" "$ADMIN_PUBLIC_KEY" >> "$ADMIN_AUTHORIZED_KEYS"
fi
chown servercontrol-admin:servercontrol-admin "$ADMIN_AUTHORIZED_KEYS"
chmod 0600 "$ADMIN_AUTHORIZED_KEYS"

MINECRAFT_AUTHORIZED_KEYS=/home/servercontrol-minecraft/.ssh/authorized_keys
touch "$MINECRAFT_AUTHORIZED_KEYS"
MINECRAFT_PUBLIC_KEY=$(cat "$MINECRAFT_KEY_PATH.pub")
if ! grep -qF "$MINECRAFT_PUBLIC_KEY" "$MINECRAFT_AUTHORIZED_KEYS"; then
  printf '%s %s\n' 'command="sudo -n -u minecraft -H /usr/bin/tmux attach-session -t dragonfyre",no-agent-forwarding,no-port-forwarding,no-X11-forwarding' "$MINECRAFT_PUBLIC_KEY" >> "$MINECRAFT_AUTHORIZED_KEYS"
fi
chown servercontrol-minecraft:servercontrol-minecraft "$MINECRAFT_AUTHORIZED_KEYS"
chmod 0600 "$MINECRAFT_AUTHORIZED_KEYS"

install -m 0440 "$SOURCE_DIR/servercontrol-admin.sudoers" /etc/sudoers.d/servercontrol-admin
install -m 0440 "$SOURCE_DIR/servercontrol-minecraft.sudoers" /etc/sudoers.d/servercontrol-minecraft
visudo -cf /etc/sudoers.d/servercontrol-admin
visudo -cf /etc/sudoers.d/servercontrol-minecraft
install -m 0644 "$SOURCE_DIR/servercontrol-admin-sshd.conf" /etc/ssh/sshd_config.d/99-server-control.conf
sshd -t
systemctl restart ssh.service
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q '^Status: active'; then
  ufw allow 2222/tcp comment 'Server Control SSH'
fi

WAS_ACTIVE=0
if systemctl is-active --quiet dragonfyre.service; then
  WAS_ACTIVE=1
  systemctl stop dragonfyre.service
fi
if [ -f /etc/systemd/system/dragonfyre.service ] && [ ! -f /etc/systemd/system/dragonfyre.service.pre-v2 ]; then
  cp -a /etc/systemd/system/dragonfyre.service /etc/systemd/system/dragonfyre.service.pre-v2
fi
install -m 0644 "$SOURCE_DIR/dragonfyre-tmux.service" /etc/systemd/system/dragonfyre.service

python3 - <<'PY'
import json
from pathlib import Path

config_path = Path("/etc/server-control/agent-config.json")
if config_path.is_file():
    config = json.loads(config_path.read_text(encoding="utf-8"))
    minecraft = config.setdefault("minecraft", {})
    minecraft["console_mode"] = "tmux"
    minecraft["tmux_session"] = "dragonfyre"
    minecraft["rcon_password"] = ""
    temporary = config_path.with_suffix(".json.v2-new")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(config_path)

properties_path = Path("/opt/minecraft/dragonfyre/server.properties")
if properties_path.is_file():
    output = []
    found = False
    for line in properties_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("enable-rcon="):
            output.append("enable-rcon=false")
            found = True
        elif line.startswith("rcon.password=") or line.startswith("rcon.port="):
            continue
        else:
            output.append(line)
    if not found:
        output.append("enable-rcon=false")
    properties_path.write_text("\n".join(output) + "\n", encoding="utf-8")
PY

systemctl daemon-reload
systemctl enable dragonfyre.service
if [ "$WAS_ACTIVE" -eq 1 ]; then
  systemctl start dragonfyre.service
fi
if systemctl cat server-control-agent.service >/dev/null 2>&1; then
  systemctl restart server-control-agent.service
fi

HOST_KEY=/etc/ssh/ssh_host_ed25519_key.pub
if [ ! -f "$HOST_KEY" ]; then
  HOST_KEY=/etc/ssh/ssh_host_rsa_key.pub
fi
HOST_FINGERPRINT=$(ssh-keygen -lf "$HOST_KEY" -E sha256 | awk '{print $2}')
install -m 0600 "$ADMIN_KEY_PATH" /root/server-control-v2-linux-private-key
install -m 0600 "$MINECRAFT_KEY_PATH" /root/server-control-v2-minecraft-private-key

printf '%s\n' \
  "SSH_HOST=46.175.223.107" \
  "SSH_PORT=2222" \
  "SSH_LINUX_USERNAME=servercontrol-admin" \
  "SSH_MINECRAFT_USERNAME=servercontrol-minecraft" \
  "SSH_HOST_KEY_SHA256=$HOST_FINGERPRINT" \
  "SSH_LINUX_PRIVATE_KEY_FILE=/root/server-control-v2-linux-private-key" \
  "SSH_MINECRAFT_PRIVATE_KEY_FILE=/root/server-control-v2-minecraft-private-key" \
  > /root/server-control-v2-ssh-info.txt
chmod 0600 /root/server-control-v2-ssh-info.txt

if id chipdan >/dev/null 2>&1; then
  install -o chipdan -g chipdan -m 0600 "$ADMIN_KEY_PATH" /home/chipdan/server-control-v2-linux-private-key
  install -o chipdan -g chipdan -m 0600 "$MINECRAFT_KEY_PATH" /home/chipdan/server-control-v2-minecraft-private-key
  install -o chipdan -g chipdan -m 0600 /root/server-control-v2-ssh-info.txt /home/chipdan/server-control-v2-ssh-info.txt
fi

echo
echo "Server Control 2 SSH настроен."
echo "На роутере требуется перенаправление TCP 2222 -> 192.168.0.108:2222."
echo "Параметры: /root/server-control-v2-ssh-info.txt"
echo "Закрытые ключи Worker: /root/server-control-v2-linux-private-key и /root/server-control-v2-minecraft-private-key"
echo "Для SCP через chipdan их копии и server-control-v2-ssh-info.txt созданы в /home/chipdan"
cat /root/server-control-v2-ssh-info.txt
