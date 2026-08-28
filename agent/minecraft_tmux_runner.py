#!/usr/bin/env python3
"""Supervise a Minecraft tmux session while systemd supervises this process."""

from __future__ import annotations

import argparse
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path


SESSION_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def tmux(*arguments: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/usr/bin/tmux", *arguments],
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def has_session(session: str) -> bool:
    return tmux("has-session", "-t", session).returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--exit-file", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not SESSION_RE.fullmatch(args.session):
        parser.error("invalid tmux session name")
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        parser.error("missing Minecraft command")

    workdir = Path(args.workdir).resolve(strict=True)
    if not workdir.is_dir():
        parser.error("Minecraft workdir is not a directory")
    exit_file = Path(args.exit_file)
    exit_file.parent.mkdir(parents=True, exist_ok=True)
    exit_file.unlink(missing_ok=True)
    stop_requested = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    if not has_session(args.session):
        payload_script = Path(__file__).with_name("minecraft_tmux_payload.py").resolve(strict=True)
        payload = [
            sys.executable,
            str(payload_script),
            "--exit-file",
            str(exit_file),
            "--",
            *command,
        ]
        created = tmux(
            "new-session",
            "-d",
            "-s",
            args.session,
            "-c",
            str(workdir),
            shlex.join(payload),
        )
        if created.returncode != 0:
            print(created.stderr.strip() or "tmux could not create the Minecraft session", file=sys.stderr)
            return 1

    # The SSH Minecraft account may type into Java, but it must not use the
    # tmux prefix to create a shell window as the minecraft Unix user.
    for option in ("prefix", "prefix2"):
        configured = tmux("set-option", "-t", args.session, option, "None")
        if configured.returncode != 0:
            print(configured.stderr.strip() or f"tmux could not disable {option}", file=sys.stderr)
            return 1

    stop_sent = False
    stop_deadline = 0.0
    while has_session(args.session):
        if stop_requested and not stop_sent:
            tmux("send-keys", "-t", args.session, "stop", "Enter")
            stop_sent = True
            stop_deadline = time.monotonic() + 170
        if stop_sent and time.monotonic() >= stop_deadline:
            tmux("kill-session", "-t", args.session)
            return 1
        time.sleep(0.5)

    if stop_requested:
        return 0
    try:
        return int(exit_file.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
