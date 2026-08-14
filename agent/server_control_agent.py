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
import shlex
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any


AGENT_VERSION = "1.0.0"
MAX_EVENT_MESSAGE = 8000


class HubError(RuntimeError):
    """A request to the control hub failed."""


@dataclass
class Config:
    hub_url: str
    agent_api_key: str
    poll_seconds: int
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
            poll_seconds=max(3, int(raw.get("poll_seconds", 6))),
            heartbeat_seconds=max(10, int(raw.get("heartbeat_seconds", 30))),
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


class EventBuffer:
    def __init__(self) -> None:
        self._events: deque[dict[str, str]] = deque()

    def add(self, kind: str, message: str) -> None:
        clean = message.strip()
        if clean:
            self._events.append({"kind": kind, "message": clean[:MAX_EVENT_MESSAGE]})

    def take(self, count: int = 100) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        while self._events and len(result) < count:
            result.append(self._events.popleft())
        return result

    def restore_front(self, events: list[dict[str, str]]) -> None:
        for event in reversed(events):
            self._events.appendleft(event)

    def __bool__(self) -> bool:
        return bool(self._events)


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


class RconClient:
    """Small, local-only implementation of the Minecraft RCON protocol."""

    AUTH = 3
    COMMAND = 2

    def __init__(self, host: str, port: int, password: str, timeout: float = 8.0) -> None:
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout

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

    def command(self, command: str) -> str:
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as connection:
            connection.settimeout(self.timeout)
            request_id = int(time.time() * 1000) & 0x7FFFFFFF
            connection.sendall(self._packet(request_id, self.AUTH, self.password))
            authenticated_id, _, _ = self._read_packet(connection)
            if authenticated_id == -1:
                raise RuntimeError("RCON authentication failed")
            connection.sendall(self._packet(request_id + 1, self.COMMAND, command))
            response_id, _, payload = self._read_packet(connection)
            if response_id == -1:
                raise RuntimeError("RCON command failed")
            return payload


class Agent:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.hub = HubClient(config)
        self.events = EventBuffer()
        minecraft = config.minecraft
        self.minecraft_service = str(minecraft.get("service", "minecraft-dragonfyre.service"))
        self.minecraft_directory = Path(str(minecraft.get("directory", "/opt/minecraft/dragonfyre")))
        self.log_tail = LogTail(Path(str(minecraft.get("log_file", self.minecraft_directory / "logs/latest.log"))))
        self.console_mode = str(minecraft.get("console_mode", "rcon"))
        self.rcon = RconClient(
            str(minecraft.get("rcon_host", "127.0.0.1")),
            int(minecraft.get("rcon_port", 25575)),
            str(minecraft.get("rcon_password", "")),
        )
        self.tmux_session = str(minecraft.get("tmux_session", "dragonfyre"))
        self.last_heartbeat = 0.0

    def run(self) -> None:
        self.events.add("server", f"Server Control agent {AGENT_VERSION} started.")
        while True:
            try:
                self._tick()
            except KeyboardInterrupt:
                raise
            except Exception as error:  # keep control access alive after a transient failure
                self._stderr(f"Agent tick failed: {error}")
                self.events.add("server", f"[agent] Ошибка: {error}")
            time.sleep(self.config.poll_seconds)

    def _tick(self) -> None:
        self._collect_minecraft_logs()
        now = time.monotonic()
        if now - self.last_heartbeat >= self.config.heartbeat_seconds:
            self.hub.heartbeat(self._server_status(), self._minecraft_status())
            self.last_heartbeat = now

        self._flush_events()
        for command in self.hub.get_commands():
            self._execute_queued_command(command)
        self._flush_events()

    def _collect_minecraft_logs(self) -> None:
        for line in self.log_tail.read_new_lines():
            self.events.add("minecraft", line)

    def _flush_events(self) -> None:
        while self.events:
            batch = self.events.take()
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
            command = str(payload.get("command", "")).strip()
            if not command or len(command) > 256:
                raise ValueError("Некорректная команда Minecraft")
            output = self._minecraft_command(command)
            self.events.add("minecraft", f"> {command}\n{output}".strip())
            return {"command": command, "output": output[:4000]}
        if command_type == "safe_power_off":
            return self._prepare_safe_power_off()
        raise ValueError(f"Неизвестная команда: {command_type}")

    def _server_status(self) -> dict[str, Any]:
        uptime = self._run(["uptime"], timeout=5)
        disk = self._run(["df", "-h", "/"], timeout=5)
        return {
            "hostname": socket.gethostname(),
            "uptime": uptime["stdout"].strip(),
            "disk": disk["stdout"].strip(),
            "agent_time": int(time.time()),
        }

    def _minecraft_status(self) -> dict[str, Any]:
        result = self._run(["systemctl", "is-active", self.minecraft_service], timeout=5)
        active = result["stdout"].strip() == "active"
        return {
            "service": self.minecraft_service,
            "active": active,
            "state": result["stdout"].strip() or result["stderr"].strip(),
            "log_file": str(self.log_tail.path),
            "console_mode": self.console_mode,
        }

    def _run_allowed_shell_command(self, command: str) -> dict[str, Any]:
        if not command or len(command) > 512:
            raise ValueError("Некорректная команда")
        if any(character in command for character in (";", "|", "&", ">", "<", "`", "$", "\n", "\r")):
            raise PermissionError("Командные цепочки и перенаправления запрещены")
        allowed_prefixes = self.config.commands.get("allow_shell_prefixes", [])
        if not isinstance(allowed_prefixes, list) or not any(
            command == prefix or command.startswith(f"{prefix} ") for prefix in allowed_prefixes if isinstance(prefix, str)
        ):
            raise PermissionError("Эта команда не внесена в allow-list агента")
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
        completed = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", **os.environ},
        )
        return {"returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}

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
