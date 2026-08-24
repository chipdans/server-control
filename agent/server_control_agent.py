#!/usr/bin/env python3
"""Server Control agent.

The agent is deliberately dependency-free: it uses only the Python standard
library and communicates outward to the Cloudflare Worker over HTTPS. It never
opens a network port on the home server.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import http.client
import json
import os
import re
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sc_agent import BackupManager, InstanceStore, JobExecutor, SystemInventory
from sc_agent.instances import InstanceProfile
from sc_agent.security import atomic_write_bytes, secure_path_within, validate_instance_id


AGENT_VERSION = "2.0.4"
PROTOCOL_VERSION = 2
MAX_EVENT_MESSAGE = 8000
MAX_EVENT_BUFFER_EVENTS = 2_000
MAX_EVENT_BUFFER_BYTES = 2 * 1024 * 1024
MAX_SUBPROCESS_OUTPUT_BYTES = 128 * 1024
RCON_STATUS_INTERVAL_SECONDS = 2.0
COMMAND_LIST_REFRESH_SECONDS = 15 * 60
PERFORMANCE_REFRESH_SECONDS = 30.0
PLAYER_LIST_REFRESH_SECONDS = 10.0
HUB_RETRY_MAX_SECONDS = 10.0
HUB_ERROR_LOG_INTERVAL_SECONDS = 60.0
MAX_HUB_JSON_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_TRANSFER_BYTES = 50 * 1024 * 1024 * 1024
MAX_INITIAL_LOG_TAIL_BYTES = 2 * 1024 * 1024
MAX_LOG_READ_BYTES = 128 * 1024
MAX_PARTIAL_LOG_LINE_BYTES = 64 * 1024


def is_rcon_lifecycle_log(message: str) -> bool:
    """Hide connection bookkeeping produced by Minecraft's local RCON server."""

    lowered = message.casefold()
    subject = any(marker in lowered for marker in ("rcon client", "rcon listener", "rcon connection"))
    lifecycle = any(marker in lowered for marker in (" started", " shutting down", " stopped", " disconnected", " connection closed", " accepted connection"))
    return subject and lifecycle


class HubError(RuntimeError):
    """A request to the control hub failed."""


@dataclass
class Config:
    hub_url: str
    agent_api_key: str
    poll_seconds: float
    heartbeat_seconds: int
    request_timeout_seconds: int
    minecraft: dict[str, Any]
    commands: dict[str, Any]
    # Direct construction is used by unit tests. Config.load supplies the
    # production paths below, while these defaults stay writable and isolated.
    state_directory: str = "/tmp/server-control-agent-test/state"
    minecraft_root: str = "/tmp/server-control-agent-test/minecraft"
    backup_root: str = "/tmp/server-control-agent-test/backups"
    max_job_workers: int = 2
    allowed_services: tuple[str, ...] = ("server-control-agent.service",)
    agent_update_command: tuple[str, ...] = ("sudo", "-n", "/usr/local/sbin/server-control-agent-update")

    @classmethod
    def load(cls, path: Path) -> "Config":
        raw = json.loads(path.read_text(encoding="utf-8"))
        required = ("hub_url", "agent_api_key", "minecraft", "commands")
        missing = [key for key in required if not raw.get(key)]
        if missing:
            raise ValueError(f"В конфигурации отсутствуют: {', '.join(missing)}")
        return cls(
            hub_url=str(raw["hub_url"]).rstrip("/"),
            agent_api_key=str(raw["agent_api_key"]),
            # Cloudflare Workers Free permits 100,000 requests per day.  Keep
            # polling responsive without allowing an old config to force the
            # Agent alone above that account-wide quota.
            poll_seconds=max(3.0, float(raw.get("poll_seconds", 3))),
            heartbeat_seconds=max(15, int(raw.get("heartbeat_seconds", 15))),
            request_timeout_seconds=max(5, int(raw.get("request_timeout_seconds", 20))),
            minecraft=dict(raw["minecraft"]),
            commands=dict(raw["commands"]),
            state_directory=str(raw.get("state_directory", "/var/lib/server-control")),
            minecraft_root=str(raw.get("minecraft_root", "/opt/minecraft")),
            backup_root=str(raw.get("backup_root", "/srv/server-control/backups")),
            max_job_workers=max(1, min(4, int(raw.get("max_job_workers", 2)))),
            allowed_services=tuple(
                str(item) for item in raw.get("allowed_services", ["server-control-agent.service"])
                if isinstance(item, str) and re.fullmatch(r"[A-Za-z0-9@_.:-]{1,128}\.service", item)
            ),
            agent_update_command=tuple(
                str(item) for item in raw.get("agent_update_command", ["sudo", "-n", "/usr/local/sbin/server-control-agent-update"])
                if isinstance(item, str) and item
            ),
        )


class HubClient:
    def __init__(self, config: Config) -> None:
        self.base_url = config.hub_url
        self.api_key = config.agent_api_key
        self.timeout = config.request_timeout_seconds
        parsed = urllib.parse.urlsplit(self.base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("hub_url должен быть корректным адресом HTTPS")
        self.host = parsed.hostname
        self.port = parsed.port or 443
        self.base_path = parsed.path.rstrip("/")

    @staticmethod
    def _create_ipv4_connection(
        address: tuple[str, int],
        timeout: float | object = socket._GLOBAL_DEFAULT_TIMEOUT,
        source_address: tuple[str, int] | None = None,
    ) -> socket.socket:
        """Create a TLS transport socket without a broken IPv6 detour."""

        host, port = address
        last_error: OSError | None = None
        for family, socktype, protocol, _name, sockaddr in socket.getaddrinfo(
            host, port, socket.AF_INET, socket.SOCK_STREAM
        ):
            sock = socket.socket(family, socktype, protocol)
            try:
                if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                    sock.settimeout(float(timeout))
                if source_address:
                    sock.bind(source_address)
                sock.connect(sockaddr)
                return sock
            except OSError as error:
                last_error = error
                sock.close()
        raise last_error or OSError(f"IPv4 address not found for {host}")

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        original_body = None if payload is None else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        body = gzip.compress(original_body, compresslevel=6) if original_body is not None and len(original_body) >= 1024 else original_body
        headers = {
            "X-Agent-Key": self.api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": f"ServerControlAgent/{AGENT_VERSION}",
            "Connection": "close",
        }
        if body is not None:
            headers["Content-Length"] = str(len(body))
        if original_body is not None and body is not original_body:
            headers["Content-Encoding"] = "gzip"
            headers["X-Uncompressed-Length"] = str(len(original_body))
        connection = http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout)
        connection._create_connection = self._create_ipv4_connection  # type: ignore[method-assign]
        try:
            connection.request(method, f"{self.base_path}{path}", body=body, headers=headers)
            response = connection.getresponse()
            try:
                raw = response.read(MAX_HUB_JSON_RESPONSE_BYTES + 1)
            finally:
                response.close()
        except (OSError, TimeoutError, http.client.HTTPException) as error:
            sizes = f"; request={len(original_body or b'')} B, wire={len(body or b'')} B"
            raise HubError(f"Hub unavailable over IPv4: {error}{sizes}") from error
        finally:
            connection.close()

        if len(raw) > MAX_HUB_JSON_RESPONSE_BYTES:
            raise HubError("Hub returned an oversized JSON response")
        response_data = raw.decode("utf-8", "replace")
        if response.status >= 400:
            raise HubError(f"Hub returned HTTP {response.status}: {response_data[:500]}")

        try:
            parsed = json.loads(response_data)
        except json.JSONDecodeError as error:
            raise HubError("Hub returned invalid JSON") from error
        if not isinstance(parsed, dict):
            raise HubError("Hub returned an unexpected response")
        return parsed

    def heartbeat(self, server: dict[str, Any], minecraft: dict[str, Any], **extra: Any) -> None:
        self.request(
            "POST",
            "/v1/agent/heartbeat",
            {
                "agent_version": AGENT_VERSION,
                "protocol_version": PROTOCOL_VERSION,
                "server": server,
                "minecraft": minecraft,
                **extra,
            },
        )

    def push_events(self, events: list[dict[str, str]]) -> None:
        if events:
            self.request("POST", "/v1/agent/events", {"events": events})

    def get_commands(self) -> list[dict[str, Any]]:
        response = self.request("GET", "/v1/agent/commands")
        commands = response.get("commands", [])
        return commands if isinstance(commands, list) else []

    def complete_command(self, command_id: str, status: str, result: dict[str, Any]) -> None:
        self.request(
            "POST",
            f"/v1/agent/commands/{urllib.parse.quote(command_id, safe='')}/result",
            {"status": status, "result": result},
        )

    def get_jobs(self) -> dict[str, Any]:
        return self.request("GET", "/v1/agent/jobs")

    def sync_work(self) -> dict[str, Any]:
        """Claim legacy commands and version-2 jobs with one idle request."""
        try:
            return self.request("GET", "/v1/agent/sync")
        except HubError as error:
            # Rolling deployments may briefly run Agent 2 against the previous
            # Worker. Preserve compatibility, but do not hide real outages.
            if "HTTP 404" not in str(error):
                raise
            return {"commands": self.get_commands(), **self.get_jobs()}

    def job_progress(self, job_id: str, progress: int, stage: str, message: str) -> bool:
        response = self.request(
            "POST",
            f"/v1/agent/jobs/{urllib.parse.quote(job_id, safe='')}/progress",
            {"progress": progress, "stage": stage, "message": message},
        )
        return bool(response.get("cancel_requested"))

    def complete_job(
        self,
        job_id: str,
        status: str,
        result: dict[str, Any],
        message: str,
        error_code: str | None = None,
    ) -> None:
        self.request(
            "POST",
            f"/v1/agent/jobs/{urllib.parse.quote(job_id, safe='')}/result",
            {"status": status, "result": result, "message": message, "error_code": error_code},
        )

    def _binary_request(self, method: str, path: str, data: bytes, *, timeout: int) -> bytes:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={
                "X-Agent-Key": self.api_key,
                "Accept": "application/json",
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(data)),
                "User-Agent": f"ServerControlAgent/{AGENT_VERSION}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                value = response.read(1024 * 1024 + 1)
                if len(value) > 1024 * 1024:
                    raise HubError("Hub returned an oversized transfer response")
                return value
        except urllib.error.HTTPError as error:
            details = error.read(4096).decode("utf-8", "replace")
            raise HubError(f"Hub returned HTTP {error.code}: {details[:500]}") from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise HubError(f"Ошибка передачи файла: {error}") from error

    def download_transfer(self, transfer_id: str, destination: Path, progress: Any, cancelled: Any) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        last_error: Exception | None = None
        for attempt in range(3):
            if cancelled():
                destination.unlink(missing_ok=True)
                raise InterruptedError("Передача отменена")
            offset = destination.stat().st_size if destination.is_file() else 0
            headers = {
                "X-Agent-Key": self.api_key,
                "Accept": "application/octet-stream",
                "User-Agent": f"ServerControlAgent/{AGENT_VERSION}",
            }
            if offset:
                headers["Range"] = f"bytes={offset}-"
            request = urllib.request.Request(
                f"{self.base_url}/v1/agent/transfers/{urllib.parse.quote(transfer_id, safe='')}/content",
                method="GET",
                headers=headers,
            )
            try:
                with urllib.request.urlopen(request, timeout=max(30, self.timeout)) as response:
                    if offset and getattr(response, "status", 200) != 206:
                        destination.unlink(missing_ok=True)
                        offset = 0
                    remaining = int(response.headers.get("content-length", 0) or 0)
                    total = offset + remaining
                    if total > MAX_TRANSFER_BYTES:
                        raise OSError("Передача превышает лимит 50 ГиБ")
                    if remaining and shutil.disk_usage(destination.parent).free < remaining + 128 * 1024 * 1024:
                        raise OSError("Недостаточно свободного места для передачи")
                    received = offset
                    # A failed first attempt may leave an empty/partial file.
                    # Truncate on a non-resumed retry instead of requiring the
                    # path to be brand new again.
                    with destination.open("ab" if offset else "wb") as output:
                        while True:
                            if cancelled():
                                raise InterruptedError("Передача отменена")
                            chunk = response.read(1024 * 1024)
                            if not chunk:
                                break
                            output.write(chunk)
                            received += len(chunk)
                            if received > MAX_TRANSFER_BYTES:
                                raise OSError("Передача превышает лимит 50 ГиБ")
                            if total:
                                progress(min(100, int(received * 100 / total)))
                        output.flush()
                        os.fsync(output.fileno())
                return
            except InterruptedError:
                destination.unlink(missing_ok=True)
                raise
            except (OSError, TimeoutError, urllib.error.URLError, urllib.error.HTTPError) as error:
                last_error = error
                if attempt < 2:
                    time.sleep(2 ** attempt)
        destination.unlink(missing_ok=True)
        raise HubError(f"Передача не завершена после трёх попыток: {last_error}")

    def upload_transfer(self, transfer_id: str, source: Path, sha256: str, progress: Any, cancelled: Any) -> None:
        total = max(1, source.stat().st_size)
        sent = 0
        part_number = 1
        with source.open("rb") as input_file:
            while True:
                if cancelled():
                    raise InterruptedError("Передача отменена")
                chunk = input_file.read(8 * 1024 * 1024)
                if not chunk:
                    break
                for attempt in range(3):
                    try:
                        self._binary_request(
                            "PUT",
                            f"/v1/agent/transfers/{urllib.parse.quote(transfer_id, safe='')}/parts/{part_number}",
                            chunk,
                            timeout=max(60, self.timeout),
                        )
                        break
                    except HubError:
                        if attempt >= 2:
                            raise
                        if cancelled():
                            raise InterruptedError("Передача отменена")
                        time.sleep(2 ** attempt)
                part_number += 1
                sent += len(chunk)
                progress(min(99, int(sent * 100 / total)))
        self.request(
            "POST",
            f"/v1/agent/transfers/{urllib.parse.quote(transfer_id, safe='')}/complete",
            {"sha256": sha256, "size_bytes": source.stat().st_size},
        )
        progress(100)


