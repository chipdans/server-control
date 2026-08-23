#!/usr/bin/env python3
"""Root-only validator for the Agent's small systemd control surface."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


CONFIG_PATH = Path("/etc/server-control/agent-config.json")
INSTANCES_PATH = Path("/var/lib/server-control/instances.json")
SYSTEMCTL = "/usr/bin/systemctl"
SERVICE_RE = re.compile(r"^[A-Za-z0-9@_.:-]{1,128}\.service$")


def _object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def allowed_services(config_path: Path = CONFIG_PATH, instances_path: Path = INSTANCES_PATH) -> tuple[set[str], set[str]]:
    config = _object(config_path)
    allowed = {
        str(item) for item in config.get("allowed_services", [])
        if isinstance(item, str) and SERVICE_RE.fullmatch(item)
    }
    legacy = config.get("minecraft") if isinstance(config.get("minecraft"), dict) else {}
    if isinstance(legacy.get("service"), str) and SERVICE_RE.fullmatch(legacy["service"]):
        allowed.add(legacy["service"])
    profiles = _object(instances_path).get("instances", [])
    minecraft: set[str] = set()
    for profile in profiles if isinstance(profiles, list) else []:
        service = profile.get("service") if isinstance(profile, dict) else None
        if isinstance(service, str) and SERVICE_RE.fullmatch(service):
            allowed.add(service)
            minecraft.add(service)
    allowed.discard("server-control-agent.service")
    return allowed, minecraft


def validated_command(
    action: str,
    service: str,
    *,
    config_path: Path = CONFIG_PATH,
    instances_path: Path = INSTANCES_PATH,
) -> list[str]:
    if action not in {"start", "stop", "restart", "kill"} or not SERVICE_RE.fullmatch(service):
        raise PermissionError("invalid service-control request")
    allowed, minecraft = allowed_services(config_path, instances_path)
    if service not in allowed or (action == "kill" and service not in minecraft):
        raise PermissionError("service is not in the local allow-list")
    if action == "kill":
        return [SYSTEMCTL, "kill", "--signal=KILL", service]
    return [SYSTEMCTL, action, service]


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: server-control-service-control ACTION UNIT", file=sys.stderr)
        return 2
    try:
        command = validated_command(sys.argv[1], sys.argv[2])
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=240,
            check=False,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        )
    except (OSError, PermissionError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 1
    sys.stdout.buffer.write(completed.stdout[:128 * 1024])
    sys.stderr.buffer.write(completed.stderr[:128 * 1024])
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
