"""HTTP client for Server Control's public API."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class ApiError(RuntimeError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class ApiClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token: str | None = None

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json", "User-Agent": "ServerControlDesktop/1.0"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if extra_headers:
            headers.update(extra_headers)
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(f"{self.base_url}{path}", data=body, method=method, headers=headers)

        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                raw = response.read().decode("utf-8")
                status = response.status
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", "replace")
            parsed = self._parse_json(raw)
            raise ApiError(error.code, str(parsed.get("error", "http_error")), str(parsed.get("message", "Ошибка сервера."))) from error
        except urllib.error.URLError as error:
            raise ApiError(0, "network_error", f"Не удалось подключиться: {error.reason}") from error

        parsed = self._parse_json(raw)
        if status >= 400:
            raise ApiError(status, str(parsed.get("error", "http_error")), str(parsed.get("message", "Ошибка сервера.")))
        return parsed

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
