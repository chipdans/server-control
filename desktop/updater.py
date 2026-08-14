"""GitHub Releases update helpers.

The release repository may be public while the source repository stays private.
The desktop client contains no GitHub token; it only downloads a public release
asset whose integrity is additionally protected by HTTPS and GitHub's release
delivery.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any


def latest_release(repository: str, asset_name: str) -> dict[str, Any] | None:
    if not repository or "/" not in repository:
        return None
    url = f"https://api.github.com/repos/{repository}/releases/latest"
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "ServerControlDesktop"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            release = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        return None
    if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
        return None
    assets = release.get("assets", [])
    if not isinstance(assets, list):
        return None
    asset = next((item for item in assets if isinstance(item, dict) and item.get("name") == asset_name), None)
    if not asset or not isinstance(asset.get("browser_download_url"), str):
        return None
    return {"tag": str(release.get("tag_name", "")), "url": asset["browser_download_url"]}


def is_newer(remote: str, local: str) -> bool:
    def version_parts(value: str) -> tuple[int, ...]:
        values = re.findall(r"\d+", value.lstrip("vV"))
        return tuple(int(part) for part in values[:4]) or (0,)

    remote_parts = version_parts(remote)
    local_parts = version_parts(local)
    longest = max(len(remote_parts), len(local_parts))
    return remote_parts + (0,) * (longest - len(remote_parts)) > local_parts + (0,) * (longest - len(local_parts))


def download_update(asset_url: str) -> Path:
    directory = Path(tempfile.mkdtemp(prefix="server-control-update-"))
    destination = directory / "update.zip"
    request = urllib.request.Request(asset_url, headers={"User-Agent": "ServerControlDesktop"})
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as target:
        while chunk := response.read(1024 * 1024):
            target.write(chunk)
    if not zipfile.is_zipfile(destination):
        raise RuntimeError("GitHub Release содержит не ZIP-файл обновления.")
    return destination


def launch_updater(update_zip: Path, current_executable: Path) -> None:
    """Launch a sibling updater after this graphical process has exited."""
    updater_name = "ServerControlUpdater.exe" if sys.platform == "win32" else "ServerControlUpdater"
    updater = current_executable.with_name(updater_name)
    if not updater.exists():
        raise RuntimeError("Не найден ServerControlUpdater рядом с программой.")
    command = [
        str(updater),
        "--wait-pid",
        "0",
        "--zip",
        str(update_zip),
        "--target",
        str(current_executable.parent),
        "--restart",
        str(current_executable),
    ]
    if sys.platform == "win32":
        # Closing a Tk window does not always end the packaged process at once.
        # Start the updater after a short delay instead of making it wait for up
        # to a minute on a process handle.  The delay gives this app time to exit.
        delayed_command = "timeout /t 2 /nobreak >NUL && " + subprocess.list2cmdline(command)
        subprocess.Popen(
            ["cmd.exe", "/d", "/c", delayed_command],
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return
    subprocess.Popen(command, close_fds=True)
