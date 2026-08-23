#!/usr/bin/env python3
"""Server Control agent.

The agent is deliberately dependency-free: it uses only the Python standard
library and communicates outward to the Cloudflare Worker over HTTPS. It never
opens a network port on the home server.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
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


AGENT_VERSION = "1.2.1"
MAX_EVENT_MESSAGE = 8000
MAX_EVENT_BUFFER_EVENTS = 2_000
MAX_EVENT_BUFFER_BYTES = 2 * 1024 * 1024
MAX_SUBPROCESS_OUTPUT_BYTES = 128 * 1024
RCON_STATUS_INTERVAL_SECONDS = 2.0
COMMAND_LIST_REFRESH_SECONDS = 15 * 60
HUB_RETRY_MAX_SECONDS = 10.0
HUB_ERROR_LOG_INTERVAL_SECONDS = 60.0


def is_rcon_lifecycle_log(message: str) -> bool:
    """Hide connection bookkeeping produced by Minecraft's local RCON server."""

    lowered = message.casefold()
    return (
        "thread rcon client" in lowered
        and ("rcon listener" in lowered or "rcon client" in lowered)
        and (" started" in lowered or " shutting down" in lowered)
    )


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
            # Commands are fetched every second.  This is the shortest useful
            # delay without a permanently connected paid relay, while the
            # heartbeat itself remains less frequent.
            poll_seconds=max(0.5, min(1.0, float(raw.get("poll_seconds", 1)))),
            heartbeat_seconds=max(1, min(2, int(raw.get("heartbeat_seconds", 2)))),
            request_timeout_seconds=max(5, int(raw.get("request_timeout_seconds", 20))),
            minecraft=dict(raw["minecraft"]),
            commands=dict(raw["commands"]),
        )


