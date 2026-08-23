"""Paginated file manager restricted to one Minecraft instance root."""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any, Callable

from .security import PathPolicy, SecurityError, atomic_write_bytes, detect_text_encoding, safe_extract_zip, validate_filename


MAX_EDITOR_BYTES = 192 * 1024
MAX_EDITOR_JSON_BYTES = 180 * 1024
MAX_SEARCH_RESULTS = 500
MAX_SEARCH_FILES = 50_000
MAX_SEARCH_CONTENT_BYTES = 64 * 1024 * 1024
MAX_SEARCH_SECONDS = 20.0
CRITICAL_CONFIG_NAMES = {
    "server.properties", "whitelist.json", "ops.json", "banned-players.json", "banned-ips.json",
    "eula.txt", "user_jvm_args.txt",
}


def _entry_dict(entry: os.DirEntry[str]) -> dict[str, Any]:
    try:
        stat = entry.stat(follow_symlinks=False)
        is_symlink = entry.is_symlink()
        return {
            "name": entry.name,
            "type": "symlink" if is_symlink else "directory" if entry.is_dir(follow_symlinks=False) else "file",
            "size": 0 if entry.is_dir(follow_symlinks=False) else int(stat.st_size),
            "modified_at": int(stat.st_mtime * 1000),
            "mode": oct(stat.st_mode & 0o777),
            "is_symlink": is_symlink,
        }
    except OSError as error:
        return {"name": entry.name, "type": "unavailable", "size": 0, "modified_at": 0, "mode": "", "error": str(error)}


