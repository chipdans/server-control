#!/usr/bin/env python3
"""Run the real Minecraft command inside tmux and persist its exit status."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 4 or sys.argv[1] != "--exit-file" or "--" not in sys.argv[3:]:
        print("usage: minecraft_tmux_payload.py --exit-file PATH -- COMMAND...", file=sys.stderr)
        return 64
    separator = sys.argv.index("--", 3)
    exit_file = Path(sys.argv[2])
    command = sys.argv[separator + 1:]
    if not command:
        print("Minecraft start command is empty", file=sys.stderr)
        return 64
    code = 1
    try:
        code = subprocess.run(command, check=False).returncode
    except OSError as error:
        print(f"Could not start Minecraft: {error}", file=sys.stderr)
        code = 127
    finally:
        try:
            exit_file.write_text(str(code), encoding="ascii")
        except OSError:
            pass
    return code


if __name__ == "__main__":
    raise SystemExit(main())