class HubClient:
    def __init__(self, config: Config) -> None:
        self.base_url = config.hub_url
        self.api_key = config.agent_api_key
        self.timeout = config.request_timeout_seconds

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            method=method,
            headers={
                "X-Agent-Key": self.api_key,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": f"ServerControlAgent/{AGENT_VERSION}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response_data = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            details = error.read().decode("utf-8", "replace")
            raise HubError(f"Hub returned HTTP {error.code}: {details[:500]}") from error
        except urllib.error.URLError as error:
            raise HubError(f"Hub unavailable: {error.reason}") from error
        except TimeoutError as error:
            # ``http.client`` can surface a read timeout directly instead of
            # wrapping it in URLError.  Treat both forms as the same temporary
            # Control Hub outage so the retry/backoff logic can coalesce them.
            raise HubError(f"Hub unavailable: {error}") from error

        try:
            parsed = json.loads(response_data)
        except json.JSONDecodeError as error:
            raise HubError("Hub returned invalid JSON") from error
        if not isinstance(parsed, dict):
            raise HubError("Hub returned an unexpected response")
        return parsed

    def heartbeat(self, server: dict[str, Any], minecraft: dict[str, Any]) -> None:
        self.request(
            "POST",
            "/v1/agent/heartbeat",
            {"agent_version": AGENT_VERSION, "server": server, "minecraft": minecraft},
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
        self._events: deque[dict[str, str]] = deque()
        self._max_events = max(1, max_events)
        self._max_bytes = max(256, max_bytes)
        self._size_bytes = 0
        self._dropped_events = 0

    @staticmethod
    def _event_size(event: dict[str, str]) -> int:
        return 64 + len(event["kind"].encode("utf-8")) + len(event["message"].encode("utf-8"))

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

    def add(self, kind: str, message: str) -> None:
        clean = message.strip()
        if not clean:
            return
        clean = clean[:MAX_EVENT_MESSAGE]
        max_message_bytes = max(1, self._max_bytes - 96)
        encoded = clean.encode("utf-8")
        if len(encoded) > max_message_bytes:
            clean = encoded[:max_message_bytes].decode("utf-8", "ignore")
        event = {"kind": kind, "message": clean}
        size = self._event_size(event)
        self._make_room(size, from_left=True)
        self._events.append(event)
        self._size_bytes += size

    def take(self, count: int = 100) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        while self._events and len(result) < count:
            event = self._events.popleft()
            self._size_bytes -= self._event_size(event)
            result.append(event)
        return result

    def restore_front(self, events: list[dict[str, str]]) -> None:
        for event in reversed(events):
            if not isinstance(event, dict) or not isinstance(event.get("kind"), str) or not isinstance(event.get("message"), str):
                continue
            size = self._event_size(event)
            self._make_room(size, from_left=False)
            self._events.appendleft(event)
            self._size_bytes += size

    def take_overflow_notice(self) -> dict[str, str] | None:
        if not self._dropped_events:
            return None
        dropped = self._dropped_events
        self._dropped_events = 0
        return {
            "kind": "server",
            "message": f"[agent] Буфер событий был переполнен: пропущено строк: {dropped}. Проверьте доступность Control Hub.",
        }

    def __bool__(self) -> bool:
        return bool(self._events) or self._dropped_events > 0


class LogTail:
    """Reads appended Minecraft log lines without keeping the file open."""

    def __init__(self, path: Path, initial_lines: int = 100) -> None:
        self.path = path
        self.position = 0
        self.initial_lines = initial_lines
        self.initialized = False

    def read_new_lines(self) -> list[str]:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return []

        if not self.initialized:
            self.initialized = True
            with self.path.open("r", encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
                self.position = handle.tell()
            return [line.rstrip() for line in lines[-self.initial_lines :] if line.strip()]

        if size < self.position:  # log rotation or truncation
            self.position = 0
        with self.path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(self.position)
            data = handle.read(128 * 1024)
            self.position = handle.tell()
        return [line.rstrip() for line in data.splitlines() if line.strip()]


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

    def set_service_active(self, active: bool) -> None:
        if active and not self._service_active:
            self.phase = "starting_java"
            self.progress = 3
            self.label = "Запускаю Java и Forge"
            self.detail = "Ожидаю первые строки запуска"
            self.ready = False
            self.started_at = int(time.time() * 1000)
        elif not active:
            self.phase = "stopped"
            self.progress = 0
            self.label = "Сервер остановлен"
            self.detail = "Minecraft-служба не запущена"
            self.ready = False
            self.started_at = None
        self._service_active = active

    def observe(self, line: str) -> None:
        self.last_log_at = int(time.time() * 1000)
        text = line.lower()

        if "done (" in text and "for help, type \"help\"" in text:
            self.mark_ready("Minecraft полностью запущен")
            return
        if "for help, type \"help\"" in text:
            self.mark_ready("Minecraft полностью запущен")
            return
        if any(marker in text for marker in ("crash report", "exception in server tick loop", "fatal", "could not load", "failed to start")):
            self._set("failed", self.progress, "Ошибка запуска", line[-240:], force=True)
            self.ready = False
            return
        if any(marker in text for marker in ("stopping server", "stopping the server", "server shutting down")):
            self._set("stopping", self.progress, "Останавливаю Minecraft", "Жду завершения службы", force=True)
            self.ready = False
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

    def snapshot(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "progress": self.progress,
            "label": self.label,
            "detail": self.detail,
            "ready": self.ready,
            "started_at": self.started_at,
            "last_log_at": self.last_log_at,
        }

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
            # A stale connection must never poison following commands.  We do
            # not automatically repeat the command because it may already
            # have been executed before the connection failed.
            self.close()
            raise


class Agent:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.hub = HubClient(config)
        self.events = EventBuffer()
        minecraft = config.minecraft
        self.minecraft_service = str(minecraft.get("service", "dragonfyre.service"))
        self.minecraft_directory = Path(str(minecraft.get("directory", "/opt/minecraft/dragonfyre")))
        self.log_tail = LogTail(Path(str(minecraft.get("log_file", self.minecraft_directory / "logs/latest.log"))))
        self.console_mode = str(minecraft.get("console_mode", "rcon"))
        self.rcon = RconClient(
            str(minecraft.get("rcon_host", "127.0.0.1")),
            int(minecraft.get("rcon_port", 25575)),
            str(minecraft.get("rcon_password", "")),
            timeout=4.0,
        )
        self.tmux_session = str(minecraft.get("tmux_session", "dragonfyre"))
        self.last_heartbeat = 0.0
        self.startup = MinecraftStartupTracker()
        self.last_rcon_probe = 0.0
        self.last_command_list_refresh = 0.0
        self.online_players: list[str] = []
        self.player_count: int | None = None
        self.player_limit: int | None = None
        self.command_names: list[str] = []
        self.system_monitor = SystemMonitor()
        self.hub_failure_count = 0
        self.hub_failure_started_at = 0.0
        self.hub_retry_not_before = 0.0
        self.last_hub_error_log_at = 0.0

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
            self.rcon.close()

    def _tick(self) -> bool:
        self._collect_minecraft_logs()
        now = time.monotonic()
        if now < self.hub_retry_not_before:
            return False
        if now - self.last_heartbeat >= self.config.heartbeat_seconds:
            self.hub.heartbeat(self._server_status(), self._minecraft_status())
            self.last_heartbeat = now

        self._flush_events()
        for command in self.hub.get_commands():
            self._execute_queued_command(command)
        self._flush_events()
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
        delay = min(HUB_RETRY_MAX_SECONDS, float(2 ** min(self.hub_failure_count - 1, 4)))
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
        for line in self.log_tail.read_new_lines():
            self.startup.observe(line)
            if is_rcon_lifecycle_log(line):
                continue
            self.events.add("minecraft", line)

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
        result = self._run(["systemctl", "is-active", self.minecraft_service], timeout=5)
        active = result["stdout"].strip() == "active"
        self.startup.set_service_active(active)
        if active:
            self._refresh_rcon_runtime()
        else:
            self.online_players = []
            self.player_count = None
            self.player_limit = None
        return {
            "service": self.minecraft_service,
            "active": active,
            "state": result["stdout"].strip() or result["stderr"].strip(),
            "log_file": str(self.log_tail.path),
            "console_mode": self.console_mode,
            "startup": self.startup.snapshot(),
            "players": {
                "online": self.player_count,
                "max": self.player_limit,
                "names": self.online_players,
            },
            # ``help`` discovers additional commands supplied by this exact
            # modpack.  The desktop merges it with a safe vanilla fallback.
            "command_names": self.command_names,
        }

    def _refresh_rcon_runtime(self) -> None:
        """Confirm readiness and gather data used by the desktop console."""

        if self.console_mode != "rcon" or not self.rcon.password or self.rcon.password.startswith("REPLACE_"):
            return
        now = time.monotonic()
        if now - self.last_rcon_probe >= RCON_STATUS_INTERVAL_SECONDS:
            self.last_rcon_probe = now
            try:
                player_output = self.rcon.command("list")
            except Exception as error:
                self.startup.wait_for_rcon()
                self._stderr(f"RCON readiness check failed: {error}")
            else:
                self.player_count, self.player_limit, self.online_players = parse_minecraft_player_list(player_output)
                if self.player_count is None:
                    self.startup.mark_ready("RCON-консоль подключена")
                else:
                    self.startup.mark_ready(f"RCON подключён · игроков: {self.player_count}/{self.player_limit}")

        if self.startup.ready and now - self.last_command_list_refresh >= COMMAND_LIST_REFRESH_SECONDS:
            self.last_command_list_refresh = now
            try:
                discovered = parse_help_command_names(self.rcon.command("help"))
            except Exception as error:
                self._stderr(f"RCON help refresh failed: {error}")
            else:
                if discovered:
                    self.command_names = discovered

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
        # dragonfyre.service is allowed 120 seconds for a clean stop.  The
        # control agent must wait longer than systemd instead of failing after
        # its normal 45-second command timeout.
        timeout_seconds = 150 if action == "stop" else 45
        self._sudo_systemctl(action, self.minecraft_service, timeout_seconds)
        time.sleep(1)
        return self._minecraft_status()

    def _prepare_safe_power_off(self) -> dict[str, Any]:
        save_output = ""
        self.events.add("server", "[safe off] Начинаю безопасное выключение.")
        if self._minecraft_status()["active"]:
            self.events.add("server", "[safe off] Сохраняю мир и останавливаю Minecraft.")
            try:
                save_output = self._minecraft_command("save-all flush")
                self._minecraft_command("stop")
            except Exception as error:
                # A service stop still gives systemd a chance to shut down Minecraft cleanly.
                self.events.add("minecraft", f"[safe off] RCON недоступен: {error}; останавливаю службу.")
            self._minecraft_service_action("stop")

        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if not self._minecraft_status()["active"]:
                self.events.add("server", "[safe off] Minecraft остановлен, синхронизирую данные.")
                self._run(["sync"], timeout=15)
                return {"ready_for_power_off": True, "save_output": save_output[:1000]}
            time.sleep(2)
        raise RuntimeError("Minecraft не остановился за 120 секунд; питание не отключено")

    def _minecraft_command(self, command: str) -> str:
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

    def _sudo_systemctl(self, action: str, service: str | None = None, timeout_seconds: int = 45) -> None:
        if service is None:
            args = ["sudo", "-n", "/usr/bin/systemctl", action]
        else:
            args = ["sudo", "-n", "/usr/bin/systemctl", action, service]
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
