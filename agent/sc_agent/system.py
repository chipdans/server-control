"""Cached system, process, storage and Java inventory for the dashboard."""

from __future__ import annotations

import os
import platform
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Iterable


def _read_text(path: Path, limit: int = 1024 * 1024) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as source:
            return source.read(limit)
    except OSError:
        return ""


def _directory_size(path: Path, *, max_files: int = 250_000, max_seconds: float = 1.0) -> tuple[int, int, bool]:
    total = 0
    count = 0
    truncated = False
    if not path.exists():
        return 0, 0, False
    deadline = time.monotonic() + max(0.05, float(max_seconds))
    for current, directories, files in os.walk(path, followlinks=False):
        if time.monotonic() >= deadline:
            return total, count, True
        directories[:] = [name for name in directories if not (Path(current) / name).is_symlink()]
        for name in files:
            file_path = Path(current) / name
            if file_path.is_symlink():
                continue
            try:
                total += file_path.stat().st_size
            except OSError:
                continue
            count += 1
            if count >= max_files or time.monotonic() >= deadline:
                truncated = True
                return total, count, truncated
    return total, count, truncated


class SystemInventory:
    def __init__(self, minecraft_root: Path, backup_root: Path, *, cache_seconds: float = 2.0) -> None:
        self.minecraft_root = minecraft_root
        self.backup_root = backup_root
        self.cache_seconds = max(2.0, cache_seconds)
        self._lock = threading.Lock()
        self._snapshot: dict[str, Any] = {}
        self._snapshot_at = 0.0
        self._sizes: dict[str, Any] = {}
        self._sizes_at = 0.0
        self._java: list[dict[str, Any]] = []
        self._java_at = 0.0
        self._system: dict[str, Any] = {}
        self._system_at = 0.0
        self._services_cache: list[dict[str, Any]] = []
        self._services_at = 0.0
        self._services_key: tuple[str, ...] = ()
        self._process_ticks: dict[int, tuple[int, float]] = {}
        self._slow_refreshing = False

    def snapshot(self, instances: Iterable[Any], services: Iterable[str]) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            if self._snapshot and now - self._snapshot_at < self.cache_seconds:
                return self._snapshot
            profiles = list(instances)
            services_key = tuple(sorted(set(str(item) for item in services)))
            refresh = {
                "sizes": not self._sizes or now - self._sizes_at >= 300,
                "java": not self._java or now - self._java_at >= 600,
                "system": not self._system or now - self._system_at >= 300,
                "services": services_key != self._services_key or now - self._services_at >= 10,
            }
            # Directory walks, Java probes and systemctl are intentionally
            # detached from the two-second heartbeat. The first heartbeat can
            # contain empty slow fields; the next one receives the completed
            # cache without ever delaying Agent job polling.
            if any(refresh.values()) and not self._slow_refreshing:
                self._slow_refreshing = True
                threading.Thread(
                    target=self._refresh_slow_inventory,
                    args=(profiles, services_key, refresh),
                    name="server-control-inventory",
                    daemon=True,
                ).start()
            self._snapshot = {
                "system": self._system,
                "storage": self._storage(self._sizes),
                "processes": self._processes(profiles),
                "java": self._java,
                "services": self._services_cache,
                "categories": self._sizes,
                "collected_at": int(time.time() * 1000),
            }
            self._snapshot_at = now
            return self._snapshot

    def _refresh_slow_inventory(
        self,
        profiles: list[Any],
        services_key: tuple[str, ...],
        refresh: dict[str, bool],
    ) -> None:
        completed: dict[str, Any] = {}
        for key, function in (
            ("sizes", lambda: self._collect_sizes(profiles)),
            ("java", self._java_versions),
            ("system", self._system_info),
            ("services", lambda: self._services(services_key)),
        ):
            if not refresh.get(key):
                continue
            try:
                completed[key] = function()
            except Exception:
                # A missing utility, vanishing mount or transient /proc race
                # leaves the previous cache intact and is retried later.
                continue
        now = time.monotonic()
        with self._lock:
            if "sizes" in completed:
                self._sizes, self._sizes_at = completed["sizes"], now
            if "java" in completed:
                self._java, self._java_at = completed["java"], now
            if "system" in completed:
                self._system, self._system_at = completed["system"], now
            if "services" in completed:
                self._services_cache, self._services_at = completed["services"], now
                self._services_key = services_key
            self._slow_refreshing = False
            self._snapshot_at = 0.0

    @staticmethod
    def _system_info() -> dict[str, Any]:
        os_release: dict[str, str] = {}
        for line in _read_text(Path("/etc/os-release"), 64 * 1024).splitlines():
            key, separator, value = line.partition("=")
            if separator:
                os_release[key] = value.strip().strip('"')
        addresses: list[str] = []
        try:
            for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                address = item[4][0]
                if not address.startswith("127.") and address not in addresses:
                    addresses.append(address)
        except OSError:
            pass
        return {
            "hostname": socket.gethostname(),
            "os": os_release.get("PRETTY_NAME", platform.platform()),
            "kernel": platform.release(),
            "architecture": platform.machine(),
            "cpu_model": SystemInventory._cpu_model(),
            "cpu_count": os.cpu_count() or 1,
            "ip_addresses": addresses,
            "agent_pid": os.getpid(),
        }

    @staticmethod
    def _cpu_model() -> str:
        for line in _read_text(Path("/proc/cpuinfo"), 512 * 1024).splitlines():
            if line.lower().startswith("model name"):
                return line.partition(":")[2].strip()
        return platform.processor() or "unknown"

    @staticmethod
    def _storage(categories: dict[str, Any]) -> dict[str, Any]:
        mounts: list[dict[str, Any]] = []
        seen: set[str] = set()
        ignored = {"proc", "sysfs", "tmpfs", "devtmpfs", "cgroup", "cgroup2", "overlay", "squashfs", "tracefs", "debugfs", "securityfs", "pstore", "configfs", "fusectl", "mqueue"}
        for line in _read_text(Path("/proc/mounts"), 2 * 1024 * 1024).splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            device, mountpoint, filesystem = parts[:3]
            mountpoint = mountpoint.replace("\\040", " ")
            if filesystem in ignored or mountpoint in seen:
                continue
            seen.add(mountpoint)
            try:
                usage = shutil.disk_usage(mountpoint)
            except OSError:
                continue
            percent = (usage.used * 100 / usage.total) if usage.total else 0
            mounts.append({
                "device": device, "mountpoint": mountpoint, "filesystem": filesystem,
                "total": usage.total, "used": usage.used, "free": usage.free, "percent": round(percent, 1),
                "warning": "critical" if percent >= 90 else "warning" if percent >= 80 else "ok",
            })
        mounts.sort(key=lambda item: (item["mountpoint"] != "/", item["mountpoint"]))
        return {"mounts": mounts, "categories": categories}

    def _collect_sizes(self, profiles: list[Any]) -> dict[str, Any]:
        instances: list[dict[str, Any]] = []
        total_instances = 0
        for profile in profiles:
            size, files, truncated = _directory_size(Path(profile.directory))
            instances.append({"instance_id": profile.id, "name": profile.name, "bytes": size, "files": files, "truncated": truncated})
            total_instances += size
        backups, backup_files, backup_truncated = _directory_size(self.backup_root)
        logs = 0
        for profile in profiles:
            log_path = Path(profile.log_file) if profile.log_file else Path(profile.directory) / "logs"
            directory = log_path if log_path.is_dir() else log_path.parent
            size, _files, _truncated = _directory_size(directory, max_files=100_000, max_seconds=0.5)
            logs += size
        return {
            "instances": instances, "minecraft_total": total_instances,
            "backups": backups, "backup_files": backup_files, "backup_truncated": backup_truncated,
            "logs": logs,
        }

    def _processes(self, profiles: list[Any]) -> list[dict[str, Any]]:
        now = time.monotonic()
        clock_ticks = max(1, os.sysconf("SC_CLK_TCK"))
        page_size = max(1, os.sysconf("SC_PAGE_SIZE"))
        try:
            system_uptime = float(_read_text(Path("/proc/uptime"), 128).split()[0])
        except (ValueError, IndexError):
            system_uptime = 0.0
        result: list[dict[str, Any]] = []
        current_ticks: dict[int, tuple[int, float]] = {}
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            try:
                stat_parts = (entry / "stat").read_text(encoding="ascii").split()
                process_ticks = int(stat_parts[13]) + int(stat_parts[14])
                process_started = int(stat_parts[21]) / clock_ticks
                rss = int(stat_parts[23]) * page_size
                command_line = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
                name = (entry / "comm").read_text(encoding="utf-8", errors="replace").strip()
            except (OSError, ValueError, IndexError):
                continue
            previous = self._process_ticks.get(pid)
            cpu = 0.0
            if previous and now > previous[1]:
                cpu = max(0.0, (process_ticks - previous[0]) / clock_ticks / (now - previous[1]) * 100)
            current_ticks[pid] = (process_ticks, now)
            instance_id = None
            for profile in profiles:
                if profile.directory and profile.directory in command_line:
                    instance_id = profile.id
                    break
                try:
                    cwd = (entry / "cwd").resolve(strict=True)
                    cwd.relative_to(Path(profile.directory).resolve(strict=True))
                except (OSError, ValueError):
                    continue
                instance_id = profile.id
                break
            result.append({
                "pid": pid, "name": name, "command": command_line[:500], "cpu_percent": round(cpu, 1),
                "memory_bytes": rss, "runtime_seconds": max(0, round(system_uptime - process_started)) if system_uptime else None,
                "instance_id": instance_id, "is_minecraft": bool(instance_id) or "minecraft" in command_line.lower(),
            })
        self._process_ticks = current_ticks
        result.sort(key=lambda item: (not item["is_minecraft"], -item["cpu_percent"], -item["memory_bytes"]))
        return result[:100]

    @staticmethod
    def _java_versions() -> list[dict[str, Any]]:
        candidates: set[Path] = {Path("/usr/bin/java")}
        jvm_root = Path("/usr/lib/jvm")
        if jvm_root.is_dir():
            candidates.update(jvm_root.glob("*/bin/java"))
        result: list[dict[str, Any]] = []
        seen: set[Path] = set()
        for candidate in sorted(candidates):
            try:
                resolved = candidate.resolve(strict=True)
                if resolved in seen or not os.access(resolved, os.X_OK):
                    continue
                seen.add(resolved)
                completed = subprocess.run(
                    [str(resolved), "-version"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, timeout=5, check=False, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                )
                output = completed.stdout.decode("utf-8", "replace")[:2000]
                match = re.search(r'version\s+"([^"]+)', output)
                version = match.group(1) if match else output.splitlines()[0] if output else "unknown"
                major_match = re.match(r"(?:1\.)?(\d+)", version)
                result.append({"path": str(resolved), "version": version, "major": int(major_match.group(1)) if major_match else None, "vendor": output.splitlines()[0] if output else "unknown"})
            except (OSError, subprocess.SubprocessError):
                continue
        return result

    @staticmethod
    def _services(names: Iterable[str]) -> list[dict[str, Any]]:
        selected = sorted({str(value) for value in names if re.fullmatch(r"[A-Za-z0-9@_.:-]{1,128}\.service", str(value))})[:100]
        if not selected:
            return []
        by_name: dict[str, dict[str, Any]] = {}
        try:
            completed = subprocess.run(
                ["systemctl", "show", *selected, "--property=Id,Description,ActiveState,SubState,MainPID", "--no-pager"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=8, check=False,
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            )
            for block in completed.stdout.decode("utf-8", "replace").split("\n\n"):
                properties: dict[str, str] = {}
                for line in block.splitlines():
                    key, separator, value = line.partition("=")
                    if separator:
                        properties[key] = value
                name = properties.get("Id", "")
                if name in selected:
                    try:
                        pid = int(properties.get("MainPID", "0") or 0)
                    except ValueError:
                        pid = 0
                    by_name[name] = {
                        "name": name, "description": properties.get("Description", name),
                        "active": properties.get("ActiveState", "unknown"),
                        "sub_state": properties.get("SubState", "unknown"), "pid": pid,
                    }
        except (OSError, subprocess.SubprocessError):
            pass
        return [by_name.get(name, {"name": name, "description": name, "active": "unknown", "sub_state": "unknown", "pid": 0}) for name in selected]
