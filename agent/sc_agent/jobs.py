"""Non-blocking job executor for every long-running Agent operation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
import uuid
import zipfile
from pathlib import Path
from typing import Any, Callable

from .backups import BackupManager
from .files import FileManager
from .instances import InstanceProfile, InstanceStore, detect_pack
from .security import (
    PathPolicy,
    SecurityError,
    safe_extract_zip,
    secure_path_within,
    sha256_file,
    validate_filename,
    validate_instance_id,
)


MAX_VANILLA_SERVER_JAR_BYTES = 512 * 1024 * 1024
MAX_LOG_VIEW_BYTES = 2 * 1024 * 1024


class JobCancelled(RuntimeError):
    pass


def read_tail_bytes(path: Path, limit: int = MAX_LOG_VIEW_BYTES) -> tuple[bytes, bool]:
    """Read a bounded suffix without ever materialising a multi-GiB log."""

    size = path.stat().st_size
    start = max(0, size - max(1, int(limit)))
    with path.open("rb") as source:
        source.seek(start)
        data = source.read(max(1, int(limit)))
    if start:
        _partial, separator, remainder = data.partition(b"\n")
        data = remainder if separator else b""
    return data, start > 0


def compatible_java_major(minecraft_version: str) -> int | None:
    match = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", str(minecraft_version))
    if not match:
        return None
    major, minor, patch = (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))
    if major != 1:
        return 21
    if minor <= 16:
        return 8
    if minor == 17:
        return 16
    if minor < 20 or (minor == 20 and patch <= 4):
        return 17
    return 21


class JobExecutor:
    def __init__(
        self,
        *,
        hub: Any,
        instances: InstanceStore,
        backups: BackupManager,
        service_action: Callable[[str, str, int], dict[str, Any]],
        instance_status: Callable[[str], dict[str, Any]],
        minecraft_command: Callable[[str, str], str],
        server_action: Callable[[str], dict[str, Any]],
        service_control: Callable[[str, str], dict[str, Any]],
        agent_update: Callable[[dict[str, Any]], dict[str, Any]],
        event: Callable[..., None],
        max_workers: int = 2,
    ) -> None:
        self.hub = hub
        self.instances = instances
        self.backups = backups
        self.service_action = service_action
        self.instance_status = instance_status
        self.minecraft_command = minecraft_command
        self.server_action = server_action
        self.service_control = service_control
        self.agent_update = agent_update
        self.event = event
        self.max_workers = max(1, min(int(max_workers), 4))
        self._lock = threading.Lock()
        self._running: dict[str, tuple[threading.Thread, threading.Event]] = {}
        self._last_progress: dict[str, tuple[int, float]] = {}

    def _instance_directory(self, profile: InstanceProfile, *, must_exist: bool = False) -> Path:
        return secure_path_within(self.instances.minecraft_root, profile.directory, must_exist=must_exist)

    def running_ids(self) -> set[str]:
        with self._lock:
            return set(self._running)

    def submit(self, job: dict[str, Any]) -> bool:
        job_id = str(job.get("id", ""))
        if not job_id:
            return False
        with self._lock:
            if job_id in self._running or len(self._running) >= self.max_workers:
                return False
            cancel = threading.Event()
            thread = threading.Thread(target=self._run, args=(job, cancel), name=f"server-control-job-{job_id[:8]}", daemon=True)
            self._running[job_id] = (thread, cancel)
            thread.start()
            return True

    def cancel(self, job_ids: list[str]) -> None:
        with self._lock:
            for job_id in job_ids:
                item = self._running.get(str(job_id))
                if item:
                    item[1].set()

    def _cancelled(self, event: threading.Event) -> bool:
        return event.is_set()

    def _progress(self, job_id: str, value: int, stage: str, message: str = "", *, force: bool = False) -> None:
        progress = max(0, min(100, int(value)))
        now = time.monotonic()
        previous = self._last_progress.get(job_id)
        if not force and previous and progress == previous[0] and now - previous[1] < 2:
            return
        if not force and previous and abs(progress - previous[0]) < 1 and now - previous[1] < 0.75:
            return
        if self.hub.job_progress(job_id, progress, stage, message):
            raise JobCancelled("Операция отменена пользователем")
        self._last_progress[job_id] = (progress, now)

    def _run(self, job: dict[str, Any], cancel: threading.Event) -> None:
        job_id = str(job["id"])
        job_type = str(job.get("type", ""))
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        instance_id = str(payload.get("instance_id") or job.get("instance_id") or "")
        self.event("server", f"[job {job_id[:8]}] {job_type}: начато.", source="jobs", instance_id=instance_id or None)
        try:
            self._progress(job_id, 0, "starting", "Начинаю операцию", force=True)
            result = self._dispatch(job_id, job_type, payload, cancel)
            if cancel.is_set():
                raise JobCancelled("Операция отменена")
            self.hub.complete_job(job_id, "completed", result, "Операция выполнена.")
            self.event("server", f"[job {job_id[:8]}] {job_type}: выполнено.", source="jobs", instance_id=instance_id or None)
        except (JobCancelled, InterruptedError) as error:
            self.hub.complete_job(job_id, "cancelled", {"message": str(error)}, str(error), "cancelled")
            self.event("server", f"[job {job_id[:8]}] {job_type}: отменено.", source="jobs", instance_id=instance_id or None, level="WARN")
        except Exception as error:  # boundary: every job must report a terminal state
            code = self._error_code(error)
            message = str(error).strip()[:2000] or error.__class__.__name__
            try:
                self.hub.complete_job(job_id, "failed", {"message": message}, message, code)
            finally:
                self.event("server", f"[job {job_id[:8]}] {job_type}: ошибка: {message}", source="jobs", instance_id=instance_id or None, level="ERROR")
        finally:
            with self._lock:
                self._running.pop(job_id, None)
                self._last_progress.pop(job_id, None)

    @staticmethod
    def _error_code(error: Exception) -> str:
        if isinstance(error, SecurityError):
            return "security_violation"
        if isinstance(error, PermissionError):
            return "permission_denied"
        if isinstance(error, FileNotFoundError):
            return "not_found"
        if isinstance(error, FileExistsError):
            return "already_exists"
        if isinstance(error, TimeoutError):
            return "timeout"
        if isinstance(error, zipfile.BadZipFile):
            return "invalid_zip"
        if isinstance(error, subprocess.TimeoutExpired):
            return "subprocess_timeout"
        if isinstance(error, OSError) and getattr(error, "errno", None) == 28:
            return "disk_full"
        return "operation_failed"

    def _dispatch(self, job_id: str, job_type: str, payload: dict[str, Any], cancel: threading.Event) -> dict[str, Any]:
        if cancel.is_set():
            raise JobCancelled("Операция отменена до запуска")
        if job_type.startswith("file_"):
            return self._file_job(job_id, job_type, payload, cancel)
        if job_type.startswith("backup_"):
            return self._backup_job(job_id, job_type, payload, cancel)
        if job_type.startswith("instance_"):
            return self._instance_job(job_id, job_type, payload, cancel)
        if job_type == "minecraft_command":
            instance_id = validate_instance_id(payload.get("instance_id"))
            command = str(payload.get("command", "")).strip().lstrip("/")
            if not command or len(command) > 512 or any(character in command for character in "\r\n\0"):
                raise ValueError("Некорректная команда Minecraft")
            output = self.minecraft_command(instance_id, command)
            return {"command": command, "output": output[:64_000]}
        if job_type == "player_action":
            return self._player_action(payload)
        if job_type == "log_read":
            return self._read_log(payload)
        if job_type == "service_action":
            return self.service_control(str(payload.get("service", "")), str(payload.get("action", "status")))
        if job_type in {"server_reboot", "server_shutdown"}:
            return self.server_action("reboot" if job_type.endswith("reboot") else "shutdown")
        if job_type == "agent_update":
            return self.agent_update(payload)
        if job_type in {"transfer_import", "transfer_export"}:
            return self._transfer_job(job_id, job_type, payload, cancel)
        raise ValueError(f"Неизвестный тип операции: {job_type}")

    def _file_job(self, job_id: str, job_type: str, payload: dict[str, Any], cancel: threading.Event) -> dict[str, Any]:
        profile = self.instances.get(payload.get("instance_id"))
        manager = FileManager(self._instance_directory(profile, must_exist=True))
        if job_type == "file_list":
            return manager.list_directory(
                str(payload.get("path", "")), page=int(payload.get("page", 1)), page_size=int(payload.get("page_size", 200)),
                sort_by=str(payload.get("sort_by", "name")), descending=bool(payload.get("descending")), query=str(payload.get("query", "")),
            )
        if job_type == "file_read":
            return manager.read_text(str(payload.get("path", "")))
        if job_type == "file_write":
            return manager.write_text(
                str(payload.get("path", "")), str(payload.get("content", "")), encoding=str(payload.get("encoding", "utf-8")),
                expected_mtime_ns=int(payload["expected_mtime_ns"]) if payload.get("expected_mtime_ns") is not None else None,
            )
        if job_type == "file_search":
            return manager.search(
                str(payload.get("path", "")), str(payload.get("query", "")), pattern=str(payload.get("pattern", "*")),
                include_content=bool(payload.get("include_content")), cancelled=lambda: cancel.is_set(),
            )
        if job_type == "file_operation":
            def progress(value: int, detail: str) -> None:
                self._progress(job_id, value, "file_operation", detail)

            return manager.operation(
                str(payload.get("action", "")), str(payload.get("path", "")), destination=str(payload.get("destination", "")),
                name=str(payload.get("name", "")), cancelled=lambda: cancel.is_set(), progress=progress,
            )
        raise ValueError("Неизвестная файловая операция")

    def _backup_job(self, job_id: str, job_type: str, payload: dict[str, Any], cancel: threading.Event) -> dict[str, Any]:
        instance_id = validate_instance_id(payload.get("instance_id"))
        profile = self.instances.get(instance_id)

        def progress(value: int, detail: str) -> None:
            self._progress(job_id, value, "backup", detail)

        def create(reason: str, comment: str = "") -> dict[str, Any]:
            instance_directory = self._instance_directory(profile, must_exist=True)
            status = self.instance_status(instance_id)
            return self.backups.create(
                instance_id, instance_directory, comment=comment, reason=reason,
                minecraft_running=bool(status.get("active")), rcon=lambda command: self.minecraft_command(instance_id, command),
                progress=progress, cancelled=lambda: cancel.is_set(), settings=profile.backup,
            )

        if job_type == "backup_list":
            return {"backups": self.backups.list(instance_id)}
        if job_type == "backup_create":
            return {"backup": create(str(payload.get("reason", "manual")), str(payload.get("comment", "")))}
        if job_type == "backup_delete":
            return self.backups.delete(instance_id, str(payload.get("backup_id", "")))
        if job_type == "backup_restore":
            instance_directory = self._instance_directory(profile, must_exist=True)

            def stop() -> None:
                self.service_action(instance_id, "stop", 180)

            def safety() -> dict[str, Any]:
                return self.backups.create(
                    instance_id, instance_directory, comment="Автоматический safety backup перед restore", reason="pre_restore",
                    minecraft_running=False, progress=lambda value, detail: self._progress(job_id, 3 + int(value * 0.04), "safety_backup", detail),
                    cancelled=lambda: cancel.is_set(), settings=profile.backup,
                    preserve_backup_ids={str(payload.get("backup_id", ""))},
                )

            return self.backups.restore(
                instance_id, str(payload.get("backup_id", "")), instance_directory, stop=stop,
                is_running=lambda: bool(self.instance_status(instance_id).get("active")), safety_backup=safety,
                progress=progress, cancelled=lambda: cancel.is_set(),
            )
        if job_type == "backup_duplicate":
            new_id = validate_instance_id(payload.get("new_instance_id"))
            source = self.instances.get(instance_id)
            destination = self.instances.minecraft_root / new_id
            try:
                result = self.backups.duplicate_to(instance_id, str(payload.get("backup_id", "")), destination, progress=progress, cancelled=lambda: cancel.is_set())
                profile_data = source.to_disk()
                profile_data.update({"id": new_id, "name": str(payload.get("name") or new_id), "directory": str(destination), "service": f"server-control-minecraft@{new_id}.service", "managed_service": True, "launch_count": 0, "last_launch_at": None, "installed_at": int(time.time())})
                new_profile = InstanceProfile.from_dict(profile_data)
                new_profile.port, new_profile.rcon_port = self._next_ports(source.port, source.rcon_port)
                new_profile.rcon_password = secrets.token_urlsafe(32)
                self._validate_profile_ports(new_profile)
                self._configure_server_properties(new_profile)
                self.instances.put(new_profile, replace=False)
                return {"instance": new_profile.to_public(), **result}
            except Exception:
                if destination.exists():
                    shutil.rmtree(destination, ignore_errors=True)
                raise
        if job_type == "backup_export":
            transfer_id = str(payload.get("transfer_id", ""))
            if not transfer_id:
                raise ValueError("Не указан transfer_id")
            archive, metadata = self.backups.resolve_archive(instance_id, str(payload.get("backup_id", "")))
            digest = sha256_file(
                archive,
                progress=lambda value: self._progress(job_id, int(value * 0.15), "hash", "Проверяю backup"),
                cancelled=lambda: cancel.is_set(),
            )
            self.hub.upload_transfer(
                transfer_id,
                archive,
                digest,
                lambda value: self._progress(job_id, 15 + int(value * 0.85), "upload", f"Передано {value}%"),
                lambda: cancel.is_set(),
            )
            return {
                "transfer_id": transfer_id,
                "size": archive.stat().st_size,
                "sha256": digest,
                "file_name": archive.name,
                "backup": metadata,
            }
        raise ValueError("Неизвестная операция резервного копирования")

    def _instance_job(self, job_id: str, job_type: str, payload: dict[str, Any], cancel: threading.Event) -> dict[str, Any]:
        if job_type in {"instance_start", "instance_stop", "instance_restart", "instance_kill"}:
            instance_id = validate_instance_id(payload.get("instance_id"))
            if job_type == "instance_start":
                profile = self.instances.get(instance_id)
                instance_directory = self._instance_directory(profile, must_exist=True)
                java_version = self._validate_resources(profile)
                if java_version and java_version != profile.java_version:
                    profile.java_version = java_version
                    self.instances.put(profile)
                if profile.backup.get("before_start") and any(instance_directory.iterdir()):
                    self.backups.create(
                        instance_id,
                        instance_directory,
                        comment="Автоматический backup перед запуском",
                        reason="pre_start",
                        minecraft_running=False,
                        progress=lambda value, detail: self._progress(job_id, int(value * 0.20), "pre_start_backup", detail),
                        cancelled=lambda: cancel.is_set(),
                        settings=profile.backup,
                    )
                self.instances.record_launch(instance_id)
            timeout = 180 if job_type in {"instance_stop", "instance_restart"} else 60
            return self.service_action(instance_id, job_type.removeprefix("instance_"), timeout)
        if job_type == "instance_update":
            instance_id = validate_instance_id(payload.get("instance_id"))
            current = self.instances.get(instance_id)
            operational = {
                "java", "jvm_arguments", "startup_arguments", "startup_command", "startup_reviewed",
                "ram_min_mb", "ram_max_mb", "port", "rcon_port", "rcon_password", "console_mode",
                "shutdown_command",
            }
            if self.instance_status(instance_id).get("active") and any(
                key in payload and payload.get(key) != current.to_disk().get(key) for key in operational
            ):
                raise RuntimeError("Остановите сборку перед изменением портов, Java, RAM или команды запуска")
            profile = self.instances.patch(
                instance_id,
                payload,
                validator=self._validate_profile_ports,
                prepare=self._configure_server_properties,
            )
            self._validate_resources(profile, warn_only=True)
            return {"instance": profile.to_public()}
        if job_type == "instance_delete":
            instance_id = validate_instance_id(payload.get("instance_id"))
            profile = self.instances.get(instance_id)
            if self.instance_status(instance_id).get("active"):
                raise RuntimeError("Сначала остановите Minecraft-сервер")
            if cancel.is_set():
                raise JobCancelled("Удаление отменено до применения")
            staged: Path | None = None
            if payload.get("delete_files"):
                path = self._instance_directory(profile, must_exist=True)
                FileManager._assert_tree_has_no_symlinks(path, lambda: cancel.is_set())
                staged = path.parent / f".{path.name}.deleted-{uuid.uuid4().hex}"
                path.rename(staged)
            try:
                removed = self.instances.remove(instance_id)
            except Exception:
                if staged and staged.exists():
                    staged.rename(self._instance_directory(profile))
                raise
            cleanup_pending = False
            if staged:
                try:
                    shutil.rmtree(staged)
                except OSError as error:
                    cleanup_pending = True
                    self.event("server", f"Не удалось полностью очистить {staged}: {error}", source="files", instance_id=instance_id, level="WARN")
            return {"deleted": removed.id, "files_deleted": bool(payload.get("delete_files")), "cleanup_pending": cleanup_pending}
        if job_type == "instance_duplicate":
            source = self.instances.get(payload.get("instance_id"))
            new_id = validate_instance_id(payload.get("new_instance_id"))
            destination = self.instances.minecraft_root / new_id
            if destination.exists():
                raise FileExistsError(destination)
            try:
                source_directory = self._instance_directory(source, must_exist=True)
                FileManager._assert_tree_has_no_symlinks(source_directory, lambda: cancel.is_set())
                self._progress(job_id, 5, "duplicate", "Копирую файлы сборки")
                FileManager._copy_tree(
                    source_directory,
                    destination,
                    lambda: cancel.is_set(),
                    lambda value, detail: self._progress(job_id, 5 + int(value * 0.80), "duplicate", detail),
                )
                data = source.to_disk()
                data.update({"id": new_id, "name": str(payload.get("name") or new_id), "directory": str(destination), "service": f"server-control-minecraft@{new_id}.service", "managed_service": True, "launch_count": 0, "last_launch_at": None, "installed_at": int(time.time())})
                profile = InstanceProfile.from_dict(data)
                profile.port, profile.rcon_port = self._next_ports(source.port, source.rcon_port)
                profile.rcon_password = secrets.token_urlsafe(32)
                self._validate_profile_ports(profile)
                self._configure_server_properties(profile)
                self.instances.put(profile, replace=False)
                self._progress(job_id, 100, "complete", "Копия сборки готова", force=True)
                return {"instance": profile.to_public()}
            except Exception:
                if destination.exists():
                    shutil.rmtree(destination, ignore_errors=True)
                raise
        if job_type == "instance_install_vanilla":
            return self._install_vanilla(job_id, payload, cancel)
        if job_type == "instance_create":
            return self._create_instance(job_id, payload, cancel)
        if job_type == "instance_update_files":
            return self._update_instance_files(job_id, payload, cancel)
        raise ValueError("Неизвестная операция сборки")

    def _update_instance_files(
        self,
        job_id: str,
        payload: dict[str, Any],
        cancel: threading.Event,
    ) -> dict[str, Any]:
        instance_id = validate_instance_id(payload.get("instance_id"))
        transfer_id = str(payload.get("transfer_id", ""))
        expected_sha256 = str(payload.get("transfer_sha256", "")).lower()
        if not transfer_id or not re.fullmatch(r"[a-f0-9]{64}", expected_sha256):
            raise ValueError("Для обновления нужен загруженный ZIP и его SHA-256")
        profile = self.instances.get(instance_id)
        original_profile = InstanceProfile.from_dict(profile.to_disk())
        destination = self._instance_directory(profile, must_exist=True)
        parent = destination.parent
        temporary = parent / f".{destination.name}.update-{uuid.uuid4().hex}.zip"
        staging = parent / f".{destination.name}.staging-{uuid.uuid4().hex}"
        old = parent / f".{destination.name}.before-update-{uuid.uuid4().hex}"
        was_running = bool(self.instance_status(instance_id).get("active"))
        safety: dict[str, Any] | None = None
        swapped = False
        try:
            safety = self.backups.create(
                instance_id,
                destination,
                comment="Автоматический safety backup перед обновлением файлов",
                reason="pre_update",
                minecraft_running=was_running,
                rcon=(lambda command: self.minecraft_command(instance_id, command)) if was_running else None,
                progress=lambda value, detail: self._progress(job_id, int(value * 0.20), "safety_backup", detail),
                cancelled=lambda: cancel.is_set(),
                settings=profile.backup,
            )
            if was_running:
                self._progress(job_id, 21, "stop", "Корректно останавливаю Minecraft")
                self.service_action(instance_id, "stop", 180)
            if cancel.is_set():
                raise JobCancelled("Обновление отменено")
            self.hub.download_transfer(
                transfer_id,
                temporary,
                lambda value: self._progress(job_id, 22 + int(value * 0.18), "download", "Загружаю ZIP обновления"),
                lambda: cancel.is_set(),
            )
            actual_sha256 = sha256_file(
                temporary,
                progress=lambda value: self._progress(job_id, 40 + int(value * 0.05), "verify", "Проверяю SHA-256"),
                cancelled=lambda: cancel.is_set(),
            )
            if actual_sha256 != expected_sha256:
                raise ValueError("SHA-256 ZIP обновления не совпал")
            FileManager._assert_tree_has_no_symlinks(destination, lambda: cancel.is_set())
            self._progress(job_id, 46, "stage", "Создаю атомарную staging-копию")
            FileManager._copy_tree(
                destination,
                staging,
                lambda: cancel.is_set(),
                lambda value, detail: self._progress(job_id, 46 + int(value * 0.09), "stage", detail),
            )
            safe_extract_zip(
                temporary,
                staging,
                progress=lambda value, detail: self._progress(job_id, 55 + int(value * 0.30), "extract", detail),
                cancelled=lambda: cancel.is_set(),
            )
            if cancel.is_set():
                raise JobCancelled("Обновление отменено до применения")
            self._progress(job_id, 87, "apply", "Атомарно применяю обновление")
            destination.rename(old)
            try:
                staging.rename(destination)
            except Exception:
                old.rename(destination)
                raise
            swapped = True
            detected = detect_pack(destination)
            changes = {
                "minecraft_version": str(detected.get("minecraft_version") or profile.minecraft_version),
                "loader": str(detected.get("loader") or profile.loader),
                "loader_version": str(detected.get("loader_version") or profile.loader_version),
                "pack_version": str(detected.get("pack_version") or profile.pack_version),
            }
            profile = self.instances.patch(
                instance_id,
                changes,
                validator=self._validate_profile_ports,
                prepare=self._configure_server_properties,
            )
            if was_running:
                self._progress(job_id, 91, "restart", "Запускаю обновлённую сборку")
                self.service_action(instance_id, "start", 60)
                deadline = time.monotonic() + 300
                while time.monotonic() < deadline:
                    if cancel.is_set():
                        raise JobCancelled("Проверка запуска отменена")
                    status = self.instance_status(instance_id)
                    if status.get("state") == "RUNNING":
                        break
                    if status.get("state") == "CRASHED":
                        raise RuntimeError("Обновлённая сборка завершилась с ошибкой; выполняю rollback")
                    self._progress(job_id, 95, "health_check", str(status.get("startup", {}).get("label") or "Жду готовности Minecraft"))
                    time.sleep(2)
                else:
                    raise TimeoutError("Обновлённая сборка не стала Running за 5 минут; выполняю rollback")
            shutil.rmtree(old)
            swapped = False
            self._progress(job_id, 100, "complete", "Файлы сборки обновлены", force=True)
            return {
                "instance": profile.to_public(),
                "safety_backup": safety,
                "detected": detected,
                "sha256": actual_sha256,
                "transfer_id": transfer_id,
                "rollback_performed": False,
            }
        except Exception:
            if swapped and old.exists():
                try:
                    if self.instance_status(instance_id).get("active"):
                        self.service_action(instance_id, "stop", 180)
                except Exception:
                    pass
                failed = parent / f".{destination.name}.failed-update-{uuid.uuid4().hex}"
                if destination.exists():
                    destination.rename(failed)
                old.rename(destination)
                shutil.rmtree(failed, ignore_errors=True)
                self.instances.put(original_profile)
                if was_running:
                    try:
                        self.service_action(instance_id, "start", 60)
                    except Exception:
                        pass
            raise
        finally:
            temporary.unlink(missing_ok=True)
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            if old.exists() and not swapped:
                shutil.rmtree(old, ignore_errors=True)

    def _create_instance(self, job_id: str, payload: dict[str, Any], cancel: threading.Event) -> dict[str, Any]:
        instance_id = validate_instance_id(payload.get("instance_id"))
        mode = str(payload.get("mode", "empty"))
        destination = self.instances.minecraft_root / instance_id
        if mode != "import" and destination.exists():
            raise FileExistsError(destination)
        if mode == "import":
            relative_existing = PathPolicy.normalize(str(payload.get("existing_path", "")), allow_empty=False)
            existing = secure_path_within(
                self.instances.minecraft_root,
                self.instances.minecraft_root / relative_existing,
                must_exist=True,
            )
            if not existing.is_dir():
                raise NotADirectoryError(existing)
            if existing.resolve(strict=True) == self.instances.minecraft_root.resolve(strict=True):
                raise ValueError("Нельзя импортировать корневой каталог всех Minecraft-сборок")
            if any(Path(item.directory).resolve(strict=False) == existing.resolve(strict=True) for item in self.instances.list()):
                raise FileExistsError("Эта директория уже зарегистрирована как сборка")
            destination = existing
        else:
            destination.mkdir(parents=True, mode=0o2770)
            destination.chmod(0o2770)
        try:
            if mode == "upload":
                transfer_id = str(payload.get("transfer_id", ""))
                if not transfer_id:
                    raise ValueError("Не указан загруженный ZIP")
                temporary = destination / f".server-control-{uuid.uuid4().hex}.zip"
                self.hub.download_transfer(transfer_id, temporary, lambda value: self._progress(job_id, int(value * 0.45), "download", "Загружаю архив на сервер"), lambda: cancel.is_set())
                expected_sha256 = str(payload.get("transfer_sha256") or "")
                if not re.fullmatch(r"[a-f0-9]{64}", expected_sha256):
                    raise ValueError("Для загруженного ZIP отсутствует SHA-256")
                actual_sha256 = sha256_file(
                    temporary,
                    progress=lambda value: self._progress(job_id, 45 + int(value * 0.05), "verify", "Проверяю SHA-256 ZIP"),
                    cancelled=lambda: cancel.is_set(),
                )
                if actual_sha256 != expected_sha256:
                    raise ValueError("SHA-256 загруженного ZIP не совпал")
                safe_extract_zip(temporary, destination, progress=lambda value, detail: self._progress(job_id, 50 + int(value * 0.45), "extract", detail), cancelled=lambda: cancel.is_set())
                temporary.unlink(missing_ok=True)
                self._flatten_single_wrapper(destination)
            detected = detect_pack(destination)
            startup_candidates = detected.get("startup_candidates") or []
            profile = InstanceProfile(
                id=instance_id, name=str(payload.get("name") or instance_id), directory=str(destination),
                service=f"server-control-minecraft@{instance_id}.service", managed_service=True,
                startup_command=list(startup_candidates[0]) if startup_candidates else [], startup_reviewed=False,
                minecraft_version=str(detected.get("minecraft_version", "unknown")), loader=str(detected.get("loader", "unknown")),
                loader_version=str(detected.get("loader_version", "unknown")), pack_version=str(detected.get("pack_version", "unknown")),
                log_file=str(destination / "logs/latest.log"), port=int(payload.get("port", 25565)),
                rcon_port=int(payload.get("rcon_port", 25575)), ram_min_mb=int(payload.get("ram_min_mb", 2048)),
                ram_max_mb=int(payload.get("ram_max_mb", 8192)), notes=str(payload.get("notes", "")),
            )
            if bool(payload.get("auto_ports", True)):
                profile.port, profile.rcon_port = self._next_ports(profile.port, profile.rcon_port)
            profile.rcon_password = secrets.token_urlsafe(32)
            self._validate_profile_ports(profile)
            self._configure_server_properties(profile)
            self.instances.put(profile, replace=False)
            return {"instance": profile.to_public(), "detected": detected, "requires_startup_review": True}
        except Exception:
            if mode != "import" and destination.exists():
                shutil.rmtree(destination, ignore_errors=True)
            raise

    @staticmethod
    def _flatten_single_wrapper(directory: Path) -> bool:
        """Flatten the common ZIP layout ``PackName/<server files>`` once."""

        entries = [item for item in directory.iterdir() if item.name not in {"__MACOSX", ".DS_Store"}]
        if len(entries) != 1 or not entries[0].is_dir() or entries[0].is_symlink():
            return False
        wrapper = entries[0]
        for child in list(wrapper.iterdir()):
            target = directory / child.name
            if target.exists():
                return False
        for child in list(wrapper.iterdir()):
            child.rename(directory / child.name)
        wrapper.rmdir()
        for junk in (directory / "__MACOSX", directory / ".DS_Store"):
            if junk.is_dir() and not junk.is_symlink():
                shutil.rmtree(junk, ignore_errors=True)
            elif junk.exists() and not junk.is_symlink():
                junk.unlink(missing_ok=True)
        return True

    def _install_vanilla(self, job_id: str, payload: dict[str, Any], cancel: threading.Event) -> dict[str, Any]:
        instance_id = validate_instance_id(payload.get("instance_id"))
        if payload.get("accept_eula") is not True:
            raise ValueError("Для установки Vanilla необходимо явно принять Minecraft EULA")
        destination = self.instances.minecraft_root / instance_id
        if destination.exists():
            raise FileExistsError(destination)
        destination.mkdir(parents=True, mode=0o2770)
        destination.chmod(0o2770)
        try:
            self._progress(job_id, 2, "manifest", "Получаю официальный список версий Minecraft")
            manifest = self._download_json("https://piston-meta.mojang.com/mc/game/version_manifest_v2.json", 8 * 1024 * 1024)
            desired = str(payload.get("minecraft_version") or "latest")
            if desired == "latest":
                desired = str(manifest.get("latest", {}).get("release", ""))
            version_row = next((item for item in manifest.get("versions", []) if isinstance(item, dict) and item.get("id") == desired and item.get("type") == "release"), None)
            if not version_row:
                raise ValueError("Официальная версия Minecraft не найдена")
            version_manifest = self._download_json(str(version_row["url"]), 4 * 1024 * 1024)
            server = version_manifest.get("downloads", {}).get("server", {})
            url, expected_sha1 = str(server.get("url", "")), str(server.get("sha1", ""))
            if not url or not re.fullmatch(r"[a-f0-9]{40}", expected_sha1):
                raise ValueError("В официальном manifest отсутствует server.jar")
            jar = destination / "server.jar"
            digest = hashlib.sha1()
            with urllib.request.urlopen(url, timeout=30) as response, jar.open("wb") as output:
                total = int(response.headers.get("content-length", 0) or 0)
                if total < 0 or total > MAX_VANILLA_SERVER_JAR_BYTES:
                    raise ValueError("Официальный server.jar превышает безопасный лимит 512 МиБ")
                required = (total or 64 * 1024 * 1024) + 128 * 1024 * 1024
                if shutil.disk_usage(destination).free < required:
                    raise OSError("Недостаточно свободного места для установки Vanilla")
                written = 0
                while True:
                    if cancel.is_set():
                        raise JobCancelled("Установка отменена")
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
                    if written > MAX_VANILLA_SERVER_JAR_BYTES:
                        raise ValueError("Официальный server.jar превышает безопасный лимит 512 МиБ")
                    if total:
                        self._progress(job_id, 5 + int(written * 80 / total), "download", "Скачиваю официальный server.jar")
            if digest.hexdigest() != expected_sha1:
                raise ValueError("SHA-1 server.jar не совпал с официальным manifest")
            accept_eula = True
            (destination / "eula.txt").write_text("eula=true\n", encoding="utf-8")
            ram_min, ram_max = int(payload.get("ram_min_mb", 1024)), int(payload.get("ram_max_mb", 4096))
            java = str(payload.get("java") or "/usr/bin/java")
            profile = InstanceProfile(
                id=instance_id, name=str(payload.get("name") or f"Vanilla {desired}"), directory=str(destination),
                service=f"server-control-minecraft@{instance_id}.service", managed_service=True,
                startup_command=[java, "-jar", "server.jar"], startup_arguments=["nogui"],
                startup_reviewed=True, java=java, ram_min_mb=ram_min, ram_max_mb=ram_max,
                minecraft_version=desired, loader="Vanilla", loader_version=desired, pack_version=desired,
                log_file=str(destination / "logs/latest.log"), port=int(payload.get("port", 25565)), rcon_port=int(payload.get("rcon_port", 25575)),
            )
            if bool(payload.get("auto_ports", True)):
                profile.port, profile.rcon_port = self._next_ports(profile.port, profile.rcon_port)
            profile.rcon_password = secrets.token_urlsafe(32)
            self._validate_profile_ports(profile)
            self._configure_server_properties(profile)
            self.instances.put(profile, replace=False)
            self._progress(job_id, 100, "complete", "Vanilla установлен")
            return {"instance": profile.to_public(), "eula_accepted": accept_eula, "sha1": expected_sha1}
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            raise

    @staticmethod
    def _download_json(url: str, limit: int) -> dict[str, Any]:
        if not url.startswith("https://"):
            raise ValueError("Разрешён только HTTPS")
        with urllib.request.urlopen(url, timeout=20) as response:
            raw = response.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("Manifest слишком большой")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("Некорректный manifest")
        return value

    def _next_ports(self, requested_game: int, requested_rcon: int) -> tuple[int, int]:
        """Choose a free, non-overlapping game/RCON pair for a new profile."""

        used = {
            int(port)
            for profile in self.instances.list()
            for port in (profile.port, profile.rcon_port)
        }
        game = max(1, min(int(requested_game), 65_535))
        rcon = max(1, min(int(requested_rcon), 65_535))
        for offset in range(0, 2000):
            candidate_game = game + offset
            candidate_rcon = rcon + offset
            if candidate_game > 65_535 or candidate_rcon > 65_535:
                break
            if candidate_game == candidate_rcon or candidate_game in used or candidate_rcon in used:
                continue
            if self._tcp_port_open(candidate_game) or self._tcp_port_open(candidate_rcon):
                continue
            return candidate_game, candidate_rcon
        raise RuntimeError("Не удалось подобрать свободные порты Minecraft и RCON")

    @staticmethod
    def _tcp_port_open(port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=0.08):
                return True
        except OSError:
            return False

    def _validate_profile_ports(
        self,
        profile: InstanceProfile,
        current: InstanceProfile | None = None,
    ) -> None:
        if profile.port == profile.rcon_port:
            raise ValueError("Игровой порт и порт RCON должны отличаться")
        used: dict[int, str] = {}
        for other in self.instances.list():
            if other.id == profile.id:
                continue
            used[other.port] = other.name
            used[other.rcon_port] = other.name
        for label, port in (("Minecraft", profile.port), ("RCON", profile.rcon_port)):
            if port in used:
                raise ValueError(f"Порт {port} уже используется сборкой «{used[port]}»")
            unchanged = bool(current and port in {current.port, current.rcon_port})
            if not unchanged and self._tcp_port_open(port):
                raise ValueError(f"Порт {port} ({label}) уже занят другим процессом")

    def _configure_server_properties(self, profile: InstanceProfile) -> None:
        """Persist the profile's ports and a private RCON password safely."""

        manager = FileManager(self._instance_directory(profile, must_exist=True))
        relative = "server.properties"
        content = ""
        encoding = "utf-8"
        expected_mtime_ns: int | None = None
        path = self._instance_directory(profile, must_exist=True) / relative
        if path.is_file() and not path.is_symlink():
            loaded = manager.read_text(relative)
            content = str(loaded["content"])
            encoding = str(loaded["encoding"])
            expected_mtime_ns = int(loaded["mtime_ns"])
            for line in content.splitlines():
                if line.startswith("rcon.password=") and line.partition("=")[2].strip():
                    profile.rcon_password = line.partition("=")[2].strip()
                    break
        if not profile.rcon_password or profile.rcon_password.startswith("REPLACE_"):
            profile.rcon_password = secrets.token_urlsafe(32)
        desired = {
            "server-port": str(profile.port),
            "enable-rcon": "true",
            "rcon.port": str(profile.rcon_port),
            "rcon.password": profile.rcon_password,
            "broadcast-rcon-to-ops": "false",
        }
        seen: set[str] = set()
        output: list[str] = []
        for line in content.splitlines():
            key = line.partition("=")[0].strip() if "=" in line and not line.lstrip().startswith(("#", "!")) else ""
            if key in desired:
                if key not in seen:
                    output.append(f"{key}={desired[key]}")
                    seen.add(key)
            else:
                output.append(line)
        for key, value in desired.items():
            if key not in seen:
                output.append(f"{key}={value}")
        manager.write_text(
            relative,
            "\n".join(output).rstrip("\n") + "\n",
            encoding=encoding,
            expected_mtime_ns=expected_mtime_ns,
        )

    @staticmethod
    def _validate_resources(profile: InstanceProfile, *, warn_only: bool = False) -> str | None:
        memory = shutil.disk_usage("/")  # forces an early, clear OS error if the host is unhealthy
        del memory
        available_kb = 0
        try:
            for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
                if line.startswith("MemAvailable:"):
                    available_kb = int(line.split()[1])
                    break
        except (OSError, ValueError, IndexError):
            pass
        if available_kb and profile.ram_max_mb * 1024 > available_kb - 512 * 1024 and not warn_only:
            raise MemoryError("Недостаточно свободной RAM: оставьте системе минимум 512 МиБ")
        java = profile.java if os.path.isabs(profile.java) else shutil.which(profile.java, path="/usr/local/bin:/usr/bin:/bin")
        if not java or not Path(java).is_file() or not os.access(java, os.X_OK):
            if not warn_only:
                raise FileNotFoundError(f"Java не найдена: {profile.java}")
            return None
        try:
            completed = subprocess.run(
                [java, "-version"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=8, check=False, env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"},
            )
            output = completed.stdout.decode("utf-8", "replace")[:4000]
        except (OSError, subprocess.SubprocessError):
            if not warn_only:
                raise RuntimeError("Не удалось проверить выбранную Java")
            return None
        version_match = re.search(r'version\s+"([^"]+)"', output) or re.search(r"(?:openjdk|java)\s+([0-9][^\s]*)", output, re.IGNORECASE)
        version = version_match.group(1) if version_match else "unknown"
        major_match = re.match(r"(?:1\.)?(\d+)", version)
        required = compatible_java_major(profile.minecraft_version)
        if required and major_match and int(major_match.group(1)) < required and not warn_only:
            raise RuntimeError(f"Для Minecraft {profile.minecraft_version} нужна Java {required} или новее; выбрана Java {major_match.group(1)}")
        return version

    def _player_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        instance_id = validate_instance_id(payload.get("instance_id"))
        player = str(payload.get("player", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{1,16}", player):
            raise ValueError("Некорректное имя игрока")
        action = str(payload.get("action", ""))
        commands = {
            "kick": f"kick {player} {str(payload.get('reason', '')).strip()[:120]}",
            "ban": f"ban {player} {str(payload.get('reason', '')).strip()[:120]}",
            "pardon": f"pardon {player}",
            "whitelist_add": f"whitelist add {player}",
            "whitelist_remove": f"whitelist remove {player}",
            "op": f"op {player}",
            "deop": f"deop {player}",
        }
        if action == "teleport":
            coordinates = str(payload.get("coordinates", "")).strip()
            token = r"(?:~?-?\d+(?:\.\d+)?|~)"
            if not re.fullmatch(fr"{token}\s+{token}\s+{token}", coordinates):
                raise ValueError("Координаты должны иметь вид: 100 64 -20 или ~ ~1 ~")
            command = f"tp {player} {coordinates}"
        else:
            command = commands.get(action)
        if not command:
            raise ValueError("Неизвестное действие с игроком")
        output = self.minecraft_command(instance_id, command.strip())
        return {"player": player, "action": action, "output": output}

    def _read_log(self, payload: dict[str, Any]) -> dict[str, Any]:
        profile = self.instances.get(payload.get("instance_id"))
        source = str(payload.get("source", "minecraft"))
        limit = max(20, min(int(payload.get("limit", 500)), 2000))
        before = max(0, int(payload.get("before", 0) or 0))
        if source in {"service", "agent"}:
            service = profile.service if source == "service" else "server-control-agent.service"
            if not re.fullmatch(r"[A-Za-z0-9@_.:-]{1,128}\.service", service):
                raise ValueError("Некорректная служба журнала")
            completed = subprocess.run(
                ["journalctl", "--no-pager", "--output=short-iso", "-u", service, "-n", str(limit)],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, check=False,
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            )
            output = completed.stdout.decode("utf-8", "replace").splitlines()
            return {"lines": output[-limit:], "source": source, "next_before": 0, "truncated": False}
        if source == "updater":
            path = Path("/var/log/server-control-updater.log")
            if not path.is_file():
                return {"lines": [], "source": source, "next_before": 0, "truncated": False}
            data, truncated = read_tail_bytes(path)
            lines = data.decode("utf-8", "replace").splitlines()
            return {"lines": lines[-limit:], "source": source, "next_before": 0, "truncated": truncated}
        if source == "minecraft":
            instance_directory = self._instance_directory(profile, must_exist=True)
            path = Path(profile.log_file or instance_directory / "logs/latest.log")
        elif source == "crash":
            instance_directory = self._instance_directory(profile, must_exist=True)
            crash_dir = instance_directory / "crash-reports"
            files = sorted(crash_dir.glob("*.txt"), key=lambda item: item.stat().st_mtime, reverse=True) if crash_dir.is_dir() else []
            if not files:
                return {"lines": [], "source": source, "next_before": 0}
            path = files[0]
        else:
            raise ValueError("Недопустимый источник журнала")
        secure_path_within(instance_directory, path, must_exist=True)
        window, truncated = read_tail_bytes(path)
        lines = window.decode("utf-8", "replace").splitlines()
        end = len(lines) if before <= 0 else max(0, len(lines) - before)
        start = max(0, end - limit)
        return {"lines": lines[start:end], "source": source, "next_before": len(lines) - start, "truncated": truncated}

    def _transfer_job(self, job_id: str, job_type: str, payload: dict[str, Any], cancel: threading.Event) -> dict[str, Any]:
        profile = self.instances.get(payload.get("instance_id"))
        manager = FileManager(self._instance_directory(profile, must_exist=True))
        transfer_id = str(payload.get("transfer_id", ""))
        if not transfer_id:
            raise ValueError("Не указан transfer_id")
        if job_type == "transfer_import":
            target_directory = manager.policy.resolve(str(payload.get("path", "")))
            target_directory.mkdir(parents=True, exist_ok=True)
            target = target_directory / validate_filename(payload.get("file_name"))
            manager.policy.resolve(target.relative_to(manager.root).as_posix())
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.upload")
            try:
                self.hub.download_transfer(
                    transfer_id, temporary,
                    lambda value: self._progress(job_id, int(value * 0.85), "download", f"Получено {value}%"),
                    lambda: cancel.is_set(),
                )
                expected = str(payload.get("sha256") or "")
                if not re.fullmatch(r"[a-f0-9]{64}", expected):
                    raise ValueError("Для загруженного файла отсутствует SHA-256")
                actual = sha256_file(
                    temporary,
                    progress=lambda value: self._progress(job_id, 85 + int(value * 0.14), "verify", "Проверяю SHA-256"),
                    cancelled=lambda: cancel.is_set(),
                )
                if actual != expected:
                    raise ValueError("SHA-256 загруженного файла не совпал")
                if target.exists() and not bool(payload.get("overwrite")):
                    raise FileExistsError(target)
                os.replace(temporary, target)
                target.chmod(0o660)
                return {"path": target.relative_to(manager.root).as_posix(), "size": target.stat().st_size, "sha256": expected}
            finally:
                temporary.unlink(missing_ok=True)
        source = manager.policy.resolve(str(payload.get("path", "")), must_exist=True, allow_empty=False)
        if source.is_symlink():
            raise SecurityError("Передача символических ссылок запрещена")
        temporary: Path | None = None
        try:
            if source.is_dir():
                temporary = manager.root / f".server-control-export-{uuid.uuid4().hex}.zip"
                FileManager(manager.root).operation(
                    "archive", source.relative_to(manager.root).as_posix(), destination=temporary.relative_to(manager.root).as_posix(),
                    cancelled=lambda: cancel.is_set(), progress=lambda value, detail: self._progress(job_id, int(value * 0.35), "archive", detail),
                )
                source = temporary
            digest = sha256_file(source, progress=lambda value: self._progress(job_id, 35 + int(value * 0.10), "hash", "Вычисляю SHA-256"), cancelled=lambda: cancel.is_set())
            self.hub.upload_transfer(
                transfer_id, source, digest,
                lambda value: self._progress(job_id, 45 + int(value * 0.55), "upload", f"Передано {value}%"),
                lambda: cancel.is_set(),
            )
            return {"transfer_id": transfer_id, "size": source.stat().st_size, "sha256": digest, "file_name": source.name}
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