def parse_proc_stat_cpu(text: str) -> tuple[int, int] | None:
    """Return total and idle CPU ticks from a Linux ``/proc/stat`` sample."""

    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0] != "cpu":
            continue
        try:
            values = [int(value) for value in parts[1:]]
        except ValueError:
            return None
        if len(values) < 4:
            return None
        total = sum(values)
        # Linux reports iowait separately, but it is still time when the CPU
        # is not doing useful work, so include it in the idle side.
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return total, idle
    return None


def parse_meminfo(text: str) -> dict[str, int]:
    """Parse the useful memory counters from Linux ``/proc/meminfo``."""

    raw: dict[str, int] = {}
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if not separator:
            continue
        match = re.search(r"(\d+)", value)
        if match:
            raw[key] = int(match.group(1)) * 1024
    total = raw.get("MemTotal", 0)
    available = raw.get("MemAvailable", raw.get("MemFree", 0) + raw.get("Buffers", 0) + raw.get("Cached", 0))
    used = max(0, total - available)
    return {
        "total_bytes": total,
        "available_bytes": available,
        "used_bytes": used,
        "swap_total_bytes": raw.get("SwapTotal", 0),
        "swap_free_bytes": raw.get("SwapFree", 0),
    }


def parse_proc_net_dev(text: str) -> tuple[int, int]:
    """Return aggregate non-loopback receive/transmit bytes."""

    received = 0
    transmitted = 0
    for line in text.splitlines():
        interface, separator, payload = line.partition(":")
        if not separator or interface.strip() == "lo":
            continue
        fields = payload.split()
        if len(fields) < 9:
            continue
        try:
            received += int(fields[0])
            transmitted += int(fields[8])
        except ValueError:
            continue
    return received, transmitted


def parse_proc_diskstats(text: str) -> tuple[int, int]:
    """Return aggregate physical-disk read/write bytes from ``/proc/diskstats``.

    Loop devices, device-mapper volumes and partitions are skipped to avoid
    double-counting.  The root filesystem usage below remains available even
    on hosts where the underlying device is not visible in this list.
    """

    read_sectors = 0
    written_sectors = 0
    physical_name = re.compile(r"(?:sd|vd|xvd)[a-z]+$|nvme\d+n\d+$|mmcblk\d+$")
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 10 or not physical_name.fullmatch(fields[2]):
            continue
        try:
            read_sectors += int(fields[5])
            written_sectors += int(fields[9])
        except ValueError:
            continue
    return read_sectors * 512, written_sectors * 512


