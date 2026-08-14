"""Standalone companion executable which installs a downloaded application ZIP."""

from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path


def wait_for_process(pid: int) -> None:
    if sys.platform == "win32":
        synchronize = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
        if handle:
            ctypes.windll.kernel32.WaitForSingleObject(handle, 60_000)
            ctypes.windll.kernel32.CloseHandle(handle)
        return
    while True:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.2)


def safe_extract(archive: zipfile.ZipFile, destination: Path) -> None:
    destination_resolved = destination.resolve()
    for member in archive.infolist():
        candidate = (destination / member.filename).resolve()
        if candidate != destination_resolved and destination_resolved not in candidate.parents:
            raise RuntimeError("Недопустимый путь внутри архива обновления.")
    archive.extractall(destination)


def main() -> int:
    parser = argparse.ArgumentParser(description="Install a Server Control update")
    parser.add_argument("--wait-pid", type=int, required=True)
    parser.add_argument("--zip", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--restart", type=Path, required=True)
    args = parser.parse_args()

    wait_for_process(args.wait_pid)
    staging = Path(tempfile.mkdtemp(prefix="server-control-apply-"))
    try:
        with zipfile.ZipFile(args.zip) as archive:
            safe_extract(archive, staging)
        replacement = staging / args.restart.name
        if not replacement.is_file():
            raise RuntimeError(f"В архиве нет {args.restart.name}")
        args.target.mkdir(parents=True, exist_ok=True)
        temporary_target = args.target / f"{args.restart.stem}.new{args.restart.suffix}"
        shutil.copy2(replacement, temporary_target)
        os.replace(temporary_target, args.restart)
        subprocess.Popen([str(args.restart)], cwd=str(args.target), close_fds=True)
        return 0
    finally:
        shutil.rmtree(staging, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
