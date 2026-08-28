"""GitHub Releases update helpers.

The release repository may be public while the source repository stays private.
The desktop client contains no GitHub token; it only downloads a public release
asset whose integrity is additionally protected by HTTPS and GitHub's release
delivery.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any


MAX_RELEASE_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_BOOTSTRAP_UPDATER_BYTES = 128 * 1024 * 1024


def _read_limited(stream: Any, limit: int) -> bytes:
    try:
        declared = int(stream.headers.get("content-length", "0") or 0)
    except (AttributeError, TypeError, ValueError):
        declared = 0
    if declared < 0 or declared > limit:
        raise RuntimeError("Ответ сервера обновлений превышает допустимый размер.")
    result = bytearray()
    while True:
        chunk = stream.read(min(64 * 1024, limit + 1 - len(result)))
        if not chunk:
            return bytes(result)
        result.extend(chunk)
        if len(result) > limit:
            raise RuntimeError("Ответ сервера обновлений превышает допустимый размер.")


def latest_release(repository: str, asset_name: str) -> dict[str, Any] | None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository or ""):
        return None
    url = f"https://api.github.com/repos/{repository}/releases/latest"
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "ServerControlDesktop"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            release = json.loads(_read_limited(response, MAX_RELEASE_RESPONSE_BYTES).decode("utf-8"))
    except (OSError, RuntimeError, UnicodeDecodeError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        return None
    if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
        return None
    assets = release.get("assets", [])
    if not isinstance(assets, list):
        return None
    asset = next((item for item in assets if isinstance(item, dict) and item.get("name") == asset_name), None)
    if not asset or not isinstance(asset.get("browser_download_url"), str) or not asset["browser_download_url"].startswith("https://github.com/"):
        return None
    digest = str(asset.get("digest") or "")
    sha256 = digest.removeprefix("sha256:") if digest.startswith("sha256:") else ""
    if not re.fullmatch(r"[a-fA-F0-9]{64}", sha256):
        checksum_asset = next(
            (item for item in assets if isinstance(item, dict) and item.get("name") == f"{asset_name}.sha256"),
            None,
        )
        if (
            checksum_asset
            and isinstance(checksum_asset.get("browser_download_url"), str)
            and checksum_asset["browser_download_url"].startswith("https://github.com/")
        ):
            try:
                checksum_request = urllib.request.Request(
                    checksum_asset["browser_download_url"],
                    headers={"Accept": "application/octet-stream", "User-Agent": "ServerControlDesktop"},
                )
                with urllib.request.urlopen(checksum_request, timeout=15) as response:
                    checksum_text = _read_limited(response, 1024).decode("ascii", "strict")
                match = re.search(r"\b([a-fA-F0-9]{64})\b", checksum_text)
                sha256 = match.group(1) if match else ""
            except (OSError, UnicodeDecodeError, urllib.error.URLError, urllib.error.HTTPError):
                sha256 = ""
    return {
        "tag": str(release.get("tag_name", "")),
        "url": asset["browser_download_url"],
        "sha256": sha256.lower() if re.fullmatch(r"[a-fA-F0-9]{64}", sha256) else None,
    }


def is_newer(remote: str, local: str) -> bool:
    semver = re.compile(
        r"^[vV]?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
        r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z.-]+)?$"
    )

    def parse(value: str) -> tuple[tuple[int, int, int], tuple[str, ...] | None] | None:
        match = semver.fullmatch(value.strip())
        if not match:
            return None
        return (
            (int(match.group(1)), int(match.group(2)), int(match.group(3))),
            tuple(match.group(4).split(".")) if match.group(4) else None,
        )

    remote_value = parse(remote)
    local_value = parse(local)
    if not remote_value or not local_value:
        return False
    remote_core, remote_pre = remote_value
    local_core, local_pre = local_value
    if remote_core != local_core:
        return remote_core > local_core
    if remote_pre is None:
        return local_pre is not None
    if local_pre is None:
        return False
    for remote_part, local_part in zip(remote_pre, local_pre):
        if remote_part == local_part:
            continue
        remote_numeric = remote_part.isdigit()
        local_numeric = local_part.isdigit()
        if remote_numeric and local_numeric:
            return int(remote_part) > int(local_part)
        if remote_numeric != local_numeric:
            return not remote_numeric
        return remote_part > local_part
    return len(remote_pre) > len(local_pre)


def download_update(asset_url: str, *, expected_sha256: str | None = None) -> Path:
    if not asset_url.startswith("https://github.com/"):
        raise RuntimeError("Обновление можно загружать только из GitHub Releases.")
    if not expected_sha256 or not re.fullmatch(r"[a-f0-9]{64}", expected_sha256, re.IGNORECASE):
        raise RuntimeError("Релиз не содержит проверяемую SHA-256 сумму обновления.")
    directory = Path(tempfile.mkdtemp(prefix="server-control-update-"))
    destination = directory / "update.zip"
    request = urllib.request.Request(asset_url, headers={"User-Agent": "ServerControlDesktop"})
    digest = hashlib.sha256()
    written = 0
    try:
        with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as target:
            while chunk := response.read(1024 * 1024):
                written += len(chunk)
                if written > 512 * 1024 * 1024:
                    raise RuntimeError("Архив обновления превышает лимит 512 МиБ.")
                target.write(chunk)
                digest.update(chunk)
            target.flush()
            os.fsync(target.fileno())
        if digest.hexdigest() != expected_sha256.lower():
            raise RuntimeError("SHA-256 обновления не совпала с опубликованной суммой.")
        if not zipfile.is_zipfile(destination):
            raise RuntimeError("GitHub Release содержит не ZIP-файл обновления.")
        return destination
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise


def prepare_bootstrap_updater(update_zip: Path, current_executable: Path) -> Path:
    """Extract the updater bundled with a release before the GUI exits.

    The normal updater EXE is often the executable that is currently running,
    so it cannot safely replace itself.  A short-lived ``.bootstrap`` copy is
    launched instead; it can update both the client and the persistent updater
    before it starts the new client.
    """

    updater_name = "ServerControlUpdater.exe" if sys.platform == "win32" else "ServerControlUpdater"
    bootstrap = current_executable.with_name(f"{Path(updater_name).stem}.bootstrap{Path(updater_name).suffix}")
    try:
        with zipfile.ZipFile(update_zip) as archive:
            member = archive.getinfo(updater_name)
            file_type = (member.external_attr >> 16) & 0o170000
            if member.is_dir() or file_type == 0o120000:
                raise RuntimeError("Некорректный помощник обновления в архиве.")
            if member.file_size <= 0 or member.file_size > MAX_BOOTSTRAP_UPDATER_BYTES:
                raise RuntimeError("Помощник обновления превышает допустимый размер.")
            if member.compress_size and member.file_size / max(1, member.compress_size) > 250:
                raise RuntimeError("Подозрительная степень сжатия помощника обновления.")
            with archive.open(member) as source:
                contents = _read_limited(source, MAX_BOOTSTRAP_UPDATER_BYTES)
    except (KeyError, OSError, zipfile.BadZipFile):
        # Compatibility with releases from before the updater was bundled.
        return current_executable.with_name(updater_name)
    temporary = bootstrap.with_name(f"{bootstrap.name}.new")
    try:
        temporary.write_bytes(contents)
        os.replace(temporary, bootstrap)
    except OSError as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError(f"Не удалось подготовить программу обновления: {error}") from error
    return bootstrap


def launch_updater(update_zip: Path, current_executable: Path) -> None:
    """Launch a sibling updater after this graphical process has exited."""
    updater_name = "ServerControlUpdater.exe" if sys.platform == "win32" else "ServerControlUpdater"
    updater = prepare_bootstrap_updater(update_zip, current_executable)
    if not updater.exists():
        raise RuntimeError("Не найден ServerControlUpdater рядом с программой.")
    command = [
        str(updater),
        "--wait-pid",
        str(os.getpid()),
        "--zip",
        str(update_zip),
        "--target",
        str(current_executable.parent),
        "--restart",
        str(current_executable),
        "--updater-target",
        str(current_executable.with_name(updater_name)),
    ]
    if sys.platform == "win32":
        # The companion waits for this exact process before replacing the EXE.
        # It is detached so closing the Tk window cannot terminate the updater.
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        subprocess.Popen(
            command,
            close_fds=True,
            creationflags=creationflags,
        )
        return
    subprocess.Popen(command, close_fds=True)
