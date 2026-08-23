"""Path, archive and file-integrity primitives for untrusted API input."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable


INSTANCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
MAX_ARCHIVE_MEMBERS = 200_000
MAX_ARCHIVE_UNCOMPRESSED = 100 * 1024 * 1024 * 1024


class SecurityError(PermissionError):
    """The requested path or archive crosses an Agent security boundary."""


def secure_path_within(root: Path, value: str | Path, *, must_exist: bool = False) -> Path:
    """Validate an absolute/profile path and reject every symlink component."""

    resolved_root = root.expanduser().resolve(strict=False)
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = resolved_root / candidate
    try:
        relative = candidate.relative_to(resolved_root)
    except ValueError as error:
        raise SecurityError("Путь находится вне разрешённой директории") from error
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise SecurityError("Недопустимый компонент пути")
    current = resolved_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise SecurityError("Символические ссылки в пути сборки запрещены")
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as error:
        raise SecurityError("Путь после canonicalization находится вне разрешённой директории") from error
    if must_exist and not candidate.exists():
        raise FileNotFoundError(candidate)
    return candidate


def validate_instance_id(value: object) -> str:
    instance_id = str(value or "").strip().lower()
    if not INSTANCE_ID_RE.fullmatch(instance_id):
        raise ValueError("Некорректный идентификатор сборки")
    return instance_id


def validate_filename(value: object) -> str:
    name = str(value or "").strip()
    if not name or len(name) > 255 or "\0" in name or "/" in name or "\\" in name:
        raise ValueError("Некорректное имя файла")
    if name in {".", ".."}:
        raise ValueError("Некорректное имя файла")
    return name


class PathPolicy:
    """Resolve relative paths without traversal or symlink escape."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve(strict=False)

    @staticmethod
    def normalize(relative: object, *, allow_empty: bool = True) -> str:
        if not isinstance(relative, str):
            raise ValueError("Путь должен быть строкой")
        raw = relative.replace("\\", "/").strip()
        if not raw and allow_empty:
            return ""
        if not raw or raw.startswith("/") or WINDOWS_DRIVE_RE.match(raw) or "\0" in raw:
            raise SecurityError("Разрешён только относительный путь")
        parts = [part for part in raw.split("/") if part and part != "."]
        if any(part == ".." for part in parts):
            raise SecurityError("Выход за пределы разрешённой директории запрещён")
        if len(parts) > 64 or any(len(part) > 255 for part in parts):
            raise ValueError("Путь слишком длинный")
        return "/".join(parts)

    def resolve(self, relative: object, *, must_exist: bool = False, allow_empty: bool = True) -> Path:
        normalized = self.normalize(relative, allow_empty=allow_empty)
        parts = PurePosixPath(normalized).parts
        candidate = self.root.joinpath(*parts)
        current = self.root
        for part in parts:
            current = current / part
            if current.is_symlink():
                raise SecurityError("Символические ссылки в пути запрещены")
        # resolve(strict=False) follows every existing symlink in the path and
        # therefore catches both a final symlink and a symlinked parent.
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(self.root)
        except ValueError as error:
            raise SecurityError("Символическая ссылка ведёт за пределы разрешённой директории") from error
        if must_exist and not candidate.exists() and not candidate.is_symlink():
            raise FileNotFoundError(normalized or ".")
        return candidate

    def relative(self, path: Path) -> str:
        resolved = path.resolve(strict=False)
        try:
            return resolved.relative_to(self.root).as_posix()
        except ValueError as error:
            raise SecurityError("Путь находится вне разрешённой директории") from error


def is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK((info.external_attr >> 16) & 0xFFFF)


