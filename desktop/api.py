"""Small HTTPS client used by Server Control 2."""

from __future__ import annotations

import http.client
import json
import logging
import os
import socket
import tempfile
import threading
import time
import urllib.parse
from contextlib import nullcontext
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


MAX_JSON_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_ERROR_RESPONSE_BYTES = 256 * 1024


class ApiError(RuntimeError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class ApiClient:
    """Authenticated JSON client with deterministic Cloudflare IPv4 failover."""

    def __init__(self, base_url: str, diagnostic_path: Path | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        parsed = urllib.parse.urlsplit(self.base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Адрес Control Hub должен быть корректным HTTPS-адресом.")
        self.host = parsed.hostname
        self.port = parsed.port or 443
        self.base_path = parsed.path.rstrip("/")
        self.token: str | None = None
        self._quota_retry_at = 0.0
        self._quota_message = ""
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
        logger = logging.getLogger(f"server_control.http.{hash(str(path.resolve()))}")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        if not logger.handlers:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                handler: logging.Handler = RotatingFileHandler(
                    path, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
                )
                handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            except OSError:
                handler = logging.NullHandler()
            logger.addHandler(handler)
        return logger

    @staticmethod
    def _diagnostic_route(path: str) -> str:
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
        suffix = f" ipv4={ipv4}" if ipv4 else ""
        if error:
            suffix += f" error={error}"
        self._http_log.log(
            logging.INFO if 200 <= status < 400 else logging.WARNING,
            "%s %s status=%s time_ms=%s bytes=%s%s",
            method.upper(),
            self._diagnostic_route(path),
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
            self.host, self.port, socket.AF_INET, socket.SOCK_STREAM
        ):
            address = str(sockaddr[0])
            if address not in candidates:
                candidates.append(address)
        return candidates

    def _json_connection(
        self, timeout_seconds: float, ipv4_address: str | None = None
    ) -> http.client.HTTPSConnection:
        connection = http.client.HTTPSConnection(self.host, self.port, timeout=max(1, timeout_seconds))
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
        timeout_seconds: float = 20,
    ) -> dict[str, Any]:
        if time.monotonic() < self._quota_retry_at and path != "/health":
            raise ApiError(503, "d1_daily_quota_exceeded", self._quota_message)
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
        body = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if payload is not None
            else None
        )
        if body is not None:
            headers["Content-Length"] = str(len(body))

        normalized_method = method.upper()
        retry_safe = normalized_method in {"GET", "HEAD", "OPTIONS"}
        started = time.monotonic()
        status = 0
        raw = ""
        last_error: Exception | None = None
        selected_candidate: str | None = None

        with (self._request_lock if retry_safe else nullcontext()):
            try:
                candidates = self._resolve_ipv4_candidates()
            except OSError:
                candidates = []
            if candidates:
                now = time.monotonic()
                healthy = [
                    value for value in candidates
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
                selected_candidate = candidate
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
                ipv4=selected_candidate or "",
            )

        parsed = self._parse_json(raw)
        if status >= 400:
            if parsed.get("error") == "d1_daily_quota_exceeded":
                try:
                    delay = max(1, min(300, int(parsed.get("retry_after", 300))))
                except (TypeError, ValueError, OverflowError):
                    delay = 300
                self._quota_message = str(parsed.get("message") or "Исчерпан суточный лимит Cloudflare D1.")
                self._quota_retry_at = time.monotonic() + delay
            raise ApiError(status, str(parsed.get("error", "http_error")), str(parsed.get("message", "Ошибка сервера.")))
        return parsed

    @staticmethod
    def _read_limited(stream: Any, limit: int) -> bytes:
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ApiError(502, "response_too_large", "Сервис вернул слишком большой ответ.")
        return data

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any]:
        try:
            value = json.loads(raw) if raw else {}
        except json.JSONDecodeError as error:
            raise ApiError(502, "invalid_json", "Сервис вернул некорректный ответ.") from error
        if not isinstance(value, dict):
            raise ApiError(502, "invalid_json", "Сервис вернул некорректный ответ.")
        return value

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

    def terminal_credentials(self, kind: str) -> dict[str, Any]:
        if kind not in {"linux", "minecraft"}:
            raise ValueError("Неизвестный тип терминала")
        return self.request("GET", f"/v2/terminal/session?kind={kind}", timeout_seconds=15)