class SystemMonitor:
    """Collect small, dependency-free Linux resource snapshots for the UI."""

    def __init__(self) -> None:
        self._last_cpu: tuple[int, int] | None = None
        self._last_network: tuple[int, int, float] | None = None
        self._last_disk_io: tuple[int, int, float] | None = None

    @staticmethod
    def _read_text(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    @staticmethod
    def _rate(previous: tuple[int, int, float] | None, current: tuple[int, int], now: float) -> tuple[float | None, float | None]:
        if previous is None:
            return None, None
        old_first, old_second, old_time = previous
        elapsed = now - old_time
        if elapsed <= 0:
            return None, None
        return max(0.0, (current[0] - old_first) / elapsed), max(0.0, (current[1] - old_second) / elapsed)

    @staticmethod
    def _temperature_celsius() -> float | None:
        for path in sorted(Path("/sys/class/thermal").glob("thermal_zone*/temp")):
            try:
                raw = float(path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                continue
            value = raw / 1000 if raw > 1_000 else raw
            if -20 <= value <= 150:
                return round(value, 1)
        return None

    def sample(self) -> dict[str, Any]:
        now = time.monotonic()
        cpu_ticks = parse_proc_stat_cpu(self._read_text(Path("/proc/stat")))
        cpu_percent: float | None = None
        if cpu_ticks and self._last_cpu:
            total_delta = cpu_ticks[0] - self._last_cpu[0]
            idle_delta = cpu_ticks[1] - self._last_cpu[1]
            if total_delta > 0:
                cpu_percent = round(max(0.0, min(100.0, 100.0 * (total_delta - idle_delta) / total_delta)), 1)
        if cpu_ticks:
            self._last_cpu = cpu_ticks

        memory = parse_meminfo(self._read_text(Path("/proc/meminfo")))
        memory_total = memory.get("total_bytes", 0)
        memory["percent"] = round((memory.get("used_bytes", 0) * 100 / memory_total), 1) if memory_total else None

        try:
            filesystem = os.statvfs("/")
            total_bytes = filesystem.f_blocks * filesystem.f_frsize
            available_bytes = filesystem.f_bavail * filesystem.f_frsize
            used_bytes = max(0, total_bytes - available_bytes)
            disk = {
                "mount": "/",
                "total_bytes": total_bytes,
                "available_bytes": available_bytes,
                "used_bytes": used_bytes,
                "percent": round(used_bytes * 100 / total_bytes, 1) if total_bytes else None,
            }
        except OSError:
            disk = {}

        network = parse_proc_net_dev(self._read_text(Path("/proc/net/dev")))
        rx_per_second, tx_per_second = self._rate(self._last_network, network, now)
        self._last_network = (*network, now)

        disk_io = parse_proc_diskstats(self._read_text(Path("/proc/diskstats")))
        read_per_second, write_per_second = self._rate(self._last_disk_io, disk_io, now)
        self._last_disk_io = (*disk_io, now)

        try:
            uptime_seconds = max(0, int(float(self._read_text(Path("/proc/uptime")).split()[0])))
        except (IndexError, ValueError):
            uptime_seconds = None
        try:
            load_average = [round(float(value), 2) for value in os.getloadavg()]
        except OSError:
            load_average = []

        return {
            "cpu": {"percent": cpu_percent, "load_average": load_average},
            "memory": memory,
            "filesystem": disk,
            "network": {"rx_bytes": network[0], "tx_bytes": network[1], "rx_per_second": rx_per_second, "tx_per_second": tx_per_second},
            "disk_io": {"read_bytes": disk_io[0], "write_bytes": disk_io[1], "read_per_second": read_per_second, "write_per_second": write_per_second},
            "temperature_celsius": self._temperature_celsius(),
            "uptime_seconds": uptime_seconds,
            "collected_at": int(time.time() * 1000),
        }


class EventBuffer:
    """A bounded outage buffer so an unavailable hub cannot consume all RAM."""

    def __init__(self, max_events: int = MAX_EVENT_BUFFER_EVENTS, max_bytes: int = MAX_EVENT_BUFFER_BYTES) -> None:
        self._events: deque[dict[str, Any]] = deque()
        self._max_events = max(1, max_events)
        self._max_bytes = max(256, max_bytes)
        self._size_bytes = 0
        self._dropped_events = 0
        self._lock = threading.RLock()

    @staticmethod
    def _event_size(event: dict[str, Any]) -> int:
        return 64 + len(json.dumps(event, ensure_ascii=False).encode("utf-8"))

    def _drop_oldest(self) -> None:
        event = self._events.popleft()
        self._size_bytes -= self._event_size(event)
        self._dropped_events += 1

    def _make_room(self, size: int, *, from_left: bool) -> None:
        while self._events and (len(self._events) >= self._max_events or self._size_bytes + size > self._max_bytes):
            if from_left:
                self._drop_oldest()
            else:
                event = self._events.pop()
                self._size_bytes -= self._event_size(event)
                self._dropped_events += 1

    def add(
        self,
        kind: str,
        message: str,
        *,
        instance_id: str | None = None,
        source: str | None = None,
        level: str | None = None,
    ) -> None:
        clean = message.strip()
        if not clean:
            return
        clean = clean[:MAX_EVENT_MESSAGE]
        max_message_bytes = max(1, self._max_bytes - 96)
        encoded = clean.encode("utf-8")
        if len(encoded) > max_message_bytes:
            clean = encoded[:max_message_bytes].decode("utf-8", "ignore")
        event: dict[str, Any] = {"kind": kind, "message": clean}
        if instance_id:
            event["instance_id"] = instance_id
        if source:
            event["source"] = str(source)[:64]
        if level:
            event["level"] = str(level).upper()[:8]
        with self._lock:
            size = self._event_size(event)
            self._make_room(size, from_left=True)
            self._events.append(event)
            self._size_bytes += size

    def take(self, count: int = 100) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        with self._lock:
            while self._events and len(result) < count:
                event = self._events.popleft()
                self._size_bytes -= self._event_size(event)
                result.append(event)
        return result

    def restore_front(self, events: list[dict[str, Any]]) -> None:
        with self._lock:
            for event in reversed(events):
                if not isinstance(event, dict) or not isinstance(event.get("kind"), str) or not isinstance(event.get("message"), str):
                    continue
                size = self._event_size(event)
                self._make_room(size, from_left=False)
                self._events.appendleft(event)
                self._size_bytes += size

    def take_overflow_notice(self) -> dict[str, str] | None:
        with self._lock:
            if not self._dropped_events:
                return None
            dropped = self._dropped_events
            self._dropped_events = 0
            return {
                "kind": "server",
                "message": f"[agent] Буфер событий был переполнен: пропущено строк: {dropped}. Проверьте доступность Control Hub.",
            }

    def __bool__(self) -> bool:
        with self._lock:
            return bool(self._events) or self._dropped_events > 0


class LogTail:
    """Reads appended Minecraft log lines without keeping the file open."""

    def __init__(self, path: Path, initial_lines: int = 100) -> None:
        self.path = path
        self.position = 0
        self.initial_lines = initial_lines
        self.initialized = False
        self._partial = b""

    def read_new_lines(self) -> list[str]:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return []

        if not self.initialized:
            self.initialized = True
            start = max(0, size - MAX_INITIAL_LOG_TAIL_BYTES)
            with self.path.open("rb") as handle:
                handle.seek(start)
                data = handle.read(MAX_INITIAL_LOG_TAIL_BYTES)
            self.position = start + len(data)
            if start:
                _discarded, separator, data = data.partition(b"\n")
                if not separator:
                    data = b""
            lines = data.decode("utf-8", "replace").splitlines()
            return [line.rstrip() for line in lines[-self.initial_lines :] if line.strip()]

        if size < self.position:  # log rotation or truncation
            self.position = 0
            self._partial = b""
        with self.path.open("rb") as handle:
            handle.seek(self.position)
            chunk = handle.read(MAX_LOG_READ_BYTES)
        self.position += len(chunk)
        if not chunk:
            return []
        data = self._partial + chunk
        parts = data.split(b"\n")
        if data.endswith(b"\n"):
            complete, self._partial = parts[:-1], b""
        else:
            complete, self._partial = parts[:-1], parts[-1][-MAX_PARTIAL_LOG_LINE_BYTES:]
        return [
            line.decode("utf-8", "replace").rstrip("\r")
            for line in complete
            if line.strip()
        ]


class MinecraftStartupTracker:
    """Turns the noisy Forge/Minecraft startup log into a small useful state.

    Forge and modpacks do not expose a universal numeric startup percentage.
    The stages below are deliberately conservative: a percentage only advances
    when a real log marker is observed, and RCON confirms the final ``ready``
    state.  This works for Dragonfyre and remains understandable for a future
    Forge 1.20.1 pack.
    """

    _SPAWN_PERCENT = re.compile(r"preparing (?:spawn area|start region).*?(\d{1,3})%", re.IGNORECASE)

    def __init__(self) -> None:
        self._service_active = False
        self.phase = "stopped"
        self.progress = 0
        self.label = "Сервер остановлен"
        self.detail = "Minecraft-служба не запущена"
        self.ready = False
        self.started_at: int | None = None
        self.last_log_at: int | None = None
        self.expected_stop = False
        self.crash: dict[str, Any] | None = None

    def set_service_active(self, active: bool) -> None:
        if active and not self._service_active:
            self.phase = "starting_java"
            self.progress = 3
            self.label = "Запускаю Java и Forge"
            self.detail = "Ожидаю первые строки запуска"
            self.ready = False
            self.started_at = int(time.time() * 1000)
            self.expected_stop = False
            self.crash = None
        elif not active:
            unexpected = self._service_active and not self.expected_stop and self.phase not in {"stopping", "stopped"}
            if self.phase in {"failed", "crashed"} or unexpected:
                if not self.crash:
                    self.crash = {
                        "code": "unexpected_exit",
                        "summary": "Процесс Minecraft неожиданно завершился",
                        "solution": "Откройте crash-report и последние строки журнала.",
                    }
                self.phase = "crashed"
                self.progress = min(self.progress, 99)
                self.label = "Сервер завершился с ошибкой"
                self.detail = str(self.crash.get("summary", "Неожиданное завершение"))
            else:
                self.phase = "stopped"
                self.progress = 0
                self.label = "Сервер остановлен"
                self.detail = "Minecraft-служба не запущена"
                self.crash = None
            self.ready = False
            self.started_at = None
            self.expected_stop = False
        self._service_active = active

    def expect_service_stop(self) -> None:
        self.expected_stop = True
        self._set("stopping", self.progress, "Останавливаю Minecraft", "Жду корректного завершения процесса", force=True)

    def observe(self, line: str) -> None:
        self.last_log_at = int(time.time() * 1000)
        text = line.lower()

        if "done (" in text and "for help, type \"help\"" in text:
            self.mark_ready("Minecraft полностью запущен")
            return
        if "for help, type \"help\"" in text:
            self.mark_ready("Minecraft полностью запущен")
            return
        crash = self._classify_crash(line)
        if crash:
            self.crash = crash
            self._set("failed", self.progress, "Ошибка запуска", crash["summary"], force=True)
            self.ready = False
            return
        if any(marker in text for marker in ("stopping server", "stopping the server", "server shutting down")):
            self._set("stopping", self.progress, "Останавливаю Minecraft", "Жду завершения службы", force=True)
            self.ready = False
            self.expected_stop = True
            return

        # Forge bootstrapping and mod discovery.
        if any(marker in text for marker in ("modlauncher running", "modlauncher", "loading minecraft", "fml loader")):
            self._set("starting_java", 10, "Запускаю Java и Forge", "Forge подготавливает загрузчик")
            return
        if any(marker in text for marker in ("found mod file", "moddiscoverer", "found valid mod file", "loading mod list")):
            self._set("loading_mods", 28, "Сканирую моды", "Forge находит моды сборки")
            return
        if any(marker in text for marker in ("constructing mods", "loading mod", "common_setup", "modloading")):
            self._set("initializing_mods", 50, "Инициализирую моды", "Загружаю рецепты, механики и зависимости")
            return
        if any(marker in text for marker in ("gamedata", "registries", "registering", "registry")):
            self._set("registering_content", 68, "Регистрирую содержимое", "Подготавливаю блоки, предметы и рецепты")
            return

        # Vanilla dedicated-server world startup.
        if "preparing level" in text or "loading level" in text:
            self._set("loading_world", 78, "Загружаю мир", "Открываю сохранение и измерения")
            return
        spawn_match = self._SPAWN_PERCENT.search(line)
        if spawn_match:
            percent = max(0, min(100, int(spawn_match.group(1))))
            self._set(
                "preparing_spawn",
                84 + round(percent * 0.14),
                "Подготавливаю спавн",
                f"Подготовка стартовой области: {percent}%",
            )
            return
        if any(marker in text for marker in ("starting rcon", "rcon running", "starting minecraft server version", "starting minecraft server")):
            self._set("starting_services", 97, "Запускаю службы Minecraft", "Открываю сеть и RCON-консоль")

    def wait_for_rcon(self) -> None:
        if not self.ready and self._service_active:
            self._set("starting_services", max(97, self.progress), "Запускаю службы Minecraft", "Жду готовности RCON-консоли")

    def mark_ready(self, detail: str) -> None:
        self._set("ready", 100, "Сервер готов", detail, force=True)
        self.ready = True
        self.crash = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "progress": self.progress,
            "label": self.label,
            "detail": self.detail,
            "ready": self.ready,
            "started_at": self.started_at,
            "last_log_at": self.last_log_at,
            "state": "RUNNING" if self.ready else "CRASHED" if self.phase in {"failed", "crashed"} else "STOPPING" if self.phase == "stopping" else "STARTING" if self._service_active else "OFFLINE",
            "crash": self.crash,
        }

    @staticmethod
    def _classify_crash(line: str) -> dict[str, str] | None:
        text = line.casefold()
        patterns = [
            (("outofmemoryerror", "java heap space", "gc overhead limit"), "out_of_memory", "Minecraft не хватило оперативной памяти", "Увеличьте RAM MAX или уменьшите число тяжёлых модов."),
            (("unsupportedclassversionerror", "class file version"), "wrong_java", "Выбрана несовместимая версия Java", "Назначьте сборке подходящую Java в настройках ресурсов."),
            (("missing mandatory dependencies", "missing mods", "requires version", "mod resolution encountered"), "missing_dependency", "Отсутствует мод или обязательная зависимость", "Откройте подробный лог и установите указанную зависимость нужной версии."),
            (("address already in use", "failed to bind to port"), "port_in_use", "Порт Minecraft уже занят", "Остановите другой сервер или измените server-port."),
            (("failed to load level.dat", "corrupted", "chunk file at"), "corrupted_world", "Возможное повреждение мира", "Не запускайте сервер повторно; восстановите последнюю исправную резервную копию."),
            (("permission denied", "accessdeniedexception"), "permission_denied", "Minecraft не хватает прав на файл", "Проверьте владельца и права директории сборки."),
            (("no such file", "filenotfoundexception"), "missing_file", "Не найден необходимый файл", "Откройте ошибку и восстановите указанный файл или мод."),
            (("exception in server tick loop", "crash report", "failed to start", "fatal", "could not load"), "minecraft_error", "Minecraft сообщил о критической ошибке", "Откройте crash-report и последние строки журнала."),
        ]
        for markers, code, summary, solution in patterns:
            if any(marker in text for marker in markers):
                return {"code": code, "summary": summary, "solution": solution, "evidence": line[-500:]}
        return None

    def _set(self, phase: str, progress: int, label: str, detail: str, *, force: bool = False) -> None:
        if self.ready and not force:
            return
        if not force and progress < self.progress:
            return
        self.phase = phase
        self.progress = max(0, min(100, int(progress)))
        self.label = label
        self.detail = detail[:240]


def parse_minecraft_player_list(output: str) -> tuple[int | None, int | None, list[str]]:
    """Parse the standard Java-server answer to the RCON ``list`` command."""

    match = re.search(r"There are\s+(\d+)\s+of a max of\s+(\d+)\s+players online:\s*(.*)", output, re.IGNORECASE | re.DOTALL)
    if not match:
        return None, None, []
    names = [name.strip() for name in match.group(3).replace("\n", " ").split(",") if name.strip()]
    return int(match.group(1)), int(match.group(2)), names


def parse_help_command_names(output: str) -> list[str]:
    """Extract root commands from Minecraft's ``help`` text when available."""

    names = {name.lower() for name in re.findall(r"/(?:minecraft:)?([a-z][a-z0-9_:-]*)", output, re.IGNORECASE)}
    return sorted(names)[:512]


def parse_minecraft_performance(output: str) -> tuple[float | None, float | None]:
    """Parse Forge/NeoForge's real ``forge tps`` result without estimating."""

    matches = re.findall(
        r"Mean tick time:\s*([0-9]+(?:\.[0-9]+)?)\s*ms.*?Mean TPS:\s*([0-9]+(?:\.[0-9]+)?)",
        output,
        re.IGNORECASE | re.DOTALL,
    )
    if not matches:
        return None, None
    mspt, tps = matches[-1]
    return min(20.0, max(0.0, float(tps))), max(0.0, float(mspt))


def read_minecraft_name_list(directory: Path, filename: str) -> list[str]:
    """Read one standard Minecraft account list without exposing UUID data."""

    path = directory / filename
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
            return []
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value[:10_000]:
        name = item.get("name") if isinstance(item, dict) else None
        if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_]{1,16}", name) and name not in result:
            result.append(name)
    return result


