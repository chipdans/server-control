"""Standalone companion executable which installs a downloaded application ZIP."""

from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import PurePosixPath
from pathlib import Path


class UpdateRolledBack(RuntimeError):
    """The previous executable was restored and relaunched successfully."""


def wait_for_process(pid: int) -> None:
    if pid <= 0:
        return
    if sys.platform == "win32":
        synchronize = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
        if handle:
            ctypes.windll.kernel32.WaitForSingleObject(handle, 60_000)
            ctypes.windll.kernel32.CloseHandle(handle)
        return
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.2)
    raise RuntimeError("Приложение не завершилось за 60 секунд; обновление отменено.")


def replace_executable(replacement: Path, target: Path) -> None:
    """Retry while Windows releases the old executable file handle."""
    temporary_target = target.with_name(f"{target.stem}.new{target.suffix}")
    deadline = time.monotonic() + 60
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        try:
            shutil.copy2(replacement, temporary_target)
            os.replace(temporary_target, target)
            return
        except OSError as error:
            last_error = error
            time.sleep(0.5)
    raise RuntimeError(f"Не удалось заменить {target.name} за 60 секунд: {last_error}")


def save_rollback_copy(target: Path) -> Path | None:
    """Keep one known-good client EXE until the next successful update."""

    if not target.is_file():
        return None
    backup = target.with_name(f"{target.stem}.previous{target.suffix}")
    shutil.copy2(target, backup)
    return backup


def write_error_log(target: Path, error: Exception) -> None:
    try:
        target.mkdir(parents=True, exist_ok=True)
        (target / "ServerControl-update-error.log").write_text(
            f"{type(error).__name__}: {error}\n", encoding="utf-8"
        )
    except OSError:
        pass


def safe_extract(archive: zipfile.ZipFile, destination: Path) -> None:
    destination_resolved = destination.resolve()
    members = archive.infolist()
    if not members or len(members) > 500:
        raise RuntimeError("Некорректное количество файлов в обновлении.")
    total = 0
    normalized_names: set[str] = set()
    for member in members:
        raw_name = member.filename.replace("\\", "/")
        path = PurePosixPath(raw_name)
        if not raw_name or raw_name.startswith("/") or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise RuntimeError("Недопустимое имя файла внутри архива обновления.")
        normalized = "/".join(path.parts).casefold()
        if normalized in normalized_names:
            raise RuntimeError("Архив обновления содержит повторяющиеся пути.")
        normalized_names.add(normalized)
        total += int(member.file_size)
        if total > 1024 * 1024 * 1024:
            raise RuntimeError("Распакованное обновление превышает лимит 1 ГиБ.")
        if (member.external_attr >> 16) & 0o170000 == 0o120000:
            raise RuntimeError("Символические ссылки в обновлении запрещены.")
        if member.compress_size and member.file_size > 1024 * 1024 and member.file_size / max(1, member.compress_size) > 250:
            raise RuntimeError("Подозрительная степень сжатия файла обновления.")
        candidate = (destination / member.filename).resolve()
        if candidate != destination_resolved and destination_resolved not in candidate.parents:
            raise RuntimeError("Недопустимый путь внутри архива обновления.")
    archive.extractall(destination)


def launch_and_verify(executable: Path, target: Path, backup: Path | None) -> None:
    health = target / f".server-control-update-health-{os.getpid()}.ok"
    health.unlink(missing_ok=True)
    process = subprocess.Popen([str(executable), f"--update-health-file={health}"], cwd=str(target), close_fds=True)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if health.is_file():
            health.unlink(missing_ok=True)
            return
        if process.poll() is not None:
            break
        time.sleep(0.5)
    if process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass
    if backup and backup.is_file():
        replace_executable(backup, executable)
        subprocess.Popen([str(executable)], cwd=str(target), close_fds=True)
    raise UpdateRolledBack("Новая версия не подтвердила успешный запуск; выполнен rollback.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Install a Server Control update")
    parser.add_argument("--wait-pid", type=int, required=True)
    parser.add_argument("--zip", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--restart", type=Path, required=True)
    parser.add_argument("--updater-target", type=Path)
    args = parser.parse_args()

    wait_for_process(args.wait_pid)
    staging = Path(tempfile.mkdtemp(prefix="server-control-apply-"))
    client_backup: Path | None = None
    updater_backup: Path | None = None
    client_replaced = False
    updater_replaced = False
    try:
        with zipfile.ZipFile(args.zip) as archive:
            safe_extract(archive, staging)
        replacement = staging / args.restart.name
        if not replacement.is_file():
            raise RuntimeError(f"В архиве нет {args.restart.name}")
        args.target.mkdir(parents=True, exist_ok=True)
        client_backup = save_rollback_copy(args.restart)
        replace_executable(replacement, args.restart)
        client_replaced = True
        if args.updater_target:
            updater_replacement = staging / args.updater_target.name
            if updater_replacement.is_file():
                updater_backup = save_rollback_copy(args.updater_target)
                replace_executable(updater_replacement, args.updater_target)
                updater_replaced = True
        launch_and_verify(args.restart, args.target, client_backup)
        return 0
    except Exception as error:
        if isinstance(error, UpdateRolledBack):
            try:
                if updater_replaced and updater_backup and updater_backup.is_file() and args.updater_target:
                    replace_executable(updater_backup, args.updater_target)
            except Exception as rollback_error:
                error = RuntimeError(f"{error}; updater rollback завершился ошибкой: {rollback_error}")
        else:
            try:
                if client_replaced and client_backup and client_backup.is_file():
                    replace_executable(client_backup, args.restart)
                if updater_replaced and updater_backup and updater_backup.is_file() and args.updater_target:
                    replace_executable(updater_backup, args.updater_target)
                if client_replaced and args.restart.is_file():
                    subprocess.Popen([str(args.restart)], cwd=str(args.target), close_fds=True)
            except Exception as rollback_error:
                error = RuntimeError(f"{error}; rollback также завершился ошибкой: {rollback_error}")
        write_error_log(args.target, error)
        return 1
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        try:
            archive_parent = args.zip.parent
            args.zip.unlink(missing_ok=True)
            if archive_parent.name.startswith("server-control-update-"):
                archive_parent.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