class FileManager:
    def __init__(self, root: Path) -> None:
        self.policy = PathPolicy(root)

    @property
    def root(self) -> Path:
        return self.policy.root

    def list_directory(
        self,
        relative: str,
        *,
        page: int = 1,
        page_size: int = 200,
        sort_by: str = "name",
        descending: bool = False,
        query: str = "",
    ) -> dict[str, Any]:
        directory = self.policy.resolve(relative, must_exist=True)
        if not directory.is_dir():
            raise NotADirectoryError(relative)
        page = max(1, int(page))
        page_size = max(25, min(500, int(page_size)))
        normalized_query = query.casefold().strip()
        entries: list[dict[str, Any]] = []
        with os.scandir(directory) as iterator:
            for entry in iterator:
                if normalized_query and normalized_query not in entry.name.casefold():
                    continue
                entries.append(_entry_dict(entry))
        key_map = {
            "name": lambda item: (item["type"] != "directory", str(item["name"]).casefold()),
            "size": lambda item: (item["type"] != "directory", int(item.get("size", 0)), str(item["name"]).casefold()),
            "modified": lambda item: (item["type"] != "directory", int(item.get("modified_at", 0)), str(item["name"]).casefold()),
            "type": lambda item: (str(item["type"]), str(item["name"]).casefold()),
        }
        entries.sort(key=key_map.get(sort_by, key_map["name"]), reverse=bool(descending))
        total = len(entries)
        start = (page - 1) * page_size
        visible = entries[start : start + page_size]
        normalized = self.policy.normalize(relative)
        parent = str(Path(normalized).parent.as_posix()) if normalized else ""
        if parent == ".":
            parent = ""
        for item in visible:
            item["path"] = f"{normalized}/{item['name']}".strip("/")
        return {
            "path": normalized, "parent": parent, "entries": visible, "page": page, "page_size": page_size,
            "total": total, "pages": max(1, (total + page_size - 1) // page_size),
        }

    def read_text(self, relative: str) -> dict[str, Any]:
        path = self.policy.resolve(relative, must_exist=True, allow_empty=False)
        if not path.is_file() or path.is_symlink():
            raise ValueError("Можно открыть только обычный файл")
        size = path.stat().st_size
        if size > MAX_EDITOR_BYTES:
            raise ValueError(f"Файл слишком велик для редактора ({size} байт); используйте скачивание")
        with path.open("rb") as source:
            data = source.read(MAX_EDITOR_BYTES + 1)
        if len(data) > MAX_EDITOR_BYTES:
            raise ValueError("Файл вырос во время чтения и теперь слишком велик для редактора")
        content, encoding = detect_text_encoding(data)
        if len(json.dumps(content, ensure_ascii=False).encode("utf-8")) > MAX_EDITOR_JSON_BYTES:
            raise ValueError("Файл содержит слишком много экранируемых данных для безопасного API-ответа; используйте скачивание")
        stat = path.stat()
        return {
            "path": self.policy.normalize(relative, allow_empty=False), "content": content, "encoding": encoding,
            "size": size, "modified_at": int(stat.st_mtime * 1000), "mtime_ns": stat.st_mtime_ns,
        }

    def write_text(
        self,
        relative: str,
        content: str,
        *,
        encoding: str = "utf-8",
        expected_mtime_ns: int | None = None,
    ) -> dict[str, Any]:
        if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_EDITOR_BYTES:
            raise ValueError("Текст превышает лимит встроенного редактора")
        path = self.policy.resolve(relative, allow_empty=False)
        if path.exists() and (not path.is_file() or path.is_symlink()):
            raise ValueError("Цель не является обычным файлом")
        if path.exists() and expected_mtime_ns is not None and path.stat().st_mtime_ns != int(expected_mtime_ns):
            raise FileExistsError("Файл изменился на сервере после открытия. Перезагрузите его перед сохранением.")
        backup_path: Path | None = None
        if path.exists() and path.name.casefold() in CRITICAL_CONFIG_NAMES:
            history = self.root / ".server-control-history"
            history.mkdir(parents=True, exist_ok=True)
            timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
            backup_path = history / f"{path.name}.{timestamp}.{time.time_ns() % 1_000_000}.bak"
            shutil.copy2(path, backup_path, follow_symlinks=False)
        allowed_encodings = {"utf-8", "utf-8-sig", "cp1251", "cp866", "latin-1"}
        selected_encoding = encoding if encoding in allowed_encodings else "utf-8"
        data = content.encode(selected_encoding)
        mode = (path.stat().st_mode & 0o777) if path.exists() else 0o640
        atomic_write_bytes(path, data, mode=mode)
        stat = path.stat()
        return {
            "path": self.policy.normalize(relative, allow_empty=False), "size": stat.st_size,
            "modified_at": int(stat.st_mtime * 1000), "mtime_ns": stat.st_mtime_ns,
            "encoding": selected_encoding,
            "safety_backup": self.policy.relative(backup_path) if backup_path else None,
        }

    def search(
        self,
        relative: str,
        query: str,
        *,
        pattern: str = "*",
        include_content: bool = False,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        directory = self.policy.resolve(relative, must_exist=True)
        if not directory.is_dir():
            raise NotADirectoryError(relative)
        needle = str(query or "").casefold().strip()
        if not needle:
            raise ValueError("Введите строку поиска")
        results: list[dict[str, Any]] = []
        truncated = False
        scanned_files = 0
        content_bytes = 0
        deadline = time.monotonic() + MAX_SEARCH_SECONDS
        for current_root, directory_names, file_names in os.walk(directory, followlinks=False):
            if cancelled and cancelled():
                raise InterruptedError("Операция отменена")
            if time.monotonic() >= deadline:
                truncated = True
                break
            directory_names[:] = [name for name in directory_names if name not in {".server-control-history", "backups"}]
            for name in [*directory_names, *file_names]:
                scanned_files += 1
                if scanned_files > MAX_SEARCH_FILES or time.monotonic() >= deadline:
                    truncated = True
                    break
                path = Path(current_root) / name
                if path.is_symlink() or not fnmatch.fnmatch(name, pattern):
                    continue
                match = needle in name.casefold()
                content_match = False
                if include_content and path.is_file() and not match:
                    try:
                        size = path.stat().st_size
                        if size <= 2 * 1024 * 1024 and content_bytes + size <= MAX_SEARCH_CONTENT_BYTES:
                            with path.open("rb") as source:
                                data = source.read(2 * 1024 * 1024 + 1)
                            content_bytes += len(data)
                            if len(data) <= 2 * 1024 * 1024:
                                text, _encoding = detect_text_encoding(data)
                                content_match = needle in text.casefold()
                    except (OSError, ValueError, UnicodeDecodeError):
                        pass
                if match or content_match:
                    stat = path.stat()
                    results.append({
                        "path": path.relative_to(self.root).as_posix(), "name": name,
                        "type": "directory" if path.is_dir() else "file", "size": stat.st_size,
                        "modified_at": int(stat.st_mtime * 1000), "content_match": content_match,
                    })
                    if len(results) >= MAX_SEARCH_RESULTS:
                        truncated = True
                        break
            if truncated:
                break
        return {
            "results": results,
            "truncated": truncated,
            "query": query,
            "scanned_files": scanned_files,
            "content_bytes_scanned": content_bytes,
        }

    def operation(
        self,
        action: str,
        relative: str,
        *,
        destination: str = "",
        name: str = "",
        cancelled: Callable[[], bool] | None = None,
        progress: Callable[[int, str], None] | None = None,
    ) -> dict[str, Any]:
        if action == "create_folder":
            directory = self.policy.resolve(relative)
            target = directory / validate_filename(name)
            self.policy.resolve(target.relative_to(self.root).as_posix())
            target.mkdir(parents=False, exist_ok=False)
            target.chmod(0o2770)
            return {"path": target.relative_to(self.root).as_posix()}
        if action == "create_file":
            directory = self.policy.resolve(relative)
            target = directory / validate_filename(name)
            self.policy.resolve(target.relative_to(self.root).as_posix())
            target.touch(exist_ok=False)
            target.chmod(0o660)
            return {"path": target.relative_to(self.root).as_posix()}

        source = self.policy.resolve(relative, must_exist=True, allow_empty=False)
        if source.is_symlink():
            raise SecurityError("Операции с символическими ссылками запрещены")
        if action == "delete":
            if source.is_dir():
                self._delete_tree(source, cancelled, progress)
            else:
                source.unlink()
                if progress:
                    progress(100, source.name)
            return {"deleted": self.policy.normalize(relative, allow_empty=False)}
        if action == "rename":
            target = source.with_name(validate_filename(name))
            self.policy.resolve(target.relative_to(self.root).as_posix())
            if target.exists():
                raise FileExistsError(target.name)
            source.rename(target)
            return {"path": target.relative_to(self.root).as_posix()}
        if action in {"copy", "move", "duplicate"}:
            if action == "duplicate":
                target = source.with_name(validate_filename(name) if name else f"{source.stem} - копия{source.suffix}")
            else:
                target_dir = self.policy.resolve(destination, must_exist=True)
                if not target_dir.is_dir():
                    raise NotADirectoryError(destination)
                target = target_dir / (validate_filename(name) if name else source.name)
            self.policy.resolve(target.relative_to(self.root).as_posix())
            if target.exists():
                raise FileExistsError(target.name)
            if action == "move":
                shutil.move(str(source), str(target))
            elif source.is_dir():
                self._copy_tree(source, target, cancelled, progress)
            else:
                self._copy_file(source, target, cancelled, progress)
            return {"path": target.relative_to(self.root).as_posix()}
        if action == "archive":
            if not source.is_dir():
                raise NotADirectoryError(relative)
            target = self.policy.resolve(destination or f"{relative.rstrip('/')}.zip", allow_empty=False)
            if target.exists():
                raise FileExistsError(target.name)
            return self._create_zip(source, target, cancelled=cancelled, progress=progress)
        if action == "extract_zip":
            if not source.is_file() or source.suffix.casefold() != ".zip":
                raise ValueError("Выберите ZIP-архив")
            target = self.policy.resolve(destination or str(Path(relative).with_suffix("")))
            if target.exists() and any(target.iterdir()):
                raise FileExistsError("Папка назначения не пуста")
            result = safe_extract_zip(source, target, progress=progress, cancelled=cancelled)
            return {"path": target.relative_to(self.root).as_posix(), **result}
        raise ValueError("Неизвестная файловая операция")

    @staticmethod
    def _assert_tree_has_no_symlinks(root: Path, cancelled: Callable[[], bool] | None) -> None:
        for current, directories, files in os.walk(root, followlinks=False):
            if cancelled and cancelled():
                raise InterruptedError("Операция отменена")
            for name in [*directories, *files]:
                if (Path(current) / name).is_symlink():
                    raise SecurityError("Копирование дерева с символическими ссылками запрещено")

    @staticmethod
    def _make_tree_shared(root: Path, cancelled: Callable[[], bool] | None = None) -> None:
        root.chmod(0o2770)
        for current, directories, files in os.walk(root, followlinks=False):
            if cancelled and cancelled():
                raise InterruptedError("Операция отменена")
            for name in directories:
                (Path(current) / name).chmod(0o2770)
            for name in files:
                path = Path(current) / name
                path.chmod(0o770 if path.stat().st_mode & 0o111 else 0o660)

    @staticmethod
    def _copy_file(
        source: Path,
        target: Path,
        cancelled: Callable[[], bool] | None,
        progress: Callable[[int, str], None] | None,
        *,
        completed_before: int = 0,
        total: int | None = None,
    ) -> int:
        size = source.stat().st_size
        total_bytes = max(1, int(total if total is not None else size))
        written = 0
        temporary = target.with_name(f".{target.name}.copy-{time.time_ns()}")
        try:
            with source.open("rb") as input_file, temporary.open("xb") as output:
                while True:
                    if cancelled and cancelled():
                        raise InterruptedError("Операция отменена")
                    chunk = input_file.read(4 * 1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    written += len(chunk)
                    if progress:
                        progress(min(99, int((completed_before + written) * 100 / total_bytes)), source.name)
                output.flush()
                os.fsync(output.fileno())
            temporary.chmod(0o770 if source.stat().st_mode & 0o111 else 0o660)
            os.replace(temporary, target)
            return written
        finally:
            temporary.unlink(missing_ok=True)

    @classmethod
    def _copy_tree(
        cls,
        source: Path,
        target: Path,
        cancelled: Callable[[], bool] | None,
        progress: Callable[[int, str], None] | None,
    ) -> None:
        total = 0
        cls._assert_tree_has_no_symlinks(source, cancelled)
        for current, _directories, files in os.walk(source, followlinks=False):
            for name in files:
                total += (Path(current) / name).stat().st_size
        target.mkdir(mode=0o2770)
        completed = 0
        try:
            for current, directories, files in os.walk(source, followlinks=False):
                if cancelled and cancelled():
                    raise InterruptedError("Операция отменена")
                relative = Path(current).relative_to(source)
                current_target = target / relative
                current_target.mkdir(parents=True, exist_ok=True)
                current_target.chmod(0o2770)
                for name in directories:
                    directory = current_target / name
                    directory.mkdir(exist_ok=True)
                    directory.chmod(0o2770)
                for name in files:
                    source_file = Path(current) / name
                    completed += cls._copy_file(source_file, current_target / name, cancelled, progress, completed_before=completed, total=total)
            if progress:
                progress(100, "Копирование завершено")
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise

    @staticmethod
    def _delete_tree(
        source: Path,
        cancelled: Callable[[], bool] | None,
        progress: Callable[[int, str], None] | None,
    ) -> None:
        total = 1
        for _current, directories, files in os.walk(source, followlinks=False):
            total += len(directories) + len(files)
        removed = 0
        for current, directories, files in os.walk(source, topdown=False, followlinks=False):
            for name in files:
                if cancelled and cancelled():
                    raise InterruptedError("Удаление отменено")
                path = Path(current) / name
                path.unlink()
                removed += 1
                if progress:
                    progress(min(99, int(removed * 100 / total)), name)
            for name in directories:
                if cancelled and cancelled():
                    raise InterruptedError("Удаление отменено")
                path = Path(current) / name
                if path.is_symlink():
                    path.unlink()
                else:
                    path.rmdir()
                removed += 1
        source.rmdir()
        if progress:
            progress(100, "Удаление завершено")

    def _create_zip(
        self,
        source: Path,
        target: Path,
        *,
        cancelled: Callable[[], bool] | None,
        progress: Callable[[int, str], None] | None,
    ) -> dict[str, Any]:
        files: list[Path] = []
        total = 0
        for current, directories, names in os.walk(source, followlinks=False):
            directories[:] = [name for name in directories if name not in {".server-control-history", "backups"}]
            for name in names:
                path = Path(current) / name
                if path.is_symlink() or path == target:
                    continue
                files.append(path)
                total += path.stat().st_size
        written = 0
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
                for path in files:
                    if cancelled and cancelled():
                        raise InterruptedError("Операция отменена")
                    archive.write(path, path.relative_to(source.parent).as_posix())
                    written += path.stat().st_size
                    if progress and total:
                        progress(min(99, int(written * 100 / total)), path.name)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        if progress:
            progress(100, "Архив создан")
        return {"path": target.relative_to(self.root).as_posix(), "files": len(files), "bytes": written, "size": target.stat().st_size}