class RconClient:
    """Small, local-only implementation of the Minecraft RCON protocol.

    Minecraft writes a pair of log lines every time an RCON TCP connection is
    opened and closed.  Reusing one authenticated connection avoids tens of
    thousands of useless lines per day while keeping commands synchronous and
    local-only.
    """

    AUTH = 3
    COMMAND = 2

    def __init__(self, host: str, port: int, password: str, timeout: float = 8.0) -> None:
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self._connection: socket.socket | None = None
        self._next_request_id = int(time.time() * 1000) & 0x7FFFFFFC
        self._lock = threading.RLock()

    @staticmethod
    def _packet(request_id: int, packet_type: int, payload: str) -> bytes:
        encoded = payload.encode("utf-8") + b"\x00\x00"
        return struct.pack("<iii", len(encoded) + 8, request_id, packet_type) + encoded

    @staticmethod
    def _read_packet(connection: socket.socket) -> tuple[int, int, str]:
        header = RconClient._read_exact(connection, 4)
        (length,) = struct.unpack("<i", header)
        if length < 10 or length > 10_000_000:
            raise RuntimeError("Invalid RCON packet length")
        body = RconClient._read_exact(connection, length)
        request_id, packet_type = struct.unpack("<ii", body[:8])
        payload = body[8:-2].decode("utf-8", "replace")
        return request_id, packet_type, payload

    @staticmethod
    def _read_exact(connection: socket.socket, count: int) -> bytes:
        chunks: list[bytes] = []
        remaining = count
        while remaining:
            chunk = connection.recv(remaining)
            if not chunk:
                raise RuntimeError("RCON connection closed")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def close(self) -> None:
        with self._lock:
            connection, self._connection = self._connection, None
            if connection is None:
                return
            try:
                connection.close()
            except OSError:
                pass

    def _connect(self) -> socket.socket:
        if self._connection is not None:
            return self._connection
        connection = socket.create_connection((self.host, self.port), timeout=self.timeout)
        connection.settimeout(self.timeout)
        request_id = self._next_request_id
        try:
            connection.sendall(self._packet(request_id, self.AUTH, self.password))
            authenticated_id, _, _ = self._read_packet(connection)
            if authenticated_id == -1:
                raise RuntimeError("RCON authentication failed")
        except Exception:
            try:
                connection.close()
            except OSError:
                pass
            raise
        self._connection = connection
        return connection

    def command(self, command: str) -> str:
        with self._lock:
            connection = self._connect()
            # Reserve two following signed-int IDs for command completion.
            request_id = self._next_request_id
            self._next_request_id = (self._next_request_id + 4) & 0x7FFFFFFC
            command_id = request_id + 1
            terminator_id = request_id + 2
            try:
                connection.sendall(self._packet(command_id, self.COMMAND, command))
                # Long RCON results are allowed to arrive as several packets.  A
                # second, empty command gives us a deterministic end marker while
                # keeping the connection local to the Minecraft machine.
                connection.sendall(self._packet(terminator_id, self.COMMAND, ""))
                chunks: list[str] = []
                while True:
                    try:
                        response_id, _, payload = self._read_packet(connection)
                    except socket.timeout:
                        if chunks:
                            break
                        raise RuntimeError("RCON command timed out") from None
                    if response_id == -1:
                        raise RuntimeError("RCON command failed")
                    if response_id == terminator_id:
                        break
                    if response_id == command_id:
                        chunks.append(payload)
                return "".join(chunks)
            except Exception:
                # A stale connection must never poison following commands.  We
                # do not repeat because it may already have been executed.
                self.close()
                raise


