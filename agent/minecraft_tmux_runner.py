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
TMUX_CONFIG = "/etc/server-control/tmux-dragonfyre.conf"


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


def exit_code(exit_file: Path) -> int:
    try:
        return int(exit_file.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return 1


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

    if has_session(args.session):
        print(f"tmux session {args.session} already exists", file=sys.stderr)
        return 1

    # Start a harmless pane first, then configure it before launching Java.
    # The old implementation launched Java immediately and raced against the
    # set-option calls: an early launcher error destroyed the only pane and hid
    # the useful output behind "no server running".
    placeholder = shlex.join(["/bin/sh", "-c", "while :; do sleep 3600; done"])
    created = tmux(
        "-f", TMUX_CONFIG, "new-session", "-d", "-s", args.session, "-c", str(workdir), placeholder,
    )
    if created.returncode != 0:
        print(f"tmux new-session failed: {created.stderr.strip()}", file=sys.stderr)
        return 1

    pane = f"{args.session}:0.0"
    # Keep a failed pane long enough to capture the real launcher error.  The
    # SSH account may type into Java but cannot create a shell window.
    for option, value in (("prefix", "None"), ("prefix2", "None"), ("remain-on-exit", "on")):
        configured = tmux("set-option", "-t", args.session, option, value)
        if configured.returncode != 0:
            print(f"tmux set-option {option} failed: {configured.stderr.strip()}", file=sys.stderr)
            tmux("kill-session", "-t", args.session)
            return 1

    payload_script = Path(__file__).with_name("minecraft_tmux_payload.py").resolve(strict=True)
    payload = [
        sys.executable,
        str(payload_script),
        "--exit-file",
        str(exit_file),
        "--",
        *command,
    ]
    launched = tmux("respawn-pane", "-k", "-t", pane, shlex.join(payload))
    if launched.returncode != 0:
        print(f"tmux respawn-pane failed: {launched.stderr.strip()}", file=sys.stderr)
        tmux("kill-session", "-t", args.session)
        return 1

    stop_sent = False
    stop_deadline = 0.0
    while has_session(args.session):
        dead = tmux("display-message", "-p", "-t", pane, "#{pane_dead}")
        if dead.returncode != 0:
            print(f"tmux pane status failed: {dead.stderr.strip()}", file=sys.stderr)
            return 1
        if dead.stdout.strip() == "1":
            captured = tmux("capture-pane", "-p", "-S", "-200", "-t", pane)
            output = captured.stdout.rstrip()
            code = exit_code(exit_file)
            tmux("kill-session", "-t", args.session)
            if output and (code != 0 or not stop_requested):
                print("Minecraft launcher output:\n" + output, file=sys.stderr)
            return 0 if stop_requested else code
        if stop_requested and not stop_sent:
            tmux("send-keys", "-t", args.session, "stop", "Enter")
            stop_sent = True
            stop_deadline = time.monotonic() + 170
        if stop_sent and time.monotonic() >= stop_deadline:
            tmux("kill-session", "-t", args.session)
            return 1
        time.sleep(0.5)

    return 0 if stop_requested else exit_code(exit_file)


if __name__ == "__main__":
    raise SystemExit(main())