def validate_archive_members(infos: Iterable[zipfile.ZipInfo], destination: Path) -> tuple[list[zipfile.ZipInfo], int]:
    root = destination.resolve(strict=False)
    accepted: list[zipfile.ZipInfo] = []
    total = 0
    for count, info in enumerate(infos, start=1):
        if count > MAX_ARCHIVE_MEMBERS:
            raise SecurityError("В архиве слишком много файлов")
        name = info.filename.replace("\\", "/")
        pure = PurePosixPath(name)
        if not name or pure.is_absolute() or WINDOWS_DRIVE_RE.match(name) or any(part in {"", ".", ".."} for part in pure.parts):
            raise SecurityError(f"Опасный путь внутри ZIP: {name!r}")
        if is_zip_symlink(info):
            raise SecurityError(f"Символические ссылки в ZIP запрещены: {name!r}")
        target = destination.joinpath(*pure.parts).resolve(strict=False)
        try:
            target.relative_to(root)
        except ValueError as error:
            raise SecurityError(f"ZIP пытается записать файл вне каталога: {name!r}") from error
        total += max(0, int(info.file_size))
        if total > MAX_ARCHIVE_UNCOMPRESSED:
            raise SecurityError("Распакованный архив превышает лимит 100 ГиБ")
        if info.compress_size > 0 and info.file_size > 1024 * 1024 and info.file_size / info.compress_size > 250:
            raise SecurityError(f"Подозрительно высокая степень сжатия: {name!r}")
        accepted.append(info)
    return accepted, total


def safe_extract_zip(
    archive_path: Path,
    destination: Path,
    *,
    progress: Callable[[int, str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, int]:
    destination_created = not destination.exists()
    destination.mkdir(parents=True, exist_ok=True)
    if destination_created:
        destination.chmod(0o2770)
    with zipfile.ZipFile(archive_path) as archive:
        infos, total = validate_archive_members(archive.infolist(), destination)
        if total and shutil.disk_usage(destination).free < total + 128 * 1024 * 1024:
            raise OSError("Недостаточно свободного места для безопасной распаковки ZIP")
        written = 0
        for info in infos:
            if cancelled and cancelled():
                raise InterruptedError("Операция отменена")
            target = destination.joinpath(*PurePosixPath(info.filename.replace("\\", "/")).parts)
            current = destination
            for part in target.relative_to(destination).parts:
                current = current / part
                if current.is_symlink():
                    raise SecurityError(f"Символическая ссылка в пути распаковки запрещена: {info.filename!r}")
            if info.is_dir():
                created = not target.exists()
                target.mkdir(parents=True, exist_ok=True)
                if created:
                    target.chmod(0o2770)
                continue
            existed = target.exists()
            missing_parents: list[Path] = []
            parent = target.parent
            while parent != destination and not parent.exists():
                missing_parents.append(parent)
                parent = parent.parent
            target.parent.mkdir(parents=True, exist_ok=True)
            for created_parent in reversed(missing_parents):
                created_parent.chmod(0o2770)
            with archive.open(info, "r") as source, target.open("wb") as output:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    if cancelled and cancelled():
                        raise InterruptedError("Операция отменена")
                    output.write(chunk)
                    written += len(chunk)
                    if progress and total:
                        progress(min(99, int(written * 100 / total)), info.filename)
            if not existed:
                mode = (info.external_attr >> 16) & 0o777
                target.chmod(0o770 if mode & 0o111 else 0o660)
    if progress:
        progress(100, "Распаковка завершена")
    return {"files": len(infos), "bytes": written}


def detect_text_encoding(data: bytes) -> tuple[str, str]:
    if b"\0" in data[:4096]:
        raise ValueError("Файл выглядит как бинарный")
    candidates = ("utf-8-sig", "utf-8", "cp1251", "cp866", "latin-1")
    for encoding in candidates:
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("utf-8", data, 0, 1, "Не удалось определить кодировку")


def atomic_write_bytes(path: Path, data: bytes, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temp_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        if mode is not None:
            temp_path.chmod(mode)
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def sha256_file(
    path: Path,
    *,
    progress: Callable[[int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> str:
    digest = hashlib.sha256()
    total = max(1, path.stat().st_size)
    read = 0
    with path.open("rb") as source:
        while True:
            chunk = source.read(4 * 1024 * 1024)
            if not chunk:
                break
            if cancelled and cancelled():
                raise InterruptedError("Операция отменена")
            digest.update(chunk)
            read += len(chunk)
            if progress:
                progress(min(100, int(read * 100 / total)))
    return digest.hexdigest()
