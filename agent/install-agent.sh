#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this installer with sudo." >&2
  exit 1
fi

bundle_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
manifest="$bundle_dir/manifest.json"
if [ ! -f "$manifest" ]; then
  echo "manifest.json is missing from ServerControl-Agent.zip" >&2
  exit 1
fi

agent_version=$(python3 - "$manifest" <<'PY'
import hashlib, json, pathlib, re, sys
manifest = pathlib.Path(sys.argv[1]).resolve()
root = manifest.parent
with manifest.open(encoding="utf-8") as source:
    value = json.load(source)
version = str(value.get("agent_version", ""))
if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", version):
    raise SystemExit("invalid agent version")
files = value.get("files")
required = {"server_control_agent.py", "agent_update_helper.py", "service_control_helper.py", "instance_runner.py", "server-control-agent.service", "server-control-minecraft@.service", "servercontrol-sudoers.example", "sc_agent/__init__.py"}
if not isinstance(files, dict) or not required.issubset(files) or len(files) > 5000:
    raise SystemExit("invalid manifest files")
total_bytes = 0
for relative, expected in files.items():
    pure = pathlib.PurePosixPath(str(relative).replace("\\", "/"))
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts) or not re.fullmatch(r"[a-f0-9]{64}", str(expected)):
        raise SystemExit(f"unsafe manifest entry: {relative}")
    path = root.joinpath(*pure.parts)
    if not path.is_file() or path.is_symlink():
        raise SystemExit(f"manifest verification failed: {relative}")
    total_bytes += path.stat().st_size
    if total_bytes > 512 * 1024 * 1024:
        raise SystemExit("manifest files exceed 512 MiB")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(4 * 1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise SystemExit(f"manifest verification failed: {relative}")
actual = {
    path.relative_to(root).as_posix()
    for path in root.rglob("*")
    if path.is_file()
    and not path.is_symlink()
    and path.name != "manifest.json"
    and path.suffix != ".pyc"
    and "__pycache__" not in path.parts
}
if actual != set(files):
    raise SystemExit("manifest file set does not match bundle contents")
print(version)
PY
)

if ! id servercontrol >/dev/null 2>&1; then
  useradd --system --home /var/lib/server-control --shell /usr/sbin/nologin servercontrol
fi
if ! id minecraft >/dev/null 2>&1; then
  useradd --system --home /opt/minecraft --shell /usr/sbin/nologin minecraft
fi

usermod -a -G minecraft servercontrol
if getent group systemd-journal >/dev/null 2>&1; then
  usermod -a -G systemd-journal servercontrol
fi
install -d -o root -g root -m 0755 /opt/server-control /opt/server-control/releases
install -d -o servercontrol -g minecraft -m 2750 /var/lib/server-control
install -d -o minecraft -g minecraft -m 2770 /opt/minecraft
install -d -o servercontrol -g servercontrol -m 0750 /srv/server-control/backups

release_dir="/opt/server-control/releases/$agent_version"
# Never reuse a partially written or locally modified release directory. Keep
# the current process runnable and install the verified bundle under a unique
# sibling instead.
if [ -e "$release_dir" ] && ! PYTHONPATH="$bundle_dir" python3 - "$release_dir" <<'PY'
import pathlib, sys
from agent_update_helper import verify_manifest
verify_manifest(pathlib.Path(sys.argv[1]))
PY
then
  release_dir="$release_dir-reinstall-$(date +%Y%m%d%H%M%S)-$$"
fi
temporary_dir="/opt/server-control/releases/.install-$agent_version-$$"
rm -rf -- "$temporary_dir"
install -d -o root -g root -m 0755 "$temporary_dir"

for item in server_control_agent.py instance_runner.py agent_update_helper.py service_control_helper.py install-agent.sh server-control-agent.service server-control-minecraft@.service servercontrol-sudoers.example config.example.json manifest.json sc_agent; do
  if [ ! -e "$bundle_dir/$item" ]; then
    echo "Agent bundle is missing $item" >&2
    rm -rf -- "$temporary_dir"
    exit 1
  fi
  cp -a -- "$bundle_dir/$item" "$temporary_dir/"
done

python3 -m compileall -q "$temporary_dir"
chown -R root:root "$temporary_dir"
find "$temporary_dir" -type d -exec chmod 0755 {} \;
find "$temporary_dir" -type f -exec chmod 0644 {} \;
chmod 0755 "$temporary_dir/server_control_agent.py" "$temporary_dir/instance_runner.py" "$temporary_dir/agent_update_helper.py" "$temporary_dir/service_control_helper.py"

if [ -e "$release_dir" ]; then
  rm -rf -- "$temporary_dir"
else
  mv -- "$temporary_dir" "$release_dir"
fi

ln -sfn "$release_dir" /opt/server-control/.current-new
mv -Tf /opt/server-control/.current-new /opt/server-control/current

install -o root -g root -m 0644 "$bundle_dir/server-control-agent.service" /etc/systemd/system/server-control-agent.service
install -o root -g root -m 0644 "$bundle_dir/server-control-minecraft@.service" /etc/systemd/system/server-control-minecraft@.service
install -o root -g root -m 0755 "$bundle_dir/agent_update_helper.py" /usr/local/sbin/server-control-agent-update
install -o root -g root -m 0755 "$bundle_dir/service_control_helper.py" /usr/local/sbin/server-control-service-control
install -o root -g root -m 0440 "$bundle_dir/servercontrol-sudoers.example" /etc/sudoers.d/servercontrol
visudo -cf /etc/sudoers.d/servercontrol >/dev/null

install -d -o root -g servercontrol -m 0750 /etc/server-control
if [ ! -f /etc/server-control/agent-config.json ]; then
  install -o root -g servercontrol -m 0640 "$bundle_dir/config.example.json" /etc/server-control/agent-config.json
  echo "Created /etc/server-control/agent-config.json. Fill in the existing Hub/RCON secrets before starting Agent." >&2
else
  chown root:servercontrol /etc/server-control/agent-config.json
  chmod 0640 /etc/server-control/agent-config.json
fi

# File Manager needs group write access but never changes ownership to the
# Agent. Minecraft remains the owner of its server files.
find /opt/minecraft -xdev -type d -exec chmod g+rwx {} \;
find /opt/minecraft -xdev -type f -exec chmod g+rw {} \;
chown -R servercontrol:servercontrol /srv/server-control/backups
find /srv/server-control/backups -xdev -type d -exec chmod 0700 {} \;
find /srv/server-control/backups -xdev -type f -exec chmod 0600 {} \;

systemctl daemon-reload
systemctl enable server-control-agent.service >/dev/null
systemctl restart server-control-agent.service
systemctl is-active --quiet server-control-agent.service
echo "Server Control Agent $agent_version is active. Existing config and secrets were preserved."
