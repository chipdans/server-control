#!/usr/bin/env python3
"""Root-only staged Agent updater with health check and automatic rollback."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath


REPOSITORY = "chipdans/server-control"
INSTALL_ROOT = Path("/opt/server-control")
RELEASES_ROOT = INSTALL_ROOT / "releases"
CURRENT_LINK = INSTALL_ROOT / "current"
LOG_PATH = Path("/var/log/server-control-updater.log")
SERVICE = "server-control-agent.service"
MAX_DOWNLOAD_BYTES = 128 * 1024 * 1024
MAX_UNPACKED_BYTES = 512 * 1024 * 1024


def log(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(line, flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as output:
            output.write(f"{line}\n")
    except OSError:
        pass


def github_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "ServerControlAgentUpdater/2"})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise RuntimeError("GitHub response is too large")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError("GitHub returned an invalid response")
    return value


def _read_checksum(url: str) -> str:
    request = urllib.request.Request(url, headers={"Accept": "application/octet-stream", "User-Agent": "ServerControlAgentUpdater/2"})
    with urllib.request.urlopen(request, timeout=30) as response:
        text = response.read(2048).decode("ascii", "strict")
    match = re.search(r"\b([a-fA-F0-9]{64})\b", text)
    if not match:
        raise RuntimeError("Agent checksum asset is invalid")
    return match.group(1).lower()


def resolve_release(version: str) -> tuple[str, str, str]:
    endpoint = "latest" if version == "latest" else f"tags/{version if version.startswith('v') else 'v' + version}"
    release = github_json(f"https://api.github.com/repos/{REPOSITORY}/releases/{endpoint}")
    tag = str(release.get("tag_name", ""))
    asset = next((item for item in release.get("assets", []) if isinstance(item, dict) and item.get("name") == "ServerControl-Agent.zip"), None)
    if not tag or not asset or not str(asset.get("browser_download_url", "")).startswith("https://github.com/"):
        raise RuntimeError("Release does not contain ServerControl-Agent.zip")
    digest = str(asset.get("digest") or "")
    sha256 = digest.removeprefix("sha256:").lower() if digest.startswith("sha256:") else ""
    if not re.fullmatch(r"[a-f0-9]{64}", sha256):
        checksum = next((item for item in release.get("assets", []) if isinstance(item, dict) and item.get("name") == "ServerControl-Agent.zip.sha256"), None)
        checksum_url = str(checksum.get("browser_download_url", "")) if checksum else ""
        if not checksum_url.startswith("https://github.com/"):
            raise RuntimeError("Release does not contain a trusted Agent SHA-256 asset")
        sha256 = _read_checksum(checksum_url)
    return tag, str(asset["browser_download_url"]), sha256


def download(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"Accept": "application/octet-stream", "User-Agent": "ServerControlAgentUpdater/2"})
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("xb") as output:
        total = int(response.headers.get("content-length", 0) or 0)
        if total > MAX_DOWNLOAD_BYTES:
            raise RuntimeError("Agent archive exceeds 128 MiB")
        written = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > MAX_DOWNLOAD_BYTES:
                raise RuntimeError("Agent archive exceeds 128 MiB")
            output.write(chunk)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def safe_extract(archive_path: Path, destination: Path) -> None:
    root = destination.resolve(strict=False)
    with zipfile.ZipFile(archive_path) as archive:
        infos = archive.infolist()
        if not infos or len(infos) > 5000:
            raise RuntimeError("Agent archive contains an invalid number of files")
        unpacked = 0
        normalized_names: set[str] = set()
        for info in infos:
            pure = PurePosixPath(info.filename.replace("\\", "/"))
            if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
                raise RuntimeError("Agent archive contains an unsafe path")
            normalized = "/".join(pure.parts).casefold()
            if normalized in normalized_names:
                raise RuntimeError("Agent archive contains duplicate paths")
            normalized_names.add(normalized)
            target = destination.joinpath(*pure.parts).resolve(strict=False)
            target.relative_to(root)
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise RuntimeError("Agent archive may not contain symlinks")
            unpacked += max(0, int(info.file_size))
            if unpacked > MAX_UNPACKED_BYTES:
                raise RuntimeError("Agent archive expands beyond 512 MiB")
            if info.compress_size > 0 and info.file_size > 1024 * 1024 and info.file_size / info.compress_size > 250:
                raise RuntimeError("Agent archive has a suspicious compression ratio")
        archive.extractall(destination)


def verify_manifest(directory: Path) -> tuple[str, str]:
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    version = str(manifest.get("agent_version", ""))
    release_tag = str(manifest.get("release_tag", ""))
    files = manifest.get("files")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", version) or not re.fullmatch(r"v\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", release_tag) or not isinstance(files, dict) or not files or len(files) > 5000:
        raise RuntimeError("Agent manifest is invalid")
    total_bytes = 0
    for relative, expected in files.items():
        pure = PurePosixPath(str(relative).replace("\\", "/"))
        if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
            raise RuntimeError(f"Unsafe path in Agent manifest: {relative}")
        path = directory.joinpath(*pure.parts)
        if not path.is_file() or path.is_symlink() or not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected):
            raise RuntimeError(f"Agent file is missing: {relative}")
        total_bytes += path.stat().st_size
        if total_bytes > MAX_UNPACKED_BYTES:
            raise RuntimeError("Agent manifest exceeds 512 MiB")
        digest_builder = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(4 * 1024 * 1024):
                digest_builder.update(chunk)
        digest = digest_builder.hexdigest()
        if digest != expected:
            raise RuntimeError(f"SHA-256 mismatch: {relative}")
    actual_files = {
        path.relative_to(directory).as_posix()
        for path in directory.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and path.name != "manifest.json"
        and path.suffix != ".pyc"
        and "__pycache__" not in path.parts
    }
    if actual_files != set(files):
        missing = sorted(set(files) - actual_files)[:5]
        extra = sorted(actual_files - set(files))[:5]
        raise RuntimeError(f"Agent manifest file set mismatch; missing={missing}, extra={extra}")
    required = {"server_control_agent.py", "agent_update_helper.py", "service_control_helper.py", "instance_runner.py", "server-control-agent.service", "server-control-minecraft@.service", "servercontrol-sudoers.example", "sc_agent/__init__.py"}
    if not required.issubset(set(files)):
        raise RuntimeError("Agent manifest does not contain every required component")
    subprocess.run(
        [sys.executable, "-m", "compileall", "-q", str(directory)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=True,
    )
    return version, release_tag


def stage(version: str) -> tuple[str, Path]:
    tag, url, expected_sha256 = resolve_release(version)
    RELEASES_ROOT.mkdir(parents=True, exist_ok=True)
    target = RELEASES_ROOT / tag.replace("/", "-")
    if target.is_dir():
        verified, manifest_tag = verify_manifest(target)
        if manifest_tag != tag:
            raise RuntimeError(f"Manifest release {manifest_tag} does not match {tag}")
        return verified, target
    # Keep staging on the same filesystem as the release root so the final
    # rename is atomic even when /tmp is a separate mount.
    with tempfile.TemporaryDirectory(prefix=".server-control-agent-update-", dir=RELEASES_ROOT) as temporary:
        temp = Path(temporary)
        archive = temp / "agent.zip"
        unpacked = temp / "unpacked"
        download(url, archive)
        if file_sha256(archive) != expected_sha256:
            raise RuntimeError("Agent archive SHA-256 does not match the release checksum")
        unpacked.mkdir()
        safe_extract(archive, unpacked)
        verified, manifest_tag = verify_manifest(unpacked)
        if manifest_tag != tag:
            raise RuntimeError(f"Manifest release {manifest_tag} does not match {tag}")
        os.replace(unpacked, target)
    return verified, target


def schedule_apply(target: Path) -> None:
    unit = f"server-control-agent-update-{int(time.time())}"
    completed = subprocess.run(
        ["systemd-run", "--unit", unit, "--on-active=3s", "/usr/local/sbin/server-control-agent-update", "--apply", str(target)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.decode("utf-8", "replace") or "systemd-run failed")


def atomic_symlink(target: Path) -> None:
    temporary = INSTALL_ROOT / f".current-{os.getpid()}"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary, CURRENT_LINK)


def atomic_copy(source: Path, destination: Path, mode: int) -> None:
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.new")
    shutil.copy2(source, temporary)
    temporary.chmod(mode)
    os.replace(temporary, destination)


def install_system_files(target: Path) -> dict[Path, bytes | None]:
    mapping = {
        Path("/etc/systemd/system/server-control-agent.service"): (target / "server-control-agent.service", 0o644),
        Path("/etc/systemd/system/server-control-minecraft@.service"): (target / "server-control-minecraft@.service", 0o644),
        Path("/etc/sudoers.d/servercontrol"): (target / "servercontrol-sudoers.example", 0o440),
        Path("/usr/local/sbin/server-control-agent-update"): (target / "agent_update_helper.py", 0o755),
        Path("/usr/local/sbin/server-control-service-control"): (target / "service_control_helper.py", 0o755),
    }
    completed = subprocess.run(
        ["visudo", "-cf", str(target / "servercontrol-sudoers.example")],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.decode("utf-8", "replace") or "sudoers validation failed")
    if subprocess.run(["getent", "group", "systemd-journal"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode == 0:
        subprocess.run(["usermod", "-a", "-G", "systemd-journal", "servercontrol"], timeout=15, check=True)
    backups: dict[Path, bytes | None] = {}
    try:
        for destination, (source, mode) in mapping.items():
            backups[destination] = destination.read_bytes() if destination.is_file() else None
            atomic_copy(source, destination, mode)
    except Exception:
        restore_system_files(backups)
        raise
    return backups


def restore_system_files(backups: dict[Path, bytes | None]) -> None:
    for path, contents in backups.items():
        if contents is None:
            path.unlink(missing_ok=True)
            continue
        temporary = path.with_name(f".{path.name}.{os.getpid()}.rollback")
        temporary.write_bytes(contents)
        temporary.chmod(0o440 if path.parent == Path("/etc/sudoers.d") else 0o755 if path.parent == Path("/usr/local/sbin") else 0o644)
        os.replace(temporary, path)


def secure_runtime_directories() -> None:
    """Repair permissions left by pre-2.0 installations before restart."""

    state = Path("/var/lib/server-control")
    backups = Path("/srv/server-control/backups")
    minecraft = Path("/opt/minecraft")
    for path in (state, backups, minecraft):
        path.mkdir(parents=True, exist_ok=True)
    shutil.chown(state, user="servercontrol", group="minecraft")
    state.chmod(0o2750)
    shutil.chown(backups, user="servercontrol", group="servercontrol")
    backups.chmod(0o750)
    shutil.chown(minecraft, user="minecraft", group="minecraft")
    minecraft.chmod(0o2770)
    for current, directories, files in os.walk(backups, followlinks=False):
        current_path = Path(current)
        if current_path.is_symlink():
            directories[:] = []
            continue
        shutil.chown(current_path, user="servercontrol", group="servercontrol")
        current_path.chmod(0o700)
        directories[:] = [name for name in directories if not (current_path / name).is_symlink()]
        for name in files:
            path = current_path / name
            if path.is_symlink():
                continue
            shutil.chown(path, user="servercontrol", group="servercontrol")
            path.chmod(0o600)
    complete_store = state / "instances.json"
    if complete_store.is_file() and not complete_store.is_symlink():
        shutil.chown(complete_store, user="servercontrol", group="servercontrol")
        complete_store.chmod(0o600)


def apply(target: Path) -> None:
    target = target.resolve(strict=True)
    target.relative_to(RELEASES_ROOT.resolve(strict=True))
    target_version, _target_tag = verify_manifest(target)
    previous = CURRENT_LINK.resolve(strict=True) if CURRENT_LINK.exists() else None
    update_started_ms = int(time.time() * 1000)
    log(f"Applying staged Agent release {target.name}")
    system_backups: dict[Path, bytes | None] = {}
    try:
        system_backups = install_system_files(target)
        secure_runtime_directories()
        atomic_symlink(target)
        subprocess.run(["systemctl", "daemon-reload"], timeout=30, check=True)
        subprocess.run(["systemctl", "restart", SERVICE], timeout=30, check=True)
    except Exception:
        if system_backups:
            restore_system_files(system_backups)
        if previous and previous.is_dir():
            atomic_symlink(previous)
        subprocess.run(["systemctl", "daemon-reload"], timeout=30, check=False)
        subprocess.run(["systemctl", "restart", SERVICE], timeout=30, check=False)
        raise
    healthy = False
    consecutive = 0
    health_path = Path("/var/lib/server-control/agent-health.json")
    for _attempt in range(45):
        time.sleep(1)
        status = subprocess.run(["systemctl", "is-active", SERVICE], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
        if status.stdout.strip() == b"active":
            try:
                marker = json.loads(health_path.read_text(encoding="utf-8"))
                marker_ok = (
                    marker.get("agent_version") == target_version
                    and int(marker.get("updated_at", 0)) >= update_started_ms
                    and marker.get("hub_sync") is True
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                marker_ok = False
            consecutive = consecutive + 1 if marker_ok else 0
            if consecutive >= 3:
                healthy = True
                break
        else:
            consecutive = 0
    if not healthy:
        log("Health check failed; rolling back")
        if previous and previous.is_dir():
            atomic_symlink(previous)
        if system_backups:
            restore_system_files(system_backups)
        subprocess.run(["systemctl", "daemon-reload"], timeout=30, check=False)
        if previous and previous.is_dir():
            subprocess.run(["systemctl", "restart", SERVICE], timeout=30, check=False)
        raise RuntimeError("Agent health check failed after update")
    log(f"Agent update {target.name} is healthy")
    releases = sorted((path for path in RELEASES_ROOT.iterdir() if path.is_dir()), key=lambda path: path.stat().st_mtime, reverse=True)
    protected = {target, previous}
    for old in releases[4:]:
        if old not in protected:
            shutil.rmtree(old, ignore_errors=True)


def main() -> int:
    if os.geteuid() != 0:
        print("This helper must run as root", file=sys.stderr)
        return 1
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default=None)
    parser.add_argument("--apply", default=None)
    args = parser.parse_args()
    try:
        if args.apply and args.version:
            raise RuntimeError("--version and --apply are mutually exclusive")
        if args.apply and len(sys.argv) != 3:
            raise RuntimeError("Invalid --apply invocation")
        if args.version and (len(sys.argv) != 3 or not (args.version == "latest" or re.fullmatch(r"v?\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", args.version))):
            raise RuntimeError("Invalid --version invocation")
        if args.apply:
            apply(Path(args.apply))
            return 0
        if not args.version:
            parser.error("--version or --apply is required")
        verified, target = stage(args.version)
        schedule_apply(target)
        log(f"Agent {verified} verified; activation scheduled with automatic rollback")
        return 0
    except (OSError, ValueError, RuntimeError, urllib.error.URLError, zipfile.BadZipFile, subprocess.SubprocessError, json.JSONDecodeError) as error:
        log(f"Update failed: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