class Agent:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.hub = HubClient(config)
        self.events = EventBuffer()
        self.state_directory = Path(config.state_directory)
        self.minecraft_root = Path(config.minecraft_root)
        self.backup_root = Path(config.backup_root)
        self.state_directory.mkdir(parents=True, exist_ok=True)
        self.minecraft_root.mkdir(parents=True, exist_ok=True)
        self.backup_root.mkdir(parents=True, exist_ok=True)
        self.instances = InstanceStore(
            self.state_directory / "instances.json",
            self.minecraft_root,
            config.minecraft,
        )
        self.backups = BackupManager(self.backup_root)
        self.inventory = SystemInventory(self.minecraft_root, self.backup_root)
        self._runtimes: dict[str, dict[str, Any]] = {}
        self._sync_instance_runtimes()

        # Compatibility aliases keep the proven 0.3.x code path and its tests
        # working while the new manager handles every installed instance.
        selected = self._selected_runtime()
        minecraft = config.minecraft
        self.minecraft_service = str(selected["profile"].service if selected else minecraft.get("service", "dragonfyre.service"))
        self.minecraft_directory = Path(str(selected["profile"].directory if selected else minecraft.get("directory", self.minecraft_root / "dragonfyre")))
        self.log_tail = selected["log_tail"] if selected else LogTail(Path(str(minecraft.get("log_file", self.minecraft_directory / "logs/latest.log"))))
        self.console_mode = str(selected["profile"].console_mode if selected else minecraft.get("console_mode", "rcon"))
        self.rcon = selected["rcon"] if selected else RconClient(
            str(minecraft.get("rcon_host", "127.0.0.1")),
            int(minecraft.get("rcon_port", 25575)),
            str(minecraft.get("rcon_password", "")),
            timeout=4.0,
        )
        self.tmux_session = str(selected["profile"].tmux_session if selected else minecraft.get("tmux_session", "dragonfyre"))
        self.last_heartbeat = 0.0
        self.startup = selected["startup"] if selected else MinecraftStartupTracker()
        self.last_rcon_probe = float(selected["last_rcon_probe"] if selected else 0.0)
        self.last_command_list_refresh = float(selected["last_command_list_refresh"] if selected else 0.0)
        self.online_players: list[str] = selected["online_players"] if selected else []
        self.player_count: int | None = selected["player_count"] if selected else None
        self.player_limit: int | None = selected["player_limit"] if selected else None
        self.command_names: list[str] = selected["command_names"] if selected else []
        self.system_monitor = SystemMonitor()
        self.hub_failure_count = 0
        self.hub_failure_started_at = 0.0
        self.hub_retry_not_before = 0.0
        self.last_hub_error_log_at = 0.0
        self.last_health_marker = 0.0
        self._job_backlog: dict[str, dict[str, Any]] = {}
        self.jobs = JobExecutor(
            hub=self.hub,
            instances=self.instances,
            backups=self.backups,
            service_action=self._instance_service_action,
            instance_status=self._instance_status,
            minecraft_command=self._instance_minecraft_command,
            server_action=self._structured_server_action,
            service_control=self._allowed_service_action,
            agent_update=self._update_agent,
            event=self.events.add,
            max_workers=config.max_job_workers,
        )

    def _sync_instance_runtimes(self) -> None:
        profiles = {profile.id: profile for profile in self.instances.list()}
        for instance_id in list(self._runtimes):
            if instance_id not in profiles:
                self._runtimes[instance_id]["rcon"].close()
                del self._runtimes[instance_id]
        for instance_id, profile in profiles.items():
            runtime = self._runtimes.get(instance_id)
            directory_error = ""
            try:
                instance_directory = secure_path_within(self.minecraft_root, profile.directory)
                log_value = Path(profile.log_file) if profile.log_file else instance_directory / "logs/latest.log"
                log_file = secure_path_within(instance_directory, log_value)
            except (OSError, ValueError, PermissionError) as error:
                instance_directory = self.minecraft_root / instance_id
                log_file = self.state_directory / "blocked-logs" / f"{instance_id}.log"
                directory_error = str(error)[:500]
            signature = (profile.rcon_host, profile.rcon_port, profile.rcon_password, str(log_file), directory_error)
            if runtime and runtime.get("signature") == signature:
                runtime["profile"] = profile
                runtime["directory"] = instance_directory
                continue
            if runtime:
                runtime["rcon"].close()
            self._runtimes[instance_id] = {
                "profile": profile,
                "signature": signature,
                "directory": instance_directory,
                "directory_error": directory_error,
                "log_tail": LogTail(log_file),
                "startup": MinecraftStartupTracker(),
                "rcon": RconClient(profile.rcon_host, profile.rcon_port, profile.rcon_password, timeout=4.0),
                "last_rcon_probe": 0.0,
                "last_command_list_refresh": 0.0,
                "last_performance_probe": 0.0,
                "performance_probe_supported": True,
                "tps": None,
                "mspt": None,
                "online_players": [],
                "player_count": None,
                "player_limit": None,
                "command_names": [],
                "last_log_line": None,
                "repeated_log_lines": 0,
                "last_repeat_notice": 0.0,
                "last_rcon_error_log_at": 0.0,
                "rcon_failure_count": 0,
                "last_player_lists_refresh": 0.0,
                "player_lists": {"whitelist": [], "ops": [], "banned": []},
            }

    def _selected_runtime(self) -> dict[str, Any] | None:
        selected_id = self.instances.selected_id
        return self._runtimes.get(selected_id) if selected_id else None

    def run(self) -> None:
        self.events.add("server", f"Server Control agent {AGENT_VERSION} started.")
        try:
            while True:
                try:
                    hub_synced = self._tick()
                except KeyboardInterrupt:
                    raise
                except HubError as error:
                    self._record_hub_failure(error)
                except Exception as error:  # keep control access alive after a local failure
                    self._stderr(f"Agent tick failed: {error}")
                    self.events.add("server", f"[agent] Ошибка: {error}")
                else:
                    if hub_synced:
                        self._record_hub_recovered()
                time.sleep(self.config.poll_seconds)
        finally:
            for runtime in self._runtimes.values():
                runtime["rcon"].close()
            if not self._runtimes:
                self.rcon.close()

    def _tick(self) -> bool:
        self._sync_instance_runtimes()
        self._collect_minecraft_logs()
        now = time.monotonic()
        if now < self.hub_retry_not_before:
            return False
        if now - self.last_heartbeat >= self.config.heartbeat_seconds:
            profiles = self.instances.list()
            inventory = self.inventory.snapshot(
                profiles,
                [*self.config.allowed_services, *(profile.service for profile in profiles if profile.service)],
            )
            # Instance status reads process CPU and cached directory sizes from
            # the inventory. Refresh inventory first so the first heartbeat is
            # useful and later values are not one cycle behind.
            instance_statuses = [self._instance_status(profile.id) for profile in profiles]
            selected = next((item for item in instance_statuses if item.get("id") == self.instances.selected_id), None)
            self.hub.heartbeat(
                self._server_status(),
                selected or self._minecraft_status(),
                instances=instance_statuses,
                selected_instance_id=self.instances.selected_id,
                storage=inventory.get("storage", {}),
                system=inventory.get("system", {}),
                processes=inventory.get("processes", []),
                services=inventory.get("services", []),
                java=inventory.get("java", []),
                backups=self.backups.list(cache_seconds=30),
                health={
                    "agent": "ok",
                    "protocol": PROTOCOL_VERSION,
                    "jobs_running": len(self.jobs.running_ids()),
                    "event_buffered": bool(self.events),
                },
            )
            self.last_heartbeat = now

        self._flush_events()
        work = self.hub.sync_work()
        commands = work.get("commands", [])
        for command in commands if isinstance(commands, list) else []:
            self._execute_queued_command(command)
        job_response = work
        jobs = job_response.get("jobs", [])
        if isinstance(jobs, list):
            for job in jobs:
                if isinstance(job, dict) and job.get("id"):
                    self._job_backlog[str(job["id"])] = job
        cancellations = job_response.get("cancel", [])
        if isinstance(cancellations, list):
            cancelled_ids = [str(item) for item in cancellations]
            self.jobs.cancel(cancelled_ids)
            for job_id in cancelled_ids:
                self._job_backlog.pop(job_id, None)
        running = self.jobs.running_ids()
        for job_id in list(self._job_backlog):
            if job_id in running:
                self._job_backlog.pop(job_id, None)
                continue
            if self.jobs.submit(self._job_backlog[job_id]):
                self._job_backlog.pop(job_id, None)
        self._flush_events()
        if now - self.last_health_marker >= 60:
            atomic_write_bytes(
                self.state_directory / "agent-health.json",
                json.dumps({
                    "agent_version": AGENT_VERSION,
                    "protocol_version": PROTOCOL_VERSION,
                    "updated_at": int(time.time() * 1000),
                    "hub_sync": True,
                }, ensure_ascii=False).encode("utf-8"),
                mode=0o600,
            )
            self.last_health_marker = now
        return True

    def _record_hub_failure(self, error: HubError) -> None:
        now = time.monotonic()
        self.hub_failure_count += 1
        if self.hub_failure_count == 1:
            self.hub_failure_started_at = now
            self.last_hub_error_log_at = now
            reason = str(error).strip()[:300]
            self._stderr(f"Control Hub connection lost: {reason}")
            self.events.add(
                "server",
                f"[agent] Предупреждение: связь с Control Hub временно потеряна: {reason}. "
                "Повторяющиеся сообщения скрыты.",
            )
        elif now - self.last_hub_error_log_at >= HUB_ERROR_LOG_INTERVAL_SECONDS:
            self.last_hub_error_log_at = now
            self._stderr(f"Control Hub is still unavailable; suppressed failures: {self.hub_failure_count - 1}")
        # Error 1027 is Cloudflare's exhausted daily Workers Free allowance.
        # Hammering the edge cannot recover it and only creates more noise;
        # retry slowly until the quota resets at 00:00 UTC.
        quota_exhausted = "HTTP 429" in str(error) or "1027" in str(error)
        delay = 300.0 if quota_exhausted else min(HUB_RETRY_MAX_SECONDS, float(2 ** min(self.hub_failure_count - 1, 4)))
        self.hub_retry_not_before = now + delay

    def _record_hub_recovered(self) -> None:
        if not self.hub_failure_count:
            return
        elapsed = max(1, round(time.monotonic() - self.hub_failure_started_at))
        suppressed = max(0, self.hub_failure_count - 1)
        self._stderr(f"Control Hub connection restored after {elapsed}s")
        self.events.add(
            "server",
            f"[agent] Связь с Control Hub восстановлена через {elapsed} с. "
            f"Скрыто повторных сообщений: {suppressed}.",
        )
        self.hub_failure_count = 0
        self.hub_failure_started_at = 0.0
        self.hub_retry_not_before = 0.0
        self.last_hub_error_log_at = 0.0

    def _collect_minecraft_logs(self) -> None:
        now = time.monotonic()
        for instance_id, runtime in self._runtimes.items():
            for line in runtime["log_tail"].read_new_lines():
                runtime["startup"].observe(line)
                if is_rcon_lifecycle_log(line):
                    continue
                if line == runtime.get("last_log_line"):
                    runtime["repeated_log_lines"] = int(runtime.get("repeated_log_lines", 0)) + 1
                    if now - float(runtime.get("last_repeat_notice", 0.0)) >= 30:
                        self.events.add(
                            "minecraft",
                            f"[повторено ещё {runtime['repeated_log_lines']} раз] {line}",
                            instance_id=instance_id,
                            source="minecraft",
                        )
                        runtime["repeated_log_lines"] = 0
                        runtime["last_repeat_notice"] = now
                    continue
                if runtime.get("repeated_log_lines"):
                    self.events.add(
                        "minecraft",
                        f"[предыдущая строка повторилась ещё {runtime['repeated_log_lines']} раз]",
                        instance_id=instance_id,
                        source="minecraft",
                    )
                runtime["last_log_line"] = line
                runtime["repeated_log_lines"] = 0
                runtime["last_repeat_notice"] = now
                self.events.add("minecraft", line, instance_id=instance_id, source="minecraft")

    def _flush_events(self) -> None:
        while self.events:
            batch: list[dict[str, str]] = []
            overflow_notice = self.events.take_overflow_notice()
            if overflow_notice:
                batch.append(overflow_notice)
            batch.extend(self.events.take(100 - len(batch)))
            if not batch:
                return
            try:
                self.hub.push_events(batch)
            except HubError:
                self.events.restore_front(batch)
                raise

    def _execute_queued_command(self, command: dict[str, Any]) -> None:
        command_id = str(command.get("id", ""))
        command_type = str(command.get("type", ""))
        payload = command.get("payload") if isinstance(command.get("payload"), dict) else {}
        if not command_id:
            return

        self.events.add("server", f"[queue] Выполняю {command_type} ({command_id[:8]}).")
        try:
            result = self._dispatch(command_type, payload)
            self.hub.complete_command(command_id, "completed", result)
            self.events.add("server", f"[queue] {command_type}: выполнено.")
        except Exception as error:
            result = {"message": str(error)[:2000]}
            self._stderr(f"Command {command_type} failed: {error}")
            self.events.add("server", f"[queue] {command_type}: ошибка: {error}")
            try:
                self.hub.complete_command(command_id, "failed", result)
            except HubError as completion_error:
                self._stderr(f"Could not report command failure: {completion_error}")

    def _dispatch(self, command_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        if command_type == "shell_command":
            command = str(payload.get("command", "")).strip()
            return self._run_allowed_shell_command(command)
        if command_type == "server_status":
            return {"server": self._server_status(), "minecraft": self._minecraft_status()}
        if command_type == "server_backup":
            return self._run_backup()
        if command_type == "server_reboot":
            self._sudo_systemctl("reboot")
            return {"message": "Перезагрузка запрошена."}
        if command_type == "server_shutdown":
            self._sudo_systemctl("poweroff")
            return {"message": "Выключение запрошено."}
        if command_type == "minecraft_start":
            return self._minecraft_service_action("start")
        if command_type == "minecraft_stop":
            return self._minecraft_service_action("stop")
        if command_type == "minecraft_restart":
            return self._minecraft_service_action("restart")
        if command_type == "minecraft_status":
            return self._minecraft_status()
        if command_type == "minecraft_command":
            command = str(payload.get("command", "")).strip().lstrip("/")
            if not command or len(command) > 256:
                raise ValueError("Некорректная команда Minecraft")
            output = self._minecraft_command(command)
            # The desktop prints the submitted command immediately.  Other
            # clients still receive the result through this shared console.
            self.events.add("minecraft", f"[RCON] {output or 'Команда выполнена.'}".strip())
            return {"command": command, "output": output[:4000]}
        if command_type == "safe_power_off":
            return self._prepare_safe_power_off()
        raise ValueError(f"Неизвестная команда: {command_type}")

    def _server_status(self) -> dict[str, Any]:
        metrics = self.system_monitor.sample()
        filesystem = metrics.get("filesystem") if isinstance(metrics.get("filesystem"), dict) else {}
        uptime_seconds = metrics.get("uptime_seconds")
        return {
            "hostname": socket.gethostname(),
            # Keep compact fields for older desktop clients.  Newer versions
            # use the structured metrics block below.
            "uptime": f"{uptime_seconds or 0} seconds",
            "disk": f"{filesystem.get('percent', '—')}% used on /",
            "metrics": metrics,
            "agent_time": int(time.time()),
        }

    def _minecraft_status(self) -> dict[str, Any]:
        if self.instances.selected_id and self.instances.selected_id in self._runtimes:
            return self._instance_status(self.instances.selected_id)
        result = self._run(["systemctl", "is-active", self.minecraft_service], timeout=5)
        active = result["stdout"].strip() == "active"
        self.startup.set_service_active(active)
        return {
            "id": "legacy",
            "name": "Minecraft",
            "service": self.minecraft_service,
            "active": active,
            "state": "RUNNING" if active else "OFFLINE",
            "service_state": result["stdout"].strip() or result["stderr"].strip(),
            "log_file": str(self.log_tail.path),
            "console_mode": self.console_mode,
            "startup": self.startup.snapshot(),
            "players": {"online": self.player_count, "max": self.player_limit, "names": self.online_players},
            "command_names": self.command_names,
        }

    def _instance_status(self, instance_id: str) -> dict[str, Any]:
        identifier = validate_instance_id(instance_id)
        self._sync_instance_runtimes()
        profile = self.instances.get(identifier)
        runtime = self._runtimes[identifier]
        service = self._service_snapshot(profile.service)
        service_state = str(service.get("active_state") or "unknown")
        active = service_state in {"active", "activating", "deactivating"}
        runtime["startup"].set_service_active(active)
        if active and not runtime.get("directory_error"):
            self._refresh_instance_runtime(identifier, runtime)
        else:
            runtime["online_players"] = []
            runtime["player_count"] = None
            runtime["player_limit"] = None
            runtime["rcon"].close()
        startup = runtime["startup"].snapshot()
        if service_state == "deactivating" or startup.get("state") == "STOPPING":
            state = "STOPPING"
        elif startup.get("state") == "CRASHED":
            state = "CRASHED"
        elif active and startup.get("ready"):
            state = "RUNNING"
        elif active:
            state = "STARTING"
        else:
            state = "OFFLINE"
        process = service.get("process") if isinstance(service.get("process"), dict) else {}
        if runtime.get("directory_error"):
            public = profile.to_public()
            message = f"Путь сборки заблокирован политикой безопасности: {runtime['directory_error']}"
            return {
                **public,
                "active": active,
                "state": "UNKNOWN",
                "service_state": service_state,
                "service_sub_state": service.get("sub_state", "unknown"),
                "startup": {"phase": "blocked", "progress": 0, "label": "Сборка заблокирована", "detail": message, "ready": False, "state": "UNKNOWN"},
                "crash": {"code": "invalid_instance_path", "summary": "Небезопасный путь сборки", "solution": "Верните обычную директорию внутрь minecraft_root без символических ссылок.", "evidence": message},
                "players": {"online": None, "max": None, "names": []},
                "command_names": [], "player_lists": {"whitelist": [], "ops": [], "banned": []},
                "pid": process.get("pid"), "process_memory_bytes": process.get("memory_bytes"),
                "process_cpu_percent": process.get("cpu_percent"), "uptime_seconds": process.get("uptime_seconds"),
                "size": 0, "tps": None, "mspt": None,
            }
        size_item = next(
            (
                item for item in self.inventory._sizes.get("instances", [])
                if isinstance(item, dict) and item.get("instance_id") == identifier
            ),
            {},
        )
        try:
            size = int(size_item.get("bytes", 0))
        except (TypeError, ValueError):
            size = 0
        public = profile.to_public()
        directory = runtime["directory"]
        now = time.monotonic()
        if now - float(runtime.get("last_player_lists_refresh", 0.0)) >= PLAYER_LIST_REFRESH_SECONDS:
            runtime["last_player_lists_refresh"] = now
            runtime["player_lists"] = {
                "whitelist": read_minecraft_name_list(directory, "whitelist.json"),
                "ops": read_minecraft_name_list(directory, "ops.json"),
                "banned": read_minecraft_name_list(directory, "banned-players.json"),
            }
        return {
            **public,
            "active": active,
            "state": state,
            "service_state": service_state,
            "service_sub_state": service.get("sub_state", "unknown"),
            "startup": startup,
            "crash": startup.get("crash"),
            "players": {
                "online": runtime["player_count"],
                "max": runtime["player_limit"],
                "names": list(runtime["online_players"]),
            },
            "command_names": list(runtime["command_names"]),
            "player_lists": runtime["player_lists"],
            "pid": process.get("pid"),
            "process_memory_bytes": process.get("memory_bytes"),
            "process_cpu_percent": process.get("cpu_percent"),
            "uptime_seconds": process.get("uptime_seconds"),
            "size": size,
            "size_truncated": bool(size_item.get("truncated")),
            "tps": runtime.get("tps"),
            "mspt": runtime.get("mspt"),
        }

    def _refresh_instance_runtime(self, instance_id: str, runtime: dict[str, Any]) -> None:
        """Confirm readiness and gather console completion data for one pack."""

        profile: InstanceProfile = runtime["profile"]
        rcon: RconClient = runtime["rcon"]
        if profile.console_mode != "rcon" or not rcon.password or rcon.password.startswith("REPLACE_"):
            return
        now = time.monotonic()
        if now - float(runtime["last_rcon_probe"]) >= RCON_STATUS_INTERVAL_SECONDS:
            runtime["last_rcon_probe"] = now
            try:
                player_output = rcon.command("list")
            except Exception as error:
                runtime["startup"].wait_for_rcon()
                runtime["rcon_failure_count"] = int(runtime.get("rcon_failure_count", 0)) + 1
                last_error_at = float(runtime.get("last_rcon_error_log_at", 0.0))
                if now - last_error_at >= HUB_ERROR_LOG_INTERVAL_SECONDS:
                    runtime["last_rcon_error_log_at"] = now
                    self._stderr(
                        f"RCON readiness check failed for {instance_id}: {error}; "
                        f"suppressed={max(0, runtime['rcon_failure_count'] - 1)}"
                    )
            else:
                runtime["rcon_failure_count"] = 0
                runtime["last_rcon_error_log_at"] = 0.0
                online, maximum, players = parse_minecraft_player_list(player_output)
                runtime["player_count"] = online
                runtime["player_limit"] = maximum
                runtime["online_players"] = players
                if online is None:
                    runtime["startup"].mark_ready("RCON-консоль подключена")
                else:
                    runtime["startup"].mark_ready(f"RCON подключён · игроков: {online}/{maximum}")

        if runtime["startup"].ready and now - float(runtime["last_command_list_refresh"]) >= COMMAND_LIST_REFRESH_SECONDS:
            runtime["last_command_list_refresh"] = now
            try:
                discovered = parse_help_command_names(rcon.command("help"))
            except Exception as error:
                self._stderr(f"RCON help refresh failed for {instance_id}: {error}")
            else:
                if discovered:
                    runtime["command_names"] = discovered

        loader = profile.loader.casefold()
        if (
            runtime["startup"].ready
            and "forge" in loader
            and runtime.get("performance_probe_supported", True)
            and now - float(runtime.get("last_performance_probe", 0.0)) >= PERFORMANCE_REFRESH_SECONDS
        ):
            runtime["last_performance_probe"] = now
            try:
                tps, mspt = parse_minecraft_performance(rcon.command("forge tps"))
            except Exception:
                # RCON failures are already reflected by the regular readiness
                # probe; a performance metric must never destabilize status.
                pass
            else:
                if tps is None:
                    runtime["performance_probe_supported"] = False
                else:
                    runtime["tps"], runtime["mspt"] = tps, mspt

    def _refresh_rcon_runtime(self) -> None:
        selected = self._selected_runtime()
        if selected and self.instances.selected_id:
            self._refresh_instance_runtime(self.instances.selected_id, selected)

    def _service_snapshot(self, service: str) -> dict[str, Any]:
        if not service:
            return {"active_state": "inactive", "sub_state": "dead", "process": {"pid": None, "memory_bytes": None, "cpu_percent": None, "uptime_seconds": None}}
        cached = next(
            (item for item in self.inventory._services_cache if item.get("name") == service),
            None,
        )
        if cached:
            try:
                pid = int(cached.get("pid", 0) or 0)
            except (TypeError, ValueError):
                pid = 0
            process = next(
                (item for item in self.inventory._snapshot.get("processes", []) if item.get("pid") == pid),
                {},
            )
            return {
                "active_state": cached.get("active", "unknown"),
                "sub_state": cached.get("sub_state", "unknown"),
                "process": {
                    "pid": pid or None,
                    "memory_bytes": process.get("memory_bytes"),
                    "cpu_percent": process.get("cpu_percent"),
                    "uptime_seconds": process.get("runtime_seconds"),
                },
            }
        result = self._run(
            ["systemctl", "show", service, "--property=ActiveState,SubState,MainPID,ActiveEnterTimestampMonotonic", "--no-pager"],
            timeout=5,
        )
        properties: dict[str, str] = {}
        for line in result["stdout"].splitlines():
            key, separator, value = line.partition("=")
            if separator:
                properties[key] = value
        try:
            pid = int(properties.get("MainPID", "0") or 0)
        except ValueError:
            pid = 0
        memory = None
        if pid > 0:
            try:
                pages = int(Path(f"/proc/{pid}/statm").read_text(encoding="ascii").split()[1])
                memory = pages * os.sysconf("SC_PAGE_SIZE")
            except (OSError, ValueError, IndexError):
                pass
        uptime_seconds = None
        try:
            entered_us = int(properties.get("ActiveEnterTimestampMonotonic", "0") or 0)
            system_uptime = float(Path("/proc/uptime").read_text(encoding="ascii").split()[0])
            if entered_us:
                uptime_seconds = max(0, int(system_uptime - entered_us / 1_000_000))
        except (OSError, ValueError, IndexError):
            pass
        cpu = None
        for item in self.inventory._snapshot.get("processes", []):
            if item.get("pid") == pid:
                cpu = item.get("cpu_percent")
                break
        return {
            "active_state": properties.get("ActiveState", "unknown"),
            "sub_state": properties.get("SubState", "unknown"),
            "process": {"pid": pid or None, "memory_bytes": memory, "cpu_percent": cpu, "uptime_seconds": uptime_seconds},
        }

    def _run_allowed_shell_command(self, command: str) -> dict[str, Any]:
        if not command or len(command) > 512:
            raise ValueError("Некорректная команда")
        if any(character in command for character in (";", "|", "&", ">", "<", "`", "$", "\n", "\r")):
            raise PermissionError("Командные цепочки и перенаправления запрещены")
        allowed_commands = self.config.commands.get("allow_shell_commands")
        # ``allow_shell_prefixes`` is accepted only for backwards-compatible
        # configuration loading.  It now means an *exact* command too: a
        # prefix such as ``tail -n`` must no longer unlock arbitrary paths.
        if not isinstance(allowed_commands, list):
            allowed_commands = self.config.commands.get("allow_shell_prefixes", [])
        normalized_allowed = {
            item.strip() for item in allowed_commands if isinstance(item, str) and item.strip()
        } if isinstance(allowed_commands, list) else set()
        if command not in normalized_allowed:
            raise PermissionError("Разрешены только точные диагностические команды из allow-list агента")
        try:
            args = shlex.split(command)
        except ValueError as error:
            raise ValueError(f"Ошибка разбора команды: {error}") from error
        result = self._run(args, timeout=30)
        output = (result["stdout"] + result["stderr"]).strip()
        self.events.add("server", f"$ {command}\n{output}".strip())
        return {"command": command, "exit_code": result["returncode"], "output": output[:6000]}

    def _run_backup(self) -> dict[str, Any]:
        command = str(self.config.commands.get("backup_command", "")).strip()
        if not command:
            raise ValueError("backup_command не задан в конфигурации")
        args = shlex.split(command)
        result = self._run(args, timeout=60 * 60)
        output = (result["stdout"] + result["stderr"]).strip()
        self.events.add("server", f"$ {command}\n{output}".strip())
        if result["returncode"] != 0:
            raise RuntimeError(output or f"Резервное копирование завершилось с кодом {result['returncode']}")
        return {"output": output[:6000]}

    def _minecraft_service_action(self, action: str) -> dict[str, Any]:
        if self.instances.selected_id:
            return self._instance_service_action(self.instances.selected_id, action, 180 if action == "stop" else 60)
        timeout_seconds = 150 if action == "stop" else 45
        self._sudo_systemctl(action, self.minecraft_service, timeout_seconds)
        time.sleep(1)
        return self._minecraft_status()

    def _instance_service_action(self, instance_id: str, action: str, timeout_seconds: int = 60) -> dict[str, Any]:
        identifier = validate_instance_id(instance_id)
        profile = self.instances.get(identifier)
        runtime = self._runtimes.get(identifier)
        if action not in {"start", "stop", "restart", "kill"}:
            raise ValueError("Недопустимое действие Minecraft-службы")
        if not profile.service or not re.fullmatch(r"[A-Za-z0-9@_.:-]{1,128}\.service", profile.service):
            raise ValueError("У сборки не настроена безопасная systemd-служба")
        if action in {"start", "restart"} and runtime and runtime.get("directory_error"):
            raise PermissionError(f"Запуск заблокирован: {runtime['directory_error']}")
        if action == "start":
            if profile.managed_service and (not profile.startup_reviewed or not profile.startup_command):
                raise RuntimeError("Сначала подтвердите безопасную команду запуска в настройках сборки")
            self._sudo_systemctl("start", profile.service, min(60, timeout_seconds))
            time.sleep(1)
            return self._instance_status(identifier)
        if action == "kill":
            result = self._run(
                ["sudo", "-n", "/usr/local/sbin/server-control-service-control", "kill", profile.service],
                timeout=min(30, timeout_seconds),
            )
            if result["returncode"] != 0:
                raise RuntimeError(result["stderr"].strip() or "Не удалось принудительно завершить Minecraft")
            if runtime:
                runtime["startup"].crash = {
                    "code": "force_killed", "summary": "Процесс был принудительно завершён",
                    "solution": "Проверьте целостность мира перед следующим запуском.",
                }
            return self._instance_status(identifier)

        if action == "restart":
            self._instance_service_action(identifier, "stop", max(120, timeout_seconds))
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if not self._port_is_open(profile.rcon_host, profile.port):
                    break
                time.sleep(1)
            else:
                raise RuntimeError("Порт Minecraft не освободился после остановки")
            return self._instance_service_action(identifier, "start", 60)

        # Safe stop: ask Minecraft itself first, then let systemd send SIGINT.
        if runtime:
            runtime["startup"].expect_service_stop()
        if self._instance_status(identifier).get("active"):
            try:
                if profile.console_mode == "rcon" and profile.rcon_password and not profile.rcon_password.startswith("REPLACE_"):
                    self._instance_minecraft_command(identifier, "save-all flush")
                    self._instance_minecraft_command(identifier, profile.shutdown_command)
                    deadline = time.monotonic() + min(120, timeout_seconds)
                    while time.monotonic() < deadline:
                        state = self._run(["systemctl", "is-active", profile.service], timeout=5)["stdout"].strip()
                        if state not in {"active", "activating", "deactivating"}:
                            return self._instance_status(identifier)
                        time.sleep(2)
            except Exception as error:
                self.events.add(
                    "minecraft",
                    f"[safe stop] RCON недоступен: {error}; перехожу к корректной остановке systemd.",
                    instance_id=identifier,
                    source="agent",
                    level="WARN",
                )
            self._sudo_systemctl("stop", profile.service, max(150, timeout_seconds))
        return self._instance_status(identifier)

    @staticmethod
    def _port_is_open(host: str, port: int) -> bool:
        try:
            with socket.create_connection((host, int(port)), timeout=0.5):
                return True
        except OSError:
            return False

    def _prepare_safe_power_off(self) -> dict[str, Any]:
        save_output: dict[str, str] = {}
        self.events.add("server", "[safe off] Начинаю безопасное выключение.")
        for profile in self.instances.list():
            if not self._instance_status(profile.id).get("active"):
                continue
            self.events.add("server", f"[safe off] Сохраняю и останавливаю {profile.name}.")
            try:
                save_output[profile.id] = self._instance_minecraft_command(profile.id, "save-all flush")[:1000]
            except Exception as error:
                self.events.add("minecraft", f"[safe off] RCON недоступен: {error}; останавливаю службу.", instance_id=profile.id, level="WARN")
            self._instance_service_action(profile.id, "stop", 180)
        still_active = [profile.id for profile in self.instances.list() if self._instance_status(profile.id).get("active")]
        if still_active:
            raise RuntimeError(f"Minecraft не остановился: {', '.join(still_active)}; питание не отключено")
        self.events.add("server", "[safe off] Все сборки остановлены, синхронизирую данные.")
        self._run(["sync"], timeout=15)
        return {"ready_for_power_off": True, "save_output": save_output}

    def _minecraft_command(self, command: str) -> str:
        if self.instances.selected_id:
            return self._instance_minecraft_command(self.instances.selected_id, command)
        if self.console_mode == "rcon":
            if not self.rcon.password or self.rcon.password.startswith("REPLACE_"):
                raise RuntimeError("RCON пароль не настроен")
            return self.rcon.command(command)
        if self.console_mode == "tmux":
            result = self._run(["tmux", "send-keys", "-t", self.tmux_session, command, "Enter"], timeout=10)
            if result["returncode"] != 0:
                raise RuntimeError(result["stderr"].strip() or "Не удалось передать команду в tmux")
            return "Команда отправлена в Minecraft-консоль."
        raise RuntimeError(f"Неизвестный console_mode: {self.console_mode}")

    def _instance_minecraft_command(self, instance_id: str, command: str) -> str:
        identifier = validate_instance_id(instance_id)
        self._sync_instance_runtimes()
        runtime = self._runtimes[identifier]
        profile: InstanceProfile = runtime["profile"]
        normalized = str(command).strip().lstrip("/")
        if not normalized or len(normalized) > 512 or any(character in normalized for character in "\r\n\0"):
            raise ValueError("Некорректная команда Minecraft")
        if profile.console_mode == "rcon":
            rcon: RconClient = runtime["rcon"]
            if not rcon.password or rcon.password.startswith("REPLACE_"):
                raise RuntimeError("RCON пароль не настроен")
            return rcon.command(normalized)
        if profile.console_mode == "tmux":
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", profile.tmux_session):
                raise RuntimeError("Некорректное имя tmux-сессии")
            result = self._run(["tmux", "send-keys", "-t", profile.tmux_session, normalized, "Enter"], timeout=10)
            if result["returncode"] != 0:
                raise RuntimeError(result["stderr"].strip() or "Не удалось передать команду в tmux")
            return "Команда отправлена в Minecraft-консоль."
        raise RuntimeError(f"Неизвестный console_mode: {profile.console_mode}")

    def _structured_server_action(self, action: str) -> dict[str, Any]:
        if action not in {"reboot", "shutdown"}:
            raise ValueError("Недопустимое действие Linux-сервера")
        stopped: list[str] = []
        for profile in self.instances.list():
            if self._instance_status(profile.id).get("active"):
                self._instance_service_action(profile.id, "stop", 180)
                stopped.append(profile.id)
        self._run(["sync"], timeout=15)
        self._sudo_systemctl("reboot" if action == "reboot" else "poweroff")
        return {"action": action, "minecraft_stopped": stopped, "message": "Команда Linux принята."}

    def _allowed_service_action(self, service: str, action: str) -> dict[str, Any]:
        allowed = set(self.config.allowed_services)
        profiles = self.instances.list()
        allowed.update(profile.service for profile in profiles if profile.service)
        if service not in allowed or not re.fullmatch(r"[A-Za-z0-9@_.:-]{1,128}\.service", service):
            raise PermissionError("Служба отсутствует в allow-list агента")
        if action not in {"start", "stop", "restart", "status"}:
            raise ValueError("Недопустимое действие службы")
        if service == "server-control-agent.service" and action != "status":
            raise RuntimeError("Agent обновляется отдельным безопасным механизмом с rollback")
        minecraft_profile = next((profile for profile in profiles if profile.service == service), None)
        if minecraft_profile and action in {"start", "stop", "restart"}:
            return self._instance_service_action(minecraft_profile.id, action, 180 if action != "start" else 60)
        if action == "status":
            result = self._run(["systemctl", "show", service, "--property=Id,Description,ActiveState,SubState,MainPID", "--no-pager"], timeout=10)
        else:
            self._sudo_systemctl(action, service, 180 if action == "stop" else 60)
            result = self._run(["systemctl", "is-active", service], timeout=5)
        return {"service": service, "action": action, "exit_code": result["returncode"], "output": (result["stdout"] + result["stderr"]).strip()[:8000]}

    def _update_agent(self, payload: dict[str, Any]) -> dict[str, Any]:
        version = str(payload.get("version", "latest"))
        if version != "latest" and not re.fullmatch(r"v?\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", version):
            raise ValueError("Некорректная версия Agent")
        command = list(self.config.agent_update_command)
        if not command or command[0] != "sudo":
            raise PermissionError("agent_update_command должен вызывать фиксированный root-helper через sudo")
        result = self._run([*command, "--version", version], timeout=10 * 60)
        output = (result["stdout"] + result["stderr"]).strip()
        if result["returncode"] != 0:
            raise RuntimeError(output or "Безопасное обновление Agent не выполнено")
        return {"version": version, "output": output[:16_000], "rollback_available": True}

    def _sudo_systemctl(self, action: str, service: str | None = None, timeout_seconds: int = 45) -> None:
        if service is None:
            args = ["sudo", "-n", "/usr/bin/systemctl", action]
        else:
            args = ["sudo", "-n", "/usr/local/sbin/server-control-service-control", action, service]
        result = self._run(args, timeout=timeout_seconds)
        if result["returncode"] != 0:
            raise RuntimeError(result["stderr"].strip() or "Команда systemctl не выполнена")

    @staticmethod
    def _run(args: list[str], timeout: int) -> dict[str, Any]:
        """Run one fixed executable with bounded output and a killable process group.

        ``shell=True`` is never used.  A timed-out backup or diagnostic may
        spawn children, so a process group is terminated rather than leaving
        orphan processes alive.  Output is read on two small background
        readers to avoid a noisy process filling a pipe or agent memory.
        """

        if not args or not all(isinstance(item, str) and item for item in args):
            raise ValueError("Некорректный список аргументов команды")
        environment = {
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TERM": "dumb",
            "HOME": "/tmp",
        }
        process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd="/",
            env=environment,
            start_new_session=True,
        )

        def read_stream(stream: Any, capture: dict[str, Any]) -> None:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    return
                remaining = MAX_SUBPROCESS_OUTPUT_BYTES - len(capture["data"])
                if remaining > 0:
                    capture["data"].extend(chunk[:remaining])
                if len(chunk) > remaining:
                    capture["truncated"] = True

        stdout_capture: dict[str, Any] = {"data": bytearray(), "truncated": False}
        stderr_capture: dict[str, Any] = {"data": bytearray(), "truncated": False}
        stdout_reader = threading.Thread(target=read_stream, args=(process.stdout, stdout_capture), daemon=True)
        stderr_reader = threading.Thread(target=read_stream, args=(process.stderr, stderr_capture), daemon=True)
        stdout_reader.start()
        stderr_reader.start()

        timed_out = False
        try:
            process.wait(timeout=max(1, timeout))
        except subprocess.TimeoutExpired:
            timed_out = True
            Agent._terminate_process_group(process)
        finally:
            stdout_reader.join(timeout=2)
            stderr_reader.join(timeout=2)

        def decode(capture: dict[str, Any]) -> str:
            output = bytes(capture["data"]).decode("utf-8", "replace")
            if capture["truncated"]:
                output += "\n[Вывод ограничен 128 КиБ]"
            return output

        stderr = decode(stderr_capture)
        if timed_out:
            stderr = f"{stderr}\n[Команда превысила лимит {timeout} с и была остановлена]".strip()
        return {
            "returncode": 124 if timed_out else int(process.returncode or 0),
            "stdout": decode(stdout_capture),
            "stderr": stderr,
            "timed_out": timed_out,
        }

    @staticmethod
    def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            return
        process.wait(timeout=3)

    @staticmethod
    def _stderr(message: str) -> None:
        print(message, file=sys.stderr, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Server Control home-server agent")
    parser.add_argument("--config", default="/etc/server-control/agent-config.json", help="Path to JSON configuration")
    args = parser.parse_args()
    config = Config.load(Path(args.config))
    Agent(config).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
