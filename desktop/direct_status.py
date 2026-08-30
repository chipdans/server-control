"""Read the dashboard snapshot directly from the Debian host over SSH."""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import socket
import textwrap
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import paramiko

from direct_instances import MAX_MANAGER_RESPONSE_BYTES, manager_command
from ssh_terminal import HostFingerprintPolicy, connection_targets, load_private_key


MAX_STATUS_BYTES = 256 * 1024

REMOTE_STATUS_PROGRAM = textwrap.dedent(
    r"""
    import glob
    import gzip
    import json
    import os
    import re
    import shutil
    import socket
    import struct
    import subprocess
    import time
    from pathlib import Path

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

    def read_log_start(path, max_chars=25165824):
        lines = []
        consumed = 0
        try:
            opener = gzip.open if str(path).endswith(".gz") else open
            with opener(path, "rt", encoding="utf-8", errors="replace") as source:
                for line in source:
                    consumed += len(line)
                    if consumed > max_chars:
                        break
                    value = line.rstrip("\r\n")
                    lines.append(value)
                    lowered = value.casefold()
                    if "done (" in lowered and "for help, type" in lowered:
                        break
        except (OSError, EOFError, gzip.BadGzipFile):
            return []
        return lines

    def normalize_startup_line(line):
        value = line.strip()
        value = re.sub(r"^\[[^\]]*(?:\d{1,2}:){2}[^\]]*\]\s*", "", value)
        value = re.sub(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
            "<uuid>", value, flags=re.IGNORECASE,
        )
        value = re.sub(r"@[0-9a-f]{6,}\b", "@<id>", value, flags=re.IGNORECASE)
        value = re.sub(
            r"\b\d+(?:\.\d+)?\s*(?:ms|milliseconds?|seconds?|secs?)\b",
            "<time>", value, flags=re.IGNORECASE,
        )
        value = re.sub(r"\s+", " ", value).strip()
        return value[:600]

    def select_startup_markers(lines, count=100):
        unique = []
        seen = set()
        ignored = (
            "preparing spawn area:", "preparing start region for dimension",
            "joined the game", "left the game", "lost connection", "saving chunks",
            "thread rcon client", "rcon listener", "stopping server",
        )
        for line in lines:
            lowered = line.casefold()
            if any(marker in lowered for marker in ignored):
                continue
            value = normalize_startup_line(line)
            if len(value) < 24 or value in seen:
                continue
            seen.add(value)
            unique.append(value)
        if len(unique) <= count:
            return unique
        indexes = [round(index * (len(unique) - 1) / (count - 1)) for index in range(count)]
        return [unique[index] for index in indexes]

    def completed_startup(lines):
        return any("done (" in line.casefold() and "for help, type" in line.casefold() for line in lines)

    def startup_reference(log_directory, current_lines):
        candidates = []
        try:
            candidates = sorted(
                [path for path in log_directory.iterdir() if path.name != "latest.log" and path.is_file() and path.suffix in (".log", ".gz")],
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )[:12]
        except OSError:
            pass
        for path in candidates:
            lines = read_log_start(path)
            if completed_startup(lines):
                return select_startup_markers(lines)
        if completed_startup(current_lines):
            return select_startup_markers(current_lines)
        return []

    def startup_from_log(current_lines, reference, ready, start_id):
        if ready:
            return {
                "progress": 100, "label": "Сервер принимает подключения", "ready": True,
                "detail": "Minecraft полностью запущен", "milestones_matched": len(reference),
                "milestones_total": len(reference), "start_id": start_id,
            }
        normalized = {normalize_startup_line(line) for line in current_lines if line.strip()}
        matched = sum(1 for marker in reference if marker in normalized)
        progress = 2
        if reference:
            progress = max(progress, min(97, 2 + round(matched * 95 / len(reference))))
        text = "\n".join(current_lines).casefold()
        phase = "Запускаю Java"
        if any(marker in text for marker in ("modlauncher running", "modlauncher", "fml loader")):
            progress, phase = max(progress, 7), "Запускаю Forge"
        if any(marker in text for marker in ("found mod file", "moddiscoverer", "loading mod list")):
            progress, phase = max(progress, 18), "Сканирую моды"
        if any(marker in text for marker in ("constructing mods", "loading mod", "common_setup", "modloading")):
            progress, phase = max(progress, 42), "Инициализирую моды"
        if any(marker in text for marker in ("gamedata", "registries", "registering", "registry")):
            progress, phase = max(progress, 66), "Регистрирую содержимое"
        if any(marker in text for marker in ("starting minecraft server version", "starting minecraft server")):
            progress, phase = max(progress, 78), "Запускаю Minecraft"
        if "preparing level" in text or "loading level" in text:
            progress, phase = max(progress, 84), "Загружаю мир"
        spawn_values = [int(value) for value in re.findall(r"preparing (?:spawn area|start region).*?(\d{1,3})%", text)]
        if spawn_values:
            spawn = max(0, min(100, max(spawn_values)))
            progress, phase = max(progress, 85 + round(spawn * 0.14)), "Подготавливаю спавн"
        progress = max(1, min(99, progress))
        detail = (
            f"Контрольные сообщения запуска: {matched}/{len(reference)}"
            if reference else "Собираю контрольные сообщения нового запуска"
        )
        return {
            "progress": progress, "label": phase, "ready": False, "detail": detail,
            "milestones_matched": matched, "milestones_total": len(reference), "start_id": start_id,
        }

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

    def active_minecraft_profile():
        fallback = {
            "id": "dragonfyre", "name": "Dragonfyre",
            "directory": "/opt/minecraft/dragonfyre", "port": 25565,
        }
        path = Path("/etc/server-control/minecraft-instances.json")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return fallback
        values = data.get("instances") if isinstance(data.get("instances"), list) else []
        active = str(data.get("active") or "")
        selected = next(
            (item for item in values if isinstance(item, dict) and str(item.get("id") or "") == active),
            None,
        )
        if not selected:
            return fallback
        directory = Path(str(selected.get("directory") or fallback["directory"]))
        try:
            directory.resolve(strict=False).relative_to(Path("/opt/minecraft").resolve(strict=False))
        except ValueError:
            return fallback
        return {
            "id": str(selected.get("id") or fallback["id"]),
            "name": str(selected.get("name") or selected.get("id") or fallback["name"]),
            "directory": str(directory),
            "port": selected.get("port", fallback["port"]),
        }

    def minecraft_port(directory, configured):
        port = 25565
        try:
            port = int(configured)
        except (TypeError, ValueError):
            pass
        for line in read_text(str(directory / "server.properties"), 1048576).splitlines():
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
        "--property=ActiveState,SubState,MainPID,ExecMainStartTimestampMonotonic,Result,NRestarts,ExecMainStatus", "--no-pager",
    ])
    service = {}
    if code == 0:
        for line in service_output.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                service[key] = value
    active_state = service.get("ActiveState", "unknown")
    sub_state = service.get("SubState", "unknown")
    try:
        restart_count = int(service.get("NRestarts", "0") or 0)
    except ValueError:
        restart_count = 0
    try:
        exit_status = int(service.get("ExecMainStatus", "0") or 0)
    except ValueError:
        exit_status = 0
    restart_loop = sub_state == "auto-restart" and restart_count > 0
    active_profile = active_minecraft_profile()
    minecraft_directory = Path(active_profile["directory"])
    port = minecraft_port(minecraft_directory, active_profile.get("port"))
    ready, online_players, maximum_players = minecraft_ping(port)
    try:
        start_id = int(service.get("ExecMainStartTimestampMonotonic", "0") or 0)
    except ValueError:
        start_id = 0
    log_directory = minecraft_directory / "logs"
    latest_log = log_directory / "latest.log"
    current_lines = read_log_start(latest_log)
    if start_id:
        try:
            boot_epoch = time.time() - float(read_text("/proc/uptime", 128).split()[0])
            service_started_epoch = boot_epoch + start_id / 1000000
            if latest_log.stat().st_mtime + 1 < service_started_epoch:
                current_lines = []
        except (OSError, IndexError, ValueError):
            pass
    reference = startup_reference(log_directory, current_lines)
    if active_state == "failed" or restart_loop:
        minecraft_state = "CRASHED"
    elif active_state == "deactivating":
        minecraft_state = "STOPPING"
    elif active_state in ("active", "activating"):
        minecraft_state = "RUNNING" if ready else "STARTING"
    else:
        minecraft_state = "STOPPED"
    if minecraft_state in ("RUNNING", "STARTING"):
        startup = startup_from_log(current_lines, reference, ready, start_id)
    else:
        startup = {
            "STOPPING": {"progress": 85, "label": "Остановка Minecraft", "ready": False},
            "CRASHED": {
                "progress": 0,
                "label": "Служба перезапускается после кода " + str(exit_status) if restart_loop else "Ошибка службы",
                "ready": False,
            },
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
            "id": active_profile["id"], "name": active_profile["name"],
            "directory": str(minecraft_directory), "service": "dragonfyre.service",
            "active": active_state in ("active", "activating", "deactivating") and not restart_loop,
            "state": minecraft_state, "service_state": active_state, "service_sub_state": sub_state,
            "service_result": service.get("Result", "unknown"), "restart_count": restart_count,
            "exit_status": exit_status, "restart_loop": restart_loop,
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
            "selected_instance_id": str(minecraft.get("id")) if minecraft.get("id") else None,
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
        self._startup_id: int | str | None = None
        self._startup_progress = 0

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
        return self._stabilize_startup(dashboard_envelope(value))

    def _stabilize_startup(self, payload: dict[str, Any]) -> dict[str, Any]:
        status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
        minecraft = status.get("minecraft") if isinstance(status.get("minecraft"), dict) else {}
        startup = minecraft.get("startup") if isinstance(minecraft.get("startup"), dict) else {}
        state = str(minecraft.get("state") or "").upper()
        if state == "STARTING":
            start_id = startup.get("start_id")
            if start_id != self._startup_id:
                self._startup_id = start_id
                self._startup_progress = 0
            try:
                current = int(startup.get("progress", 0) or 0)
            except (TypeError, ValueError):
                current = 0
            self._startup_progress = max(self._startup_progress, current)
            startup["progress"] = self._startup_progress
        elif state == "RUNNING":
            self._startup_id = startup.get("start_id")
            self._startup_progress = 100
            startup["progress"] = 100
        else:
            self._startup_id = None
            self._startup_progress = 0
        return payload

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

    def _execute_instance_request(self, payload: dict[str, Any], timeout: int = 900) -> dict[str, Any]:
        client = self._client
        transport = client.get_transport() if client else None
        if not client or not transport or not transport.is_active():
            raise paramiko.SSHException("SSH-соединение закрыто.")
        _stdin, stdout, stderr = client.exec_command(manager_command(payload), timeout=max(30, timeout))
        raw = stdout.read(MAX_MANAGER_RESPONSE_BYTES + 1)
        error = stderr.read(256 * 1024).decode("utf-8", "replace").strip()
        code = stdout.channel.recv_exit_status()
        if len(raw) > MAX_MANAGER_RESPONSE_BYTES:
            raise ValueError("Менеджер сборок вернул слишком большой ответ.")
        if code != 0:
            raise RuntimeError(error or f"Операция со сборкой завершилась с кодом {code}.")
        try:
            result = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:
            raise ValueError("Менеджер сборок вернул некорректный ответ.") from exc
        if not isinstance(result, dict):
            raise ValueError("Менеджер сборок вернул некорректный ответ.")
        return result

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

    def instance_request(
        self,
        credentials: Callable[[], dict[str, Any]],
        payload: dict[str, Any],
        *,
        timeout: int = 900,
    ) -> dict[str, Any]:
        """Run one instance-manager operation directly over the admin SSH channel."""

        with self._lock:
            try:
                if self._client is None:
                    self._connect(credentials())
                return self._execute_instance_request(dict(payload), timeout)
            except (OSError, socket.timeout, paramiko.SSHException, EOFError) as error:
                if self._client:
                    self._client.close()
                self._client = None
                self._target = ""
                raise RuntimeError(
                    "SSH-соединение прервалось. Обновите список сборок, чтобы проверить результат операции."
                ) from error

    def import_instance_archive(
        self,
        credentials: Callable[[], dict[str, Any]],
        archive: str | Path,
        payload: dict[str, Any],
        *,
        progress: Callable[[int, int], None] | None = None,
    ) -> dict[str, Any]:
        """Upload one server ZIP over SFTP and import it into an isolated directory."""

        source = Path(archive).resolve(strict=True)
        if source.suffix.casefold() != ".zip":
            raise ValueError("Выберите серверный ZIP-архив.")
        if source.stat().st_size > 12 * 1024 * 1024 * 1024:
            raise ValueError("ZIP больше допустимого размера 12 GB.")
        with self._lock:
            if self._client is None:
                self._connect(credentials())
            client = self._client
            transport = client.get_transport() if client else None
            if not client or not transport or not transport.is_active():
                raise paramiko.SSHException("SSH-соединение закрыто.")
            sftp = client.open_sftp()
            remote_path = ""
            try:
                home = sftp.normalize(".").rstrip("/")
                upload_directory = f"{home}/.server-control-upload"
                try:
                    sftp.mkdir(upload_directory, mode=0o700)
                except OSError:
                    pass
                remote_path = f"{upload_directory}/server-control-{uuid.uuid4().hex}.zip"
                sftp.put(str(source), remote_path, callback=progress, confirm=True)
                request = dict(payload)
                request.update({"action": "import_zip", "archive": remote_path})
                return self._execute_instance_request(request, timeout=24 * 60 * 60)
            finally:
                if remote_path:
                    try:
                        sftp.remove(remote_path)
                    except OSError:
                        pass
                sftp.close()

    def export_translation_archive(
        self,
        credentials: Callable[[], dict[str, Any]],
        instance_id: str,
        destination: str | Path,
        *,
        progress: Callable[[int, int], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
        paused: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Build the translation report remotely and download it over SFTP."""

        target = Path(destination).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".part-" + uuid.uuid4().hex)
        remote_path = ""
        sftp = None
        with self._lock:
            try:
                if self._client is None:
                    self._connect(credentials())
                client = self._client
                transport = client.get_transport() if client else None
                if not client or not transport or not transport.is_active():
                    raise paramiko.SSHException("SSH-соединение закрыто.")
                scan = self._execute_instance_request(
                    {"action": "translation_scan", "id": str(instance_id)},
                    timeout=60 * 60,
                )
                remote_path = str(scan.get("archive") or "")
                if not re.fullmatch(r"/var/tmp/server-control-translation-[0-9a-f]{32}\.zip", remote_path):
                    raise ValueError("Сервер вернул некорректный путь архива перевода.")
                if cancelled and cancelled():
                    raise RuntimeError("Выгрузка перевода отменена.")
                sftp = client.open_sftp()
                remote_size = int(sftp.stat(remote_path).st_size)
                if remote_size < 1 or remote_size > 512 * 1024 * 1024:
                    raise ValueError("Некорректный размер архива перевода.")

                def transferred(current: int, total: int) -> None:
                    while paused and paused():
                        if cancelled and cancelled():
                            raise RuntimeError("Выгрузка перевода отменена.")
                        time.sleep(0.1)
                    if cancelled and cancelled():
                        raise RuntimeError("Выгрузка перевода отменена.")
                    if progress:
                        progress(current, total)

                sftp.get(remote_path, str(temporary), callback=transferred)
                if temporary.stat().st_size != remote_size:
                    raise IOError("Архив перевода скачан не полностью.")
                os.replace(temporary, target)
                scan["local_path"] = str(target)
                scan["size"] = remote_size
                return scan
            finally:
                if sftp is not None:
                    try:
                        sftp.close()
                    except OSError:
                        pass
                if remote_path:
                    try:
                        self._execute_instance_request(
                            {"action": "translation_cleanup", "archive": remote_path},
                            timeout=60,
                        )
                    except Exception:
                        pass
                try:
                    temporary.unlink()
                except OSError:
                    pass
