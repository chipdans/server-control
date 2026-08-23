#!/usr/bin/env python3
"""Exec one reviewed instance command for server-control-minecraft@.service."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path


STATE_FILE = Path(os.environ.get("SERVER_CONTROL_INSTANCE_STORE", "/var/lib/server-control/instances.json"))


def fail(message: str) -> "NoReturn":  # type: ignore[name-defined]
    print(f"Server Control instance runner: {message}", file=sys.stderr, flush=True)
    raise SystemExit(2)


def main() -> int:
    if len(sys.argv) != 2:
        fail("expected exactly one instance id")
    instance_id = sys.argv[1]
    if not instance_id or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in instance_id):
        fail("invalid instance id")
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        fail(f"cannot read instance store: {error}")
    profiles = data.get("instances") if isinstance(data, dict) else None
    profile = next((item for item in profiles or [] if isinstance(item, dict) and item.get("id") == instance_id), None)
    if not profile:
        fail("instance profile not found")
    if profile.get("startup_reviewed") is not True:
        fail("startup command has not been reviewed")
    stored_command = profile.get("startup_command")
    if not isinstance(stored_command, list) or not stored_command or not all(isinstance(item, str) and item and "\0" not in item and "\n" not in item for item in stored_command):
        fail("invalid startup command")
    jvm_arguments = profile.get("jvm_arguments", [])
    startup_arguments = profile.get("startup_arguments", [])
    if not isinstance(jvm_arguments, list) or not isinstance(startup_arguments, list):
        fail("invalid JVM or startup arguments")
    if not all(isinstance(item, str) and item and len(item) <= 2048 and "\0" not in item and "\n" not in item for item in [*jvm_arguments, *startup_arguments]):
        fail("invalid JVM or startup argument")
    directory = Path(str(profile.get("directory", ""))).resolve(strict=True)
    root = Path(os.environ.get("SERVER_CONTROL_MINECRAFT_ROOT", "/opt/minecraft")).resolve(strict=True)
    try:
        directory.relative_to(root)
    except ValueError:
        fail("instance directory is outside minecraft root")
    configured_java = str(profile.get("java", "")).strip()
    executable = configured_java or stored_command[0]
    if not os.path.isabs(executable):
        executable = shutil.which(executable, path="/usr/local/bin:/usr/bin:/bin") or ""
    if not executable or not Path(executable).is_file() or not os.access(executable, os.X_OK):
        fail("startup executable is unavailable")
    if Path(executable).name != "java":
        fail("only a reviewed Java executable may start a managed instance")
    base_arguments = [item for item in stored_command[1:] if not item.lower().startswith(("-xms", "-xmx"))]
    # Old profiles stored ``nogui`` in startup_command.  Avoid passing it twice
    # after the new separate startup_arguments setting is introduced.
    for argument in startup_arguments:
        if base_arguments and base_arguments[-1] == argument:
            base_arguments.pop()
    try:
        ram_min = max(256, min(int(profile.get("ram_min_mb", 2048)), 131_072))
        ram_max = max(ram_min, min(int(profile.get("ram_max_mb", 8192)), 131_072))
    except (TypeError, ValueError):
        fail("invalid RAM limits")
    command = [
        executable,
        f"-Xms{ram_min}M",
        f"-Xmx{ram_max}M",
        *jvm_arguments,
        *base_arguments,
        *startup_arguments,
    ]
    environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HOME": str(directory),
        "USER": "minecraft",
        "LOGNAME": "minecraft",
    }
    os.chdir(directory)
    os.execve(executable, command, environment)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
