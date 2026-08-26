"""HTTP client for Server Control's public API."""

from __future__ import annotations

import json
import hashlib
import http.client
import logging
import os
import socket
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import nullcontext
from pathlib import Path
from logging.handlers import RotatingFileHandler
from typing import Any


MAX_JSON_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_ERROR_RESPONSE_BYTES = 1024 * 1024
MAX_TRANSFER_BYTES = 50 * 1024 * 1024 * 1024
DOWNLOAD_FREE_SPACE_RESERVE = 128 * 1024 * 1024


class ApiError(RuntimeError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class ApiClient:
    def __init__(self, base_url: str, diagnostic_path: Path | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        parsed = urllib.parse.urlsplit(self.base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Адрес Control Hub должен быть корректным HTTPS-адресом.")
        self.host = parsed.hostname
        self.port = parsed.port or 443
        self.base_path = parsed.path.rstrip("/")
        self.token: str | None = None
        self._request_lock = threading.Lock()
        self._address_cursor = 0
        self._ipv4_failed_at: dict[str, float] = {}
        if diagnostic_path is None:
            local_root = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
            diagnostic_path = (
                Path(local_root) / "ServerControl" / "ServerControl-client.log"
                if local_root
                else Path(tempfile.gettempdir()) / "ServerControl" / "ServerControl-client.log"
            )
        self.diagnostic_log_path = Path(diagnostic_path)
        self._http_log = self._create_diagnostic_logger(self.diagnostic_log_path)

    @staticmethod
    def _create_diagnostic_logger(path: Path) -> logging.Logger:
        """Create a small rotating HTTP log that never records credentials or bodies."""

        logger = logging.getLogger(f"server_control.http.{hash(str(path.resolve()))}")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        if not logger.handlers:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                handler: logging.Handler = RotatingFileHandler(
                    path,
                    maxBytes=1_000_000,
                    backupCount=2,
                    encoding="utf-8",
                )
                handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            except OSError:
                # Diagnostics must never prevent login or server control.
                handler = logging.NullHandler()
            logger.addHandler(handler)
        return logger

    @staticmethod
    def _diagnostic_route(path: str) -> str:
        """Strip all query values because they can contain opaque identifiers."""

        return urllib.parse.urlsplit(path).path or "/"

    def _record_http(
        self,
        method: str,
        path: str,
        started: float,
        *,
        status: int,
        size: int = 0,
        error: str = "",
        ipv4: str = "",
    ) -> None:
        elapsed_ms = max(0, round((time.monotonic() - started) * 1000))
        route = self._diagnostic_route(path)
        suffix = f" ipv4={ipv4}" if ipv4 else ""
        if error:
            suffix += f" error={error}"
        level = logging.INFO if 200 <= status < 400 else logging.WARNING
        self._http_log.log(
            level,
            "%s %s status=%s time_ms=%s bytes=%s%s",
            method.upper(),
            route,
            status,
            elapsed_ms,
            max(0, size),
            suffix,
        )

    @staticmethod
    def _create_ipv4_connection(
        address: tuple[str, int],
        timeout: float | object = socket._GLOBAL_DEFAULT_TIMEOUT,
        source_address: tuple[str, int] | None = None,
    ) -> socket.socket:
        """Connect directly over IPv4 while retaining normal TLS validation.

        Some home routers advertise an IPv6 route that accepts a connection
        but never delivers a complete Cloudflare response.  ``urllib`` then
        waits until its read timeout even though the same URL works through
        ``curl -4``.  Resolve A records explicitly for the JSON control plane;
        ``HTTPSConnection`` still validates the certificate for ``host``.
        """

        host, port = address
        last_error: OSError | None = None
        for family, socktype, protocol, _name, sockaddr in socket.getaddrinfo(
            host, port, socket.AF_INET, socket.SOCK_STREAM
        ):
            connection = socket.socket(family, socktype, protocol)
            try:
                if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                    connection.settimeout(float(timeout))
                if source_address:
                    connection.bind(source_address)
                connection.connect(sockaddr)
                return connection
            except OSError as error:
                last_error = error
                connection.close()
        raise last_error or OSError(f"IPv4-адрес не найден для {host}")

    def _resolve_ipv4_candidates(self) -> list[str]:
        candidates: list[str] = []
        for _family, _socktype, _protocol, _name, sockaddr in socket.getaddrinfo(
            self.host,
            self.port,
            socket.AF_INET,
            socket.SOCK_STREAM,
        ):
            address = str(sockaddr[0])
            if address not in candidates:
                candidates.append(address)
        return candidates

    def _json_connection(self, timeout_seconds: float, ipv4_address: str | None = None) -> http.client.HTTPSConnection:
        connection = http.client.HTTPSConnection(
            self.host,
            self.port,
            timeout=max(1, timeout_seconds),
        )
        if ipv4_address:
            def connect_selected(
                _address: tuple[str, int],
                timeout: float | object = socket._GLOBAL_DEFAULT_TIMEOUT,
                source_address: tuple[str, int] | None = None,
            ) -> socket.socket:
                return self._create_ipv4_connection((ipv4_address, self.port), timeout, source_address)

            connection._create_connection = connect_selected  # type: ignore[method-assign]
            connection._server_control_ipv4 = ipv4_address  # type: ignore[attr-defined]
        else:
            connection._create_connection = self._create_ipv4_connection  # type: ignore[method-assign]
        return connection

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        *,
        timeout_seconds: float = 25,
    ) -> dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Connection": "close",
            "User-Agent": "ServerControlDesktop/2.0",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if extra_headers:
            headers.update(extra_headers)
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") if payload is not None else None
        if body is not None:
            headers["Content-Length"] = str(len(body))
        normalized_method = method.upper()
        retry_safe = normalized_method in {"GET", "HEAD", "OPTIONS"}
        last_error: Exception | None = None
        status = 0
        raw = ""

        # Some routers or ISP paths can establish TLS to one Cloudflare anycast
        # IPv4 address but then silently stall on the response. Keep only one
        # read-only desktop JSON request in flight and retry it through the next
        # resolved IPv4 address within the original timeout budget. Mutations
        # are never retried and are not queued behind a stalled status read.
        with (self._request_lock if retry_safe else nullcontext()):
            started = time.monotonic()
            try:
                candidates = self._resolve_ipv4_candidates()
            except OSError:
                candidates = []
            if candidates:
                now = time.monotonic()
                healthy = [
                    value
                    for value in candidates
                    if now - self._ipv4_failed_at.get(value, -10_000.0) >= 300.0
                ]
                if healthy:
                    candidates = healthy
                offset = self._address_cursor % len(candidates)
                candidates = candidates[offset:] + candidates[:offset]
                self._address_cursor = (self._address_cursor + 1) % len(candidates)
            attempts: list[str | None] = candidates[:2] or [None]
            if retry_safe and len(attempts) == 1:
                attempts.append(attempts[0])
            if not retry_safe:
                attempts = attempts[:1]
            attempt_timeout = max(1.0, float(timeout_seconds) / len(attempts))

            for attempt, candidate in enumerate(attempts, start=1):
                connection = self._json_connection(attempt_timeout, candidate)
                try:
                    connection.request(normalized_method, f"{self.base_path}{path}", body=body, headers=headers)
                    response = connection.getresponse()
                    try:
                        limit = MAX_ERROR_RESPONSE_BYTES if response.status >= 400 else MAX_JSON_RESPONSE_BYTES
                        raw = self._read_limited(response, limit).decode("utf-8", "replace")
                        status = response.status
                    finally:
                        response.close()
                except (TimeoutError, socket.timeout, OSError, http.client.HTTPException) as error:
                    last_error = error
                    if candidate:
                        self._ipv4_failed_at[candidate] = time.monotonic()
                    self._record_http(
                        normalized_method,
                        path,
                        started,
                        status=0,
                        error=f"{type(error).__name__}_attempt_{attempt}_of_{len(attempts)}",
                        ipv4=candidate or "",
                    )
                    continue
                finally:
                    connection.close()
                if candidate:
                    self._ipv4_failed_at.pop(candidate, None)
                break

            if status == 0:
                if isinstance(last_error, (TimeoutError, socket.timeout)):
                    raise ApiError(0, "network_timeout", "Время ожидания ответа истекло.") from last_error
                raise ApiError(
                    0,
                    "network_error",
                    f"Не удалось подключиться к Control Hub по IPv4: {last_error or 'нет доступного IPv4-адреса'}",
                ) from last_error

            self._record_http(
                normalized_method,
                path,
                started,
                status=status,
                size=len(raw.encode("utf-8")),
                ipv4=candidate or "",
            )
        parsed = self._parse_json(raw)
        if status >= 400:
            raise ApiError(status, str(parsed.get("error", "http_error")), str(parsed.get("message", "Ошибка сервера.")))
        return parsed

    def request_binary(
        self,
        method: str,
        path: str,
        data: bytes,
        *,
        timeout_seconds: float = 90,
    ) -> dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(data)),
            "User-Agent": "ServerControlDesktop/2.0",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(f"{self.base_url}{path}", data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=max(1, timeout_seconds)) as response:
                raw = self._read_limited(response, MAX_ERROR_RESPONSE_BYTES).decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            parsed = self._parse_json(self._read_limited(error, MAX_ERROR_RESPONSE_BYTES).decode("utf-8", "replace"))
            raise ApiError(error.code, str(parsed.get("error", "http_error")), str(parsed.get("message", "Ошибка передачи."))) from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise ApiError(0, "network_error", f"Ошибка передачи: {error}") from error
        return self._parse_json(raw)

    def wait_for_job(
        self,
        job: str | dict[str, Any],
        *,
        timeout_seconds: float = 120,
        cancelled: Any = None,
        progress: Any = None,
    ) -> dict[str, Any]:
        job_id = str(job.get("id") if isinstance(job, dict) else job)
        if not job_id:
            raise ValueError("Ответ API не содержит job id")
        deadline = time.monotonic() + max(1, timeout_seconds)
        delay = 0.25
        previous: tuple[Any, ...] | None = None
        while time.monotonic() < deadline:
            if cancelled and cancelled():
                try:
                    self.request("POST", f"/v1/jobs/{urllib.parse.quote(job_id, safe='')}/cancel", {}, timeout_seconds=7)
                finally:
                    raise ApiError(499, "cancelled", "Операция отменена пользователем.")
            current = self.request("GET", f"/v1/jobs/{urllib.parse.quote(job_id, safe='')}", timeout_seconds=15).get("job")
            if not isinstance(current, dict):
                raise ApiError(502, "invalid_job", "Сервис вернул некорректное состояние операции.")
            signature = (current.get("status"), current.get("progress"), current.get("stage"), current.get("message"))
            if progress and signature != previous:
                progress(current)
            previous = signature
            status = str(current.get("status", ""))
            if status == "completed":
                return current
            if status == "cancelled":
                raise ApiError(499, "cancelled", str(current.get("message") or "Операция отменена."))
            if status == "failed":
                raise ApiError(500, str(current.get("error_code") or "job_failed"), str(current.get("message") or "Операция завершилась с ошибкой."))
            time.sleep(delay)
            delay = min(1.5, delay * 1.25)
        raise ApiError(504, "job_timeout", "Операция продолжается в фоне. Её состояние доступно на странице «Задачи».")

    def run_job(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        method: str = "POST",
        timeout_seconds: float = 120,
        cancelled: Any = None,
        progress: Any = None,
    ) -> dict[str, Any]:
        response = self.request(method, path, payload, timeout_seconds=20)
        job = response.get("job")
        if not isinstance(job, dict):
            raise ApiError(502, "invalid_job", "Сервис не создал операцию.")
        completed = self.wait_for_job(job, timeout_seconds=timeout_seconds, cancelled=cancelled, progress=progress)
        result = completed.get("result")
        return result if isinstance(result, dict) else {"result": result}

    @staticmethod
    def hash_file(path: Path, progress: Any = None, cancelled: Any = None) -> str:
        total = max(1, path.stat().st_size)
        read = 0
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while True:
                if cancelled and cancelled():
                    raise ApiError(499, "cancelled", "Передача отменена.")
                chunk = source.read(4 * 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                read += len(chunk)
                if progress:
                    progress({
                        "stage": "hash",
                        "progress": min(100, int(read * 100 / total)),
                        "message": "Проверяю файл",
                        "transferred_bytes": read,
                        "total_bytes": total,
                    })
        return digest.hexdigest()

    def upload_file(
        self,
        source: Path,
        instance_id: str,
        destination: str,
        *,
        remote_name: str | None = None,
        overwrite: bool = False,
        progress: Any = None,
        cancelled: Any = None,
        paused: Any = None,
    ) -> dict[str, Any]:
        source = source.resolve(strict=True)
        if not source.is_file():
            raise ValueError("Выберите обычный файл")
        if source.stat().st_size > MAX_TRANSFER_BYTES:
            raise ValueError("Файл превышает лимит передачи 50 ГиБ")
        digest = self.hash_file(source, progress=progress, cancelled=cancelled)
        created = self.request(
            "POST", "/v1/transfers",
            {"direction": "upload", "instance_id": instance_id, "path": destination, "file_name": remote_name or source.name, "size_bytes": source.stat().st_size, "sha256": digest, "overwrite": overwrite},
            timeout_seconds=20,
        )
        transfer = created.get("transfer") if isinstance(created.get("transfer"), dict) else {}
        transfer_id = str(transfer.get("id", ""))
        if not transfer_id:
            raise ApiError(502, "invalid_transfer", "Сервис не создал передачу.")
        total = max(1, source.stat().st_size)
        sent = 0
        part_number = 1
        try:
            with source.open("rb") as input_file:
                while True:
                    if cancelled and cancelled():
                        raise ApiError(499, "cancelled", "Передача отменена.")
                    while paused and paused():
                        if cancelled and cancelled():
                            raise ApiError(499, "cancelled", "Передача отменена.")
                        time.sleep(0.2)
                    chunk = input_file.read(8 * 1024 * 1024)
                    if not chunk:
                        break
                    attempts = 0
                    while True:
                        try:
                            self.request_binary(
                                "PUT", f"/v1/transfers/{urllib.parse.quote(transfer_id, safe='')}/parts/{part_number}", chunk,
                            )
                            break
                        except ApiError:
                            attempts += 1
                            if attempts >= 3:
                                raise
                            time.sleep(2**attempts)
                    sent += len(chunk)
                    part_number += 1
                    if progress:
                        progress({
                            "stage": "upload",
                            "progress": min(99, int(sent * 100 / total)),
                            "message": f"Передано {sent} из {total} байт",
                            "transferred_bytes": sent,
                            "total_bytes": total,
                        })
            self.request("POST", f"/v1/transfers/{urllib.parse.quote(transfer_id, safe='')}/complete", {}, timeout_seconds=60)
            committed = self.request("POST", f"/v1/transfers/{urllib.parse.quote(transfer_id, safe='')}/commit", {}, timeout_seconds=20)
            completed = self.wait_for_job(committed.get("job", {}), timeout_seconds=max(180, total / (1024 * 1024)), cancelled=cancelled, progress=progress)
            if progress:
                progress({"stage": "complete", "progress": 100, "message": "Файл загружен"})
            return {"transfer": transfer, "job": completed, "sha256": digest}
        except Exception:
            try:
                self.request("POST", f"/v1/transfers/{urllib.parse.quote(transfer_id, safe='')}/cancel", {}, timeout_seconds=10)
            except Exception:
                pass
            raise

    def upload_staging_archive(
        self,
        source: str | Path,
        instance_id: str,
        *,
        progress: Any = None,
        cancelled: Any = None,
        paused: Any = None,
    ) -> dict[str, Any]:
        """Upload a new-instance archive to R2 without importing it yet."""

        path = Path(source).resolve(strict=True)
        if not path.is_file() or path.suffix.casefold() != ".zip":
            raise ValueError("Выберите ZIP-архив")
        if path.stat().st_size > MAX_TRANSFER_BYTES:
            raise ValueError("Архив превышает лимит передачи 50 ГиБ")
        digest = self.hash_file(path, progress=progress, cancelled=cancelled)
        created = self.request(
            "POST", "/v1/transfers",
            {"direction": "upload", "instance_id": instance_id, "path": "", "file_name": path.name, "size_bytes": path.stat().st_size, "sha256": digest, "staging": True},
        )
        transfer = created.get("transfer") if isinstance(created.get("transfer"), dict) else {}
        transfer_id = str(transfer.get("id", ""))
        if not transfer_id:
            raise ApiError(502, "invalid_transfer", "Сервис не создал передачу.")
        sent = 0
        total = max(1, path.stat().st_size)
        try:
            with path.open("rb") as source_file:
                for part_number in range(1, 10_001):
                    if cancelled and cancelled():
                        raise ApiError(499, "cancelled", "Передача отменена.")
                    while paused and paused():
                        if cancelled and cancelled():
                            raise ApiError(499, "cancelled", "Передача отменена.")
                        time.sleep(0.2)
                    chunk = source_file.read(8 * 1024 * 1024)
                    if not chunk:
                        break
                    for attempt in range(3):
                        try:
                            self.request_binary("PUT", f"/v1/transfers/{urllib.parse.quote(transfer_id, safe='')}/parts/{part_number}", chunk)
                            break
                        except ApiError:
                            if attempt >= 2:
                                raise
                            time.sleep(2**attempt)
                    sent += len(chunk)
                    if progress:
                        progress({
                            "stage": "upload",
                            "progress": min(99, int(sent * 100 / total)),
                            "message": f"Передано {sent} из {total} байт",
                            "transferred_bytes": sent,
                            "total_bytes": total,
                        })
            completed = self.request("POST", f"/v1/transfers/{urllib.parse.quote(transfer_id, safe='')}/complete", {})
            if progress:
                progress({"stage": "complete", "progress": 100, "message": "ZIP загружен и проверен"})
            return completed
        except Exception:
            try:
                self.request("POST", f"/v1/transfers/{urllib.parse.quote(transfer_id, safe='')}/cancel", {})
            except Exception:
                pass
            raise

    def download_backup(
        self,
        instance_id: str,
        backup_id: str,
        destination: Path,
        *,
        progress: Any = None,
        cancelled: Any = None,
        paused: Any = None,
    ) -> dict[str, Any]:
        created = self.request(
            "POST", "/v1/backups/download",
            {"instance_id": instance_id, "backup_id": backup_id}, timeout_seconds=20,
        )
        return self._receive_created_download(created, destination, progress=progress, cancelled=cancelled, paused=paused)

    def _receive_created_download(
        self,
        created: dict[str, Any],
        destination: Path,
        *,
        progress: Any = None,
        cancelled: Any = None,
        paused: Any = None,
    ) -> dict[str, Any]:
        transfer = created.get("transfer") if isinstance(created.get("transfer"), dict) else {}
        transfer_id = str(transfer.get("id", ""))
        job = created.get("job")
        if not transfer_id or not isinstance(job, dict):
            raise ApiError(502, "invalid_transfer", "Сервис не создал задачу скачивания.")
        completed = self.wait_for_job(job, timeout_seconds=24 * 60 * 60, cancelled=cancelled, progress=progress)
        result = completed.get("result") if isinstance(completed.get("result"), dict) else {}
        expected_hash = str(result.get("sha256") or "")
        try:
            expected_size = int(result.get("size") or 0)
        except (TypeError, ValueError) as error:
            raise ApiError(502, "invalid_transfer_size", "Agent вернул некорректный размер файла.") from error
        if expected_size < 0 or expected_size > MAX_TRANSFER_BYTES:
            raise ApiError(502, "invalid_transfer_size", "Размер файла выше допустимого лимита.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.part")
        last_error: Exception | None = None
        for attempt in range(3):
            offset = temporary.stat().st_size if temporary.is_file() else 0
            if expected_size and offset > expected_size:
                temporary.unlink(missing_ok=True)
                offset = 0
            digest = hashlib.sha256()
            if offset:
                with temporary.open("rb") as existing:
                    while chunk := existing.read(4 * 1024 * 1024):
                        digest.update(chunk)
            headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/octet-stream", "User-Agent": "ServerControlDesktop/2.0"}
            if offset:
                headers["Range"] = f"bytes={offset}-"
            request = urllib.request.Request(
                f"{self.base_url}/v1/transfers/{urllib.parse.quote(transfer_id, safe='')}/content",
                method="GET", headers=headers,
            )
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    if offset and getattr(response, "status", 200) != 206:
                        offset = 0
                        digest = hashlib.sha256()
                        temporary.unlink(missing_ok=True)
                    remaining = int(response.headers.get("content-length", 0) or 0)
                    if remaining < 0 or offset + remaining > MAX_TRANSFER_BYTES:
                        raise ApiError(502, "transfer_too_large", "Сервис передаёт файл больше 50 ГиБ.")
                    required_free = max(0, (expected_size or (offset + remaining)) - offset) + DOWNLOAD_FREE_SPACE_RESERVE
                    if shutil.disk_usage(destination.parent).free < required_free:
                        raise ApiError(507, "disk_full", "На диске Windows недостаточно места для скачивания.")
                    total = offset + remaining
                    received = offset
                    with temporary.open("ab" if offset else "wb") as output:
                        while True:
                            if cancelled and cancelled():
                                temporary.unlink(missing_ok=True)
                                raise ApiError(499, "cancelled", "Скачивание отменено.")
                            while paused and paused():
                                if cancelled and cancelled():
                                    temporary.unlink(missing_ok=True)
                                    raise ApiError(499, "cancelled", "Скачивание отменено.")
                                time.sleep(0.2)
                            chunk = response.read(1024 * 1024)
                            if not chunk:
                                break
                            output.write(chunk)
                            digest.update(chunk)
                            received += len(chunk)
                            if received > MAX_TRANSFER_BYTES or (expected_size and received > expected_size):
                                raise ApiError(502, "transfer_too_large", "Скачано больше байт, чем объявил Agent.")
                            if progress and total:
                                progress({
                                    "stage": "download",
                                    "progress": min(99, int(received * 100 / total)),
                                    "message": f"Получено {received} из {total} байт",
                                    "transferred_bytes": received,
                                    "total_bytes": expected_size or total,
                                })
                        output.flush()
                        os.fsync(output.fileno())
                if expected_size and temporary.stat().st_size != expected_size:
                    raise ApiError(502, "size_mismatch", f"Получено {temporary.stat().st_size} байт вместо {expected_size}.")
                if expected_hash and digest.hexdigest() != expected_hash:
                    temporary.unlink(missing_ok=True)
                    raise ApiError(502, "hash_mismatch", "SHA-256 скачанного файла не совпал.")
                os.replace(temporary, destination)
                if progress:
                    progress({"stage": "complete", "progress": 100, "message": "Скачивание завершено"})
                return {"path": str(destination), "size": destination.stat().st_size, "sha256": digest.hexdigest()}
            except ApiError as error:
                if error.code in {"transfer_too_large", "size_mismatch", "hash_mismatch", "invalid_transfer_size"}:
                    temporary.unlink(missing_ok=True)
                raise
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                last_error = error
                if attempt < 2:
                    time.sleep(2**attempt)
        raise ApiError(0, "download_failed", f"Скачивание не завершено после трёх попыток: {last_error}")

    def download_file(
        self,
        instance_id: str,
        source: str,
        destination: Path,
        *,
        progress: Any = None,
        cancelled: Any = None,
        paused: Any = None,
    ) -> dict[str, Any]:
        created = self.request(
            "POST", "/v1/transfers",
            {"direction": "download", "instance_id": instance_id, "path": source, "file_name": Path(source).name or "download.zip", "size_bytes": 0},
            timeout_seconds=20,
        )
        return self._receive_created_download(created, destination, progress=progress, cancelled=cancelled, paused=paused)

    def setup_owner(self, bootstrap_key: str, username: str, password: str) -> dict[str, Any]:
        result = self.request(
            "POST",
            "/v1/setup",
            {"username": username, "password": password},
            {"X-Bootstrap-Key": bootstrap_key},
        )
        self.token = str(result["token"])
        return result

    def login(self, username: str, password: str) -> dict[str, Any]:
        result = self.request("POST", "/v1/login", {"username": username, "password": password})
        self.token = str(result["token"])
        return result

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any]:
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def _read_limited(stream: Any, limit: int) -> bytes:
        """Read an HTTP response without trusting Content-Length or EOF."""

        try:
            declared = int(stream.headers.get("content-length", "0") or 0)
        except (AttributeError, TypeError, ValueError):
            declared = 0
        if declared < 0 or declared > limit:
            raise ApiError(502, "response_too_large", "Сервис вернул слишком большой ответ.")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = stream.read(min(64 * 1024, limit + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise ApiError(502, "response_too_large", "Сервис вернул слишком большой ответ.")
            chunks.append(chunk)
        return b"".join(chunks)
