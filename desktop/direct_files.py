"""Dedicated verified SSH transport for file browsing and streamed transfers."""
from __future__ import annotations

import base64
import json
import os
import shlex
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from direct_status import DirectSshStatusClient
from remote_files import REMOTE_FILE_PROGRAM


class TransferCancelled(RuntimeError):
    pass


def file_command(payload: dict[str, Any]) -> str:
    encoded = base64.b64encode(json.dumps(payload, ensure_ascii=True).encode()).decode('ascii')
    return 'sudo -n /usr/bin/python3 -c ' + shlex.quote(REMOTE_FILE_PROGRAM) + ' ' + shlex.quote(encoded)


def read_reply(stream: Any) -> dict[str, Any]:
    raw = stream.readline(16 * 1024 * 1024)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as error:
        raise RuntimeError('Сервер не вернул ответ проводнику. Проверьте SSH и доступ sudo.') from error
    if not isinstance(value, dict) or value.get('ok') is False:
        raise RuntimeError(value.get('error', 'Ошибка файловой операции') if isinstance(value, dict) else 'Неверный ответ сервера')
    return value


class DirectFilesClient(DirectSshStatusClient):
    """Never shares the status transport or retries a mutation after a disconnect."""

    @contextmanager
    def channel(self, credentials: Callable[[], dict[str, Any]], payload: dict[str, Any]):
        with self._lock:
            transport = self._client.get_transport() if self._client else None
            if not transport or not transport.is_active():
                if self._client: self._client.close()
                self._client = None
                self._connect(credentials())
            stdin, stdout, stderr = self._client.exec_command(file_command(payload), timeout=1800)
            try:
                yield stdin, stdout, stderr
            finally:
                stdout.channel.close()

    def request(self, credentials: Callable[[], dict[str, Any]], payload: dict[str, Any]) -> dict[str, Any]:
        with self.channel(credentials, payload) as (stdin, stdout, stderr):
            stdin.channel.shutdown_write()
            result = read_reply(stdout)
            if stdout.channel.recv_exit_status() != 0:
                raise RuntimeError(stderr.read(8192).decode('utf-8', 'replace') or 'Файловая операция не завершена')
            return result

    def upload(self, credentials: Callable[[], dict[str, Any]], source: Path, destination: str,
               expected: list[int] | None, progress: Callable[[int, int], None], cancelled: Callable[[], bool]) -> dict[str, Any]:
        if source.is_symlink() or not source.is_file(): raise ValueError('Выберите обычный файл')
        with source.open('rb') as file:
            before = os.fstat(file.fileno())
            payload = dict(action='upload', path=destination, expected=expected, size=before.st_size)
            with self.channel(credentials, payload) as (stdin, stdout, stderr):
                if not read_reply(stdout).get('ready'): raise RuntimeError('Сервер не готов к загрузке')
                sent = 0
                while sent < before.st_size:
                    if cancelled(): raise TransferCancelled('Передача отменена; незавершённый файл не заменяет исходный.')
                    block = file.read(min(256 * 1024, before.st_size - sent))
                    if not block: raise RuntimeError('Исходный файл изменился во время передачи')
                    stdin.write(block)
                    stdin.flush()
                    sent += len(block)
                    progress(sent, before.st_size)
                after = os.fstat(file.fileno())
                if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                    raise RuntimeError('Исходный файл изменился во время передачи. Замена отменена.')
                if cancelled(): raise TransferCancelled('Передача отменена до сохранения.')
                stdin.write(b'C')
                stdin.flush()
                stdin.channel.shutdown_write()
                result = read_reply(stdout)
                if stdout.channel.recv_exit_status() != 0: raise RuntimeError('Сервер не подтвердил сохранение файла')
                progress(sent, before.st_size)
                return result

    def download(self, credentials: Callable[[], dict[str, Any]], path: str, target: Path,
                 progress: Callable[[int, int], None], cancelled: Callable[[], bool]) -> None:
        descriptor, tmp_name = tempfile.mkstemp(prefix='.sc-download-', dir=target.parent)
        temp = Path(tmp_name)
        initial = target.stat() if target.exists() else None
        try:
            with os.fdopen(descriptor, 'wb') as file:
                with self.channel(credentials, dict(action='download', path=path)) as (stdin, stdout, stderr):
                    stdin.channel.shutdown_write()
                    reply = read_reply(stdout)
                    if not reply.get('ready'): raise RuntimeError('Сервер не готов к скачиванию')
                    size = int(reply['size'])
                    current = 0
                    while current < size:
                        if cancelled(): raise TransferCancelled('Скачивание отменено; исходный файл на ПК сохранён.')
                        block = stdout.read(min(256 * 1024, size-current))
                        if not block: raise RuntimeError('Скачивание прервалось')
                        file.write(block)
                        current += len(block)
                        progress(current, size)
                    if stdout.channel.recv_exit_status() != 0:
                        raise RuntimeError('Файл изменился или чтение прервалось. Скачайте повторно.')
                    file.flush()
                    os.fsync(file.fileno())
            now = target.stat() if target.exists() else None
            stamp = lambda s: (s.st_mtime_ns, s.st_size, s.st_ino) if s else None
            if stamp(initial) != stamp(now): raise RuntimeError('Файл на ПК изменился во время скачивания. Сохранение отменено.')
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)

    def abort(self) -> None:
        """Close without waiting for a long-running file worker during logout."""
        client, self._client = self._client, None
        if client: client.close()
