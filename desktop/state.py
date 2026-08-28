"""Thread-safe client state and non-secret local UI preferences."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


PERMISSION_ALIASES = {
    "status.view": {"status.view", "server.view", "server_view", "minecraft.view", "minecraft_view"},
    "terminal.linux": {"terminal.linux", "server_command"},
    "terminal.minecraft": {"terminal.minecraft", "minecraft.console", "minecraft_command"},
    "server.view": {"server.view", "server_view"},
    "server.power": {"server.power", "power_control"},
    "server.reboot": {"server.reboot", "server_command"},
    "server.services": {"server.services", "server_command"},
    "server.processes": {"server.processes", "server_view"},
    "minecraft.view": {"minecraft.view", "minecraft_view"},
    "minecraft.start": {"minecraft.start", "minecraft_command"},
    "minecraft.stop": {"minecraft.stop", "minecraft_command"},
    "minecraft.restart": {"minecraft.restart", "minecraft_command"},
    "minecraft.kill": {"minecraft.kill"},
    "minecraft.console": {"minecraft.console", "minecraft_command"},
    "minecraft.players": {"minecraft.players", "minecraft_command"},
    "minecraft.instances.manage": {"minecraft.instances.manage"},
    "minecraft.settings": {"minecraft.settings"},
    "minecraft.files.read": {"minecraft.files.read"},
    "minecraft.files.write": {"minecraft.files.write"},
    "minecraft.backups": {"minecraft.backups"},
    "minecraft.restore": {"minecraft.restore"},
    "minecraft.delete": {"minecraft.delete"},
    "logs.view": {"logs.view", "server_view", "minecraft_view"},
    "audit.view": {"audit.view"},
    "settings.manage": {"settings.manage"},
    "updates.manage": {"updates.manage"},
    "users.manage": {"users.manage", "user_manage"},
}


@dataclass
class AppState:
    user: dict[str, Any]
    server: dict[str, Any] = field(default_factory=dict)
    power: dict[str, Any] = field(default_factory=dict)
    instances: dict[str, dict[str, Any]] = field(default_factory=dict)
    selected_instance_id: str | None = None
    jobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    notifications: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    event_cursor: int = 0
    minecraft_event_cursor: int = 0
    server_event_cursor: int = 0
    jobs_cursor: int = 0
    notification_cursor: int = 0
    protocol: dict[str, Any] = field(default_factory=dict)
    connected: bool = False
    last_error: str = ""
    sync_failures: int = 0
    latency_ms: int | None = None
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def has_permission(self, permission: str) -> bool:
        permissions = set(self.user.get("permissions", []))
        if self.user.get("role") == "owner":
            return True
        return bool(permissions & PERMISSION_ALIASES.get(permission, {permission}))

    def _apply_server_locked(self, incoming_server: dict[str, Any]) -> None:
        """Replace the lightweight server envelope without losing last status."""

        previous_status = self.server.get("status") if isinstance(self.server.get("status"), dict) else None
        self.server = dict(incoming_server)
        if not isinstance(incoming_server.get("status"), dict) and previous_status is not None:
            self.server["status"] = previous_status
        status = self.server.get("status") if isinstance(self.server.get("status"), dict) else {}
        values = status.get("instances") if isinstance(status.get("instances"), list) else []
        if not values and isinstance(status.get("minecraft"), dict):
            values = [status["minecraft"]]
        self.instances = {
            str(item.get("id") or item.get("service") or index): dict(item)
            for index, item in enumerate(values)
            if isinstance(item, dict)
        }
        selected = status.get("selected_instance_id") or self.selected_instance_id
        if selected not in self.instances:
            selected = next(iter(self.instances), None)
        self.selected_instance_id = str(selected) if selected else None

    def apply_server_snapshot(self, payload: dict[str, Any], protocol: dict[str, Any] | None = None) -> None:
        """Apply the proven /v1/server/status feed as the connection authority."""

        with self.lock:
            if protocol is not None:
                self.protocol = dict(protocol)
            self._apply_server_locked(payload)
            self.connected = True
            self.last_error = ""
            self.sync_failures = 0

    def apply_power(self, payload: dict[str, Any]) -> None:
        with self.lock:
            value = payload.get("power")
            if isinstance(value, dict):
                self.power = dict(value)

    def apply_events(self, payload: dict[str, Any], *, stream: str) -> list[dict[str, Any]]:
        """Merge one independent log stream and advance only its own cursor."""

        with self.lock:
            seen = {
                int(item["id"])
                for item in self.events
                if isinstance(item, dict) and str(item.get("id", "")).isdigit()
            }
            new_events: list[dict[str, Any]] = []
            for value in payload.get("events", []):
                if not isinstance(value, dict):
                    continue
                item = dict(value)
                try:
                    identifier = int(item.get("id"))
                except (TypeError, ValueError):
                    identifier = 0
                if identifier and identifier in seen:
                    continue
                if identifier:
                    seen.add(identifier)
                new_events.append(item)
            self.events.extend(new_events)
            self.events = self.events[-10_000:]
            next_after = int(payload.get("next_after", 0) or 0)
            if stream == "minecraft":
                self.minecraft_event_cursor = max(self.minecraft_event_cursor, next_after)
            elif stream == "server":
                self.server_event_cursor = max(self.server_event_cursor, next_after)
            return new_events

    def apply_notifications(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        with self.lock:
            seen = {
                int(item["id"])
                for item in self.notifications
                if isinstance(item, dict) and str(item.get("id", "")).isdigit()
            }
            new_notifications: list[dict[str, Any]] = []
            for value in payload.get("notifications", []):
                if not isinstance(value, dict):
                    continue
                item = dict(value)
                try:
                    identifier = int(item.get("id"))
                except (TypeError, ValueError):
                    identifier = 0
                if identifier and identifier in seen:
                    continue
                if identifier:
                    seen.add(identifier)
                    self.notification_cursor = max(self.notification_cursor, identifier)
                new_notifications.append(item)
            self.notifications.extend(new_notifications)
            self.notifications = self.notifications[-500:]
            return new_notifications

    def apply_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Backward-compatible parser for old clients and fixture tests."""

        with self.lock:
            self.protocol = dict(payload.get("protocol") or {})
            incoming_server = payload.get("server") if isinstance(payload.get("server"), dict) else {}
            self._apply_server_locked(incoming_server)
            self.power = dict(payload.get("power") or {})
            new_events = [dict(item) for item in payload.get("events", []) if isinstance(item, dict)]
            self.events.extend(new_events)
            self.events = self.events[-10_000:]
            for item in payload.get("jobs", []):
                if isinstance(item, dict) and item.get("id"):
                    self.jobs[str(item["id"])] = dict(item)
            self.jobs = dict(sorted(self.jobs.items(), key=lambda pair: int(pair[1].get("created_at", 0)), reverse=True)[:500])
            new_notifications = [dict(item) for item in payload.get("notifications", []) if isinstance(item, dict)]
            self.notifications.extend(new_notifications)
            self.notifications = self.notifications[-500:]
            self.event_cursor = int(payload.get("next_after", self.event_cursor) or self.event_cursor)
            self.jobs_cursor = int(payload.get("jobs_cursor", self.jobs_cursor) or self.jobs_cursor)
            self.notification_cursor = int(payload.get("notification_cursor", self.notification_cursor) or self.notification_cursor)
            self.connected = True
            self.last_error = ""
            self.sync_failures = 0
            return {"events": new_events, "notifications": new_notifications}

    def mark_disconnected(self, error: Exception) -> None:
        with self.lock:
            self.connected = False
            self.last_error = str(error)
            self.sync_failures += 1

    def selected_instance(self) -> dict[str, Any] | None:
        with self.lock:
            if not self.selected_instance_id:
                return None
            value = self.instances.get(self.selected_instance_id)
            return dict(value) if value else None


class LocalPreferences:
    """Stores only UI state; access tokens and server secrets are excluded."""

    DEFAULTS = {
        "theme": "dark",
        "console_autoscroll": True,
        "console_history": {},
        "recent_files": [],
        "favourite_files": [],
        "recent_actions": [],
        "window_geometry": "1280x820",
    }

    def __init__(self, path: Path) -> None:
        self.path = path
        self.values = dict(self.DEFAULTS)
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self.values.update({key: loaded[key] for key in self.DEFAULTS if key in loaded})
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def set(self, key: str, value: Any) -> None:
        if key not in self.DEFAULTS:
            raise KeyError(key)
        self.values[key] = value
        self.save()

    def remember(self, key: str, value: Any, limit: int = 30) -> None:
        values = list(self.values.get(key, []))
        values = [item for item in values if item != value]
        values.insert(0, value)
        self.values[key] = values[:limit]
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=str(self.path.parent))
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(self.values, output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass
