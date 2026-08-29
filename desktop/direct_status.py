"""Read the dashboard snapshot directly from the Debian host over SSH."""

from __future__ import annotations

import base64
import json
import shlex
import socket
import textwrap
import threading
import time
from typing import Any, Callable

import paramiko

from ssh_terminal import HostFingerprintPolicy, connection_targets, load_private_key


MAX_STATUS_BYTES = 256 * 1024

REMOTE_STATUS_PROGRAM = textwrap.dedent(
    r"""
    import glob
    import json
    import os
    import shutil
    import socket
    import struct
    import subprocess
    import time

    def read_text(path, limit=1048576):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as source:
                return source.read(limit)
        except OSError:
            return ""

    def cpu_ticks():
        fields = read_text("/proc/stat", 4096).splitlines()[0].split()[1:]
        values = [int(value) for value in fields]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return sum(values), idle

    def cpu_percent():
        try:
            first_total, first_idle = cpu_ticks()
            time.sleep(0.2)
            total, idle = cpu_ticks()
            delta = total - first_total
            return round(max(0.0, min(100.0, 100.0 * (delta - idle + first_idle) / delta)), 1) if delta > 0 else None
        except (IndexError, OSError, ValueError):
            return None

    def memory_status():
        values = {}
        for line in read_text("/proc/meminfo").splitlines():
            key, separator, payload = line.partition(":")
            if not separator:
                continue
            try:
                values[key] = int(payload.split()[0]) * 1024
            except (IndexError, ValueError):
                pass
        total = values.get("MemTotal", 0)
        available = values.get("MemAvailable", values.get("MemFree", 0))
        used = max(0, total - available)
        return {
            "total_bytes": total,
            "available_bytes": available,
            "used_bytes": used,
            "percent": round(used * 100 / total, 1) if total else None,
        }

    def temperature():
        candidates = []
        paths = glob.glob("/sys/class/thermal/thermal_zone*/temp")
        paths += glob.glob("/sys/class/hwmon/hwmon*/temp*_input")
        for path in paths:
            try:
                value = float(read_text(path, 128).strip())
            except ValueError:
                continue
            value = value / 1000 if value > 1000 else value
            if -20 <= value <= 150:
                candidates.append(value)
        return round(max(candidates), 1) if candidates else None

    def run(command, timeout=3):
        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"},
            )
            return result.returncode, result.stdout.decode("utf-8", "replace").strip()
        except (OSError, subprocess.SubprocessError):
            return 1, ""

    def varint(value):
        output = bytearray()
        while True:
            byte = value & 0x7f
            value >>= 7
            output.append(byte | (0x80 if value else 0))
            if not value:
                return bytes(output)

    def read_varint(stream):
        value = 0
        for offset in range(5):
            byte = stream.recv(1)
            if not byte:
                raise OSError("short Minecraft response")
            value |= (byte[0] & 0x7f) << (7 * offset)
            if not byte[0] & 0x80:
                return value
        raise OSError("invalid Minecraft response")

    def read_exact(stream, length):
        output = bytearray()
        while len(output) < length:
            chunk = stream.recv(length - len(output))
            if not chunk:
                raise OSError("short Minecraft response")
            output.extend(chunk)
        return bytes(output)

    def minecraft_ping(port):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.8) as stream:
                stream.settimeout(0.8)
                host = b"127.0.0.1"
                handshake = varint(0) + varint(763) + varint(len(host)) + host + struct.pack(">H", port) + varint(1)
                stream.sendall(varint(len(handshake)) + handshake + b"\x01\x00")
                read_varint(stream)
                if read_varint(stream) != 0:
                    raise OSError("unexpected Minecraft packet")
                payload = json.loads(read_exact(stream, read_varint(stream)).decode("utf-8", "replace"))
                players = payload.get("players") if isinstance(payload.get("players"), dict) else {}
                return True, players.get("online"), players.get("max")
        except (OSError, ValueError, json.JSONDecodeError):
            return False, None, None

    def minecraft_port():
        port = 25565
        for line in read_text("/opt/minecraft/dragonfyre/server.properties", 1048576).splitlines():
            if line.startswith("server-port="):
                try:
                    port = int(line.partition("=")[2].strip())
                except ValueError:
                    pass
        return port if 1 <= port <= 65535 else 25565

    try:
        uptime = max(0, int(float(read_text("/proc/uptime", 128).split()[0])))
    except (IndexError, ValueError):
        uptime = None
    try:
        loads = [round(value, 2) for value in os.getloadavg()]
    except OSError:
        loads = []
    disk = shutil.disk_usage("/")
    disk_used = disk.total - disk.free
    code, service_output = run([
        "systemctl", "show", "dragonfyre.service",
        "--property=ActiveState,SubState,MainPID", "--no-pager",
    ])
    service = {}
    if code == 0:
        for line in service_output.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                service[key] = value
    active_state = service.get("ActiveState", "unknown")
    sub_state = service.get("SubState", "unknown")
    port = minecraft_port()
    ready, online_players, maximum_players = minecraft_ping(port)
    if active_state == "failed":
        minecraft_state = "CRASHED"
    elif active_state == "deactivating":
        minecraft_state = "STOPPING"
    elif active_state in ("active", "activating"):
        minecraft_state = "RUNNING" if ready else "STARTING"
    else:
        minecraft_state = "STOPPED"
    startup = {
        "RUNNING": {"progress": 100, "label": "Сервер принимает подключения", "ready": True},
        "STARTING": {"progress": 60, "label": "Запуск Minecraft", "ready": False},
        "STOPPING": {"progress": 85, "label": "Остановка Minecraft", "ready": False},
        "CRASHED": {"progress": 0, "label": "Ошибка службы", "ready": False},
        "STOPPED": {"progress": 0, "label": "Служба остановлена", "ready": False},
    }[minecraft_state]
    collected_at = int(time.time() * 1000)
    snapshot = {
        "hostname": socket.gethostname(),
        "ip_addresses": run(["hostname", "-I"], timeout=2)[1].split(),
        "metrics": {
            "cpu": {"percent": cpu_percent(), "load_average": loads},
            "memory": memory_status(),
            "filesystem": {
                "mount": "/", "total_bytes": disk.total, "available_bytes": disk.free,
                "used_bytes": disk_used,
                "percent": round(disk_used * 100 / disk.total, 1) if disk.total else None,
            },
            "temperature_celsius": temperature(),
            "uptime_seconds": uptime,
            "collected_at": collected_at,
        },
        "minecraft": {
            "id": "dragonfyre", "name": "Dragonfyre", "service": "dragonfyre.service",
            "active": active_state in ("active", "activating", "deactivating"),
            "state": minecraft_state, "service_state": active_state, "service_sub_state": sub_state,
            "pid": int(service.get("MainPID", "0") or 0) or None,
            "port": port, "startup": startup,
            "players": {"online": online_players, "max": maximum_players, "names": []},
        },
        "collected_at": collected_at,
    }
    print(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")))
    """
).strip()

_REMOTE_CODE = "import base64;exec(compile(base64.b64decode(" + repr(
    base64.b64encode(REMOTE_STATUS_PROGRAM.encode("utf-8")).decode("ascii")
) + "),'<server-control-status>','exec'))"
REMOTE_STATUS_COMMAND = "sudo -n /usr/bin/python3 -c " + shlex.quote(_REMOTE_CODE)


def dashboard_envelope(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Convert the fixed SSH result to the dashboard's existing data shape."""

    collected_at = int(snapshot.get("collected_at") or time.time() * 1000)
    minecraft = snapshot.get("minecraft") if isinstance(snapshot.get("minecraft"), dict) else {}
    return {
        "online": True,
        "updated_at": collected_at,
        "age_ms": max(0, int(time.time() * 1000) - collected_at),
        "source": "direct_ssh",
        "status": {
            "source": "direct_ssh",
            "server": {
                "hostname": str(snapshot.get("hostname") or "ChipdanServer"),
                "metrics": snapshot.get("metrics") if isinstance(snapshot.get("metrics"), dict) else {},
            },
            "minecraft": minecraft,
            "instances": [minecraft] if minecraft else [],
            "selected_instance_id": "dragonfyre" if minecraft else None,
            "system": {
                "ip_addresses": snapshot.get("ip_addresses")
                if isinstance(snapshot.get("ip_addresses"), list)
                else [],
            },
        },
    }


class DirectSshStatusClient:
    """Keep one verified SSH transport and open a short status channel per poll."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._client: paramiko.SSHClient | None = None
        self._target = ""

    def close(self) -> None:
        with self._lock:
            client, self._client = self._client, None
            self._target = ""
        if client:
            client.close()

    def _connect(self, credentials: dict[str, Any]) -> None:
        values = dict(credentials)
        username = str(values.get("username", ""))
        fingerprint = str(values.get("host_key_sha256", ""))
        private_key = str(values.pop("private_key", ""))
        pkey = load_private_key(private_key)
        private_key = ""
        last_error: Exception | None = None
        for host, port in connection_targets(values):
            candidate = paramiko.SSHClient()
            candidate.set_missing_host_key_policy(HostFingerprintPolicy(fingerprint))
            try:
                candidate.connect(
                    hostname=host,
                    port=port,
                    username=username,
                    pkey=pkey,
                    allow_agent=False,
                    look_for_keys=False,
                    timeout=10,
                    banner_timeout=10,
                    auth_timeout=10,
                    channel_timeout=12,
                    compress=True,
                )
            except Exception as error:
                last_error = error
                candidate.close()
                continue
            transport = candidate.get_transport()
            if not transport or not transport.is_active():
                candidate.close()
                last_error = paramiko.SSHException("SSH-транспорт не запустился.")
                continue
            transport.set_keepalive(15)
            self._client = candidate
            self._target = f"{host}:{port}"
            values.clear()
            return
        values.clear()
        raise last_error or paramiko.SSHException("Нет доступного SSH-адреса.")

    def _execute(self) -> dict[str, Any]:
        client = self._client
        transport = client.get_transport() if client else None
        if not client or not transport or not transport.is_active():
            raise paramiko.SSHException("SSH-соединение закрыто.")
        _stdin, stdout, stderr = client.exec_command(REMOTE_STATUS_COMMAND, timeout=15)
        raw = stdout.read(MAX_STATUS_BYTES + 1)
        error = stderr.read(64 * 1024).decode("utf-8", "replace").strip()
        code = stdout.channel.recv_exit_status()
        if len(raw) > MAX_STATUS_BYTES:
            raise ValueError("Сервер вернул слишком большой ответ состояния.")
        if code != 0:
            raise RuntimeError(error or f"Команда состояния завершилась с кодом {code}.")
        try:
            value = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:
            raise ValueError("Сервер вернул некорректное состояние.") from exc
        if not isinstance(value, dict):
            raise ValueError("Сервер вернул некорректное состояние.")
        return dashboard_envelope(value)

    def _restart_minecraft(self) -> dict[str, Any]:
        client = self._client
        transport = client.get_transport() if client else None
        if not client or not transport or not transport.is_active():
            raise paramiko.SSHException("SSH-соединение закрыто.")
        _stdin, stdout, stderr = client.exec_command(
            "sudo -n /usr/bin/systemctl restart dragonfyre.service",
            timeout=180,
        )
        output = stdout.read(64 * 1024).decode("utf-8", "replace").strip()
        error = stderr.read(64 * 1024).decode("utf-8", "replace").strip()
        code = stdout.channel.recv_exit_status()
        if code != 0:
            raise RuntimeError(error or output or f"Перезапуск завершился с кодом {code}.")
        return {"ok": True}

    def snapshot(self, credentials: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        with self._lock:
            for attempt in range(2):
                try:
                    if self._client is None:
                        self._connect(credentials())
                    return self._execute()
                except (OSError, socket.timeout, paramiko.SSHException, EOFError) as error:
                    if self._client:
                        self._client.close()
                    self._client = None
                    self._target = ""
                    if attempt:
                        raise RuntimeError(f"Прямой SSH недоступен: {error}") from error
            raise RuntimeError("Прямой SSH недоступен.")

    def restart_minecraft(self, credentials: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """Restart only the fixed Dragonfyre service over the verified SSH connection."""

        with self._lock:
            for attempt in range(2):
                try:
                    if self._client is None:
                        self._connect(credentials())
                    return self._restart_minecraft()
                except (OSError, socket.timeout, paramiko.SSHException, EOFError) as error:
                    if self._client:
                        self._client.close()
                    self._client = None
                    self._target = ""
                    if attempt:
                        raise RuntimeError(f"Прямой SSH недоступен: {error}") from error
            raise RuntimeError("Прямой SSH недоступен.")
