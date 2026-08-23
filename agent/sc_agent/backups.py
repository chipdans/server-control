"""Consistent, cancellable Minecraft backups with retention and safe restore."""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Callable

from .security import atomic_write_bytes, safe_extract_zip, validate_filename, validate_instance_id


Progress = Callable[[int, str], None]


class BackupManager:
    def __init__(self, backup_root: Path) -> None:
        self.root = backup_root.resolve(strict=False)
        self.root.mkdir(parents=True, exist_ok=True)
        self._cache_lock = threading.RLock()
        self._cache: dict[str | None, tuple[float, list[dict[str, Any]]]] = {}

    def _invalidate_cache(self) -> None:
        with self._cache_lock:
            self._cache.clear()

    def instance_directory(self, instance_id: str) -> Path:
        identifier = validate_instance_id(instance_id)
        directory = self.root / identifier
        if directory.is_symlink():
            raise PermissionError("Символическая ссылка в хранилище backup запрещена")
        resolved = directory.resolve(strict=False)
        try:
            resolved.relative_to(self.root)
        except ValueError as error:
            raise PermissionError("Каталог backup находится вне разрешённого хранилища") from error
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        return directory

    def list(self, instance_id: str | None = None, *, cache_seconds: float = 0.0) -> list[dict[str, Any]]:
        cache_key = validate_instance_id(instance_id) if instance_id is not None else None
        if cache_seconds > 0:
            with self._cache_lock:
                cached = self._cache.get(cache_key)
                if cached and time.monotonic() - cached[0] < cache_seconds:
                    return [dict(item) for item in cached[1]]
        directories = [self.instance_directory(instance_id)] if instance_id else [path for path in self.root.iterdir() if path.is_dir() and not path.is_symlink()]
        backups: list[dict[str, Any]] = []
        for directory in directories:
            for metadata_path in directory.glob("*.json"):
                try:
                    if metadata_path.is_symlink():
                        continue
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                    archive = directory / str(metadata["archive"])
                    if not archive.is_file() or archive.is_symlink():
                        continue
                    backups.append({
                        **metadata,
                        "size": archive.stat().st_size,
                        "modified_at": int(archive.stat().st_mtime * 1000),
                        "download_name": archive.name,
                    })
                except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
        backups.sort(key=lambda item: int(item.get("created_at", 0)), reverse=True)
        result = backups[:1000]
        with self._cache_lock:
            self._cache[cache_key] = (time.monotonic(), [dict(item) for item in result])
        return result

    def _archive_paths(self, source: Path, archive_target: Path, cancelled: Callable[[], bool] | None) -> tuple[list[Path], int]:
        files: list[Path] = []
        total = 0
        for current, directories, names in os.walk(source, followlinks=False):
            if cancelled and cancelled():
                raise InterruptedError("Операция отменена")
            directories[:] = [
                name for name in directories
                if name not in {"backups", ".server-control-history"} and not (Path(current) / name).is_symlink()
            ]
            for name in names:
                path = Path(current) / name
                if path == archive_target or path.is_symlink():
                    continue
                files.append(path)
                total += path.stat().st_size
        return files, total

    def create(
        self,
        instance_id: str,
        source: Path,
        *,
        comment: str = "",
        reason: str = "manual",
        minecraft_running: bool = False,
        rcon: Callable[[str], str] | None = None,
        progress: Progress | None = None,
        cancelled: Callable[[], bool] | None = None,
        settings: dict[str, Any] | None = None,
        preserve_backup_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        source = source.resolve(strict=True)
        if not source.is_dir():
            raise NotADirectoryError(source)
        backup_id = f"{time.strftime('%Y%m%d-%H%M%S', time.localtime())}-{uuid.uuid4().hex[:8]}"
        directory = self.instance_directory(instance_id)
        archive_path = directory / f"{backup_id}.zip"
        metadata_path = directory / f"{backup_id}.json"
        save_disabled = False
        started_at = time.time()
        try:
            if minecraft_running and rcon:
                if progress:
                    progress(1, "Приостанавливаю автоматическое сохранение мира")
                rcon("save-off")
                save_disabled = True
                rcon("save-all flush")
            files, total = self._archive_paths(source, archive_path, cancelled)
            free = shutil.disk_usage(directory).free
            # Deflate varies by pack. Requiring at least 110% of source bytes
            # avoids a half-written backup in the worst common case.
            if total and free < min(total + 512 * 1024 * 1024, int(total * 1.10)):
                raise OSError("Недостаточно свободного места для резервной копии")
            written = 0
            with zipfile.ZipFile(archive_path, "x", zipfile.ZIP_DEFLATED, allowZip64=True, compresslevel=6) as archive:
                for path in files:
                    if cancelled and cancelled():
                        raise InterruptedError("Операция отменена")
                    archive.write(path, path.relative_to(source).as_posix())
                    written += path.stat().st_size
                    if progress and total:
                        progress(2 + min(96, int(written * 96 / total)), path.name)
            archive_path.chmod(0o600)
            metadata = {
                "id": backup_id,
                "instance_id": instance_id,
                "archive": archive_path.name,
                "created_at": int(started_at * 1000),
                "duration_seconds": round(time.time() - started_at, 2),
                "source_bytes": total,
                "files": len(files),
                "comment": str(comment).strip()[:500],
                "reason": str(reason).strip()[:64] or "manual",
                "format": 1,
            }
            atomic_write_bytes(metadata_path, json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8"), mode=0o600)
            self._invalidate_cache()
            if progress:
                progress(100, "Резервная копия готова")
            self.enforce_retention(instance_id, settings or {}, preserve_ids=preserve_backup_ids)
            return {**metadata, "size": archive_path.stat().st_size, "download_name": archive_path.name}
        except Exception:
            archive_path.unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)
            self._invalidate_cache()
            raise
        finally:
            if save_disabled and rcon:
                try:
                    rcon("save-on")
                except Exception:
                    # Backup itself must not be reported as failed after the
                    # complete archive was committed. The caller logs this.
                    pass

    def resolve_archive(self, instance_id: str, backup_id: str) -> tuple[Path, dict[str, Any]]:
        identifier = validate_filename(backup_id)
        metadata_path = self.instance_directory(instance_id) / f"{identifier}.json"
        if not metadata_path.is_file() or metadata_path.is_symlink():
            raise FileNotFoundError("Резервная копия не найдена")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        archive_name = validate_filename(metadata.get("archive"))
        archive_path = metadata_path.parent / archive_name
        if not archive_path.is_file() or archive_path.is_symlink():
            raise FileNotFoundError("Архив резервной копии отсутствует")
        return archive_path, metadata

    def restore(
        self,
        instance_id: str,
        backup_id: str,
        destination: Path,
        *,
        stop: Callable[[], None],
        is_running: Callable[[], bool],
        safety_backup: Callable[[], dict[str, Any]],
        progress: Progress | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        archive, metadata = self.resolve_archive(instance_id, backup_id)
        destination = destination.resolve(strict=True)
        if is_running():
            if progress:
                progress(1, "Корректно останавливаю Minecraft")
            stop()
        if is_running():
            raise RuntimeError("Minecraft не остановился; восстановление отменено")
        if cancelled and cancelled():
            raise InterruptedError("Операция отменена")
        if progress:
            progress(3, "Создаю safety backup текущего состояния")
        safety = safety_backup()
        parent = destination.parent
        staging = parent / f".{destination.name}.restore-{uuid.uuid4().hex}"
        old = parent / f".{destination.name}.before-restore-{uuid.uuid4().hex}"
        try:
            staging.mkdir(mode=0o2770)

            def extract_progress(value: int, detail: str) -> None:
                if progress:
                    progress(8 + int(value * 0.80), detail)

            safe_extract_zip(archive, staging, progress=extract_progress, cancelled=cancelled)
            if cancelled and cancelled():
                raise InterruptedError("Операция отменена")
            if progress:
                progress(90, "Атомарно заменяю файлы сервера")
            destination.rename(old)
            try:
                staging.rename(destination)
            except Exception:
                old.rename(destination)
                raise
            shutil.rmtree(old)
            if progress:
                progress(100, "Восстановление завершено")
            return {"backup": metadata, "safety_backup": safety, "restored": True}
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            if old.exists() and not destination.exists():
                old.rename(destination)

    def delete(self, instance_id: str, backup_id: str) -> dict[str, Any]:
        archive, metadata = self.resolve_archive(instance_id, backup_id)
        metadata_path = archive.with_suffix(".json")
        size = archive.stat().st_size
        archive.unlink()
        metadata_path.unlink(missing_ok=True)
        self._invalidate_cache()
        return {"deleted": metadata.get("id", backup_id), "freed_bytes": size}

    def duplicate_to(
        self,
        instance_id: str,
        backup_id: str,
        destination: Path,
        *,
        progress: Progress | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        archive, metadata = self.resolve_archive(instance_id, backup_id)
        if destination.exists():
            raise FileExistsError(destination)
        safe_extract_zip(archive, destination, progress=progress, cancelled=cancelled)
        return {"path": str(destination), "backup": metadata}

    def enforce_retention(
        self,
        instance_id: str,
        settings: dict[str, Any],
        *,
        preserve_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        keep = max(1, min(int(settings.get("keep_last", 10) or 10), 500))
        max_bytes = max(0, int(settings.get("max_total_bytes", 0) or 0))
        backups = self.list(instance_id)
        protected = {str(value) for value in (preserve_ids or set())}
        removed: list[str] = []
        total = sum(int(item.get("size", 0)) for item in backups)
        while len(backups) > keep or (max_bytes and total > max_bytes and len(backups) > 1):
            removable_index = next(
                (index for index in range(len(backups) - 1, -1, -1) if str(backups[index].get("id")) not in protected),
                None,
            )
            if removable_index is None:
                break
            oldest = backups.pop(removable_index)
            total -= int(oldest.get("size", 0))
            try:
                self.delete(instance_id, str(oldest["id"]))
                removed.append(str(oldest["id"]))
            except OSError:
                break
        return {"removed": removed, "remaining": len(backups), "total_bytes": total}
