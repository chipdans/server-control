"""Persistent multi-instance profiles and safe Minecraft pack discovery."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .security import PathPolicy, atomic_write_bytes, secure_path_within, validate_instance_id


@dataclass
class InstanceProfile:
    id: str
    name: str
    directory: str
    service: str = ""
    startup_command: list[str] = field(default_factory=list)
    startup_reviewed: bool = False
    java: str = "/usr/bin/java"
    java_version: str = ""
    jvm_arguments: list[str] = field(default_factory=list)
    startup_arguments: list[str] = field(default_factory=lambda: ["nogui"])
    shutdown_command: str = "stop"
    ram_min_mb: int = 2048
    ram_max_mb: int = 8192
    port: int = 25565
    rcon_host: str = "127.0.0.1"
    rcon_port: int = 25575
    rcon_password: str = ""
    console_mode: str = "rcon"
    tmux_session: str = ""
    log_file: str = ""
    minecraft_version: str = "unknown"
    loader: str = "unknown"
    loader_version: str = "unknown"
    pack_version: str = "unknown"
    icon: str = ""
    notes: str = ""
    tags: list[str] = field(default_factory=list)
    favourite: bool = False
    installed_at: int = field(default_factory=lambda: int(time.time()))
    last_launch_at: int | None = None
    launch_count: int = 0
    backup: dict[str, Any] = field(default_factory=dict)
    managed_service: bool = False

    def __post_init__(self) -> None:
        self.id = validate_instance_id(self.id)
        self.name = str(self.name or self.id).strip()[:80]
        self.directory = str(Path(self.directory))
        self.ram_min_mb = max(256, min(int(self.ram_min_mb), 131_072))
        self.ram_max_mb = max(self.ram_min_mb, min(int(self.ram_max_mb), 131_072))
        self.port = max(1, min(int(self.port), 65_535))
        self.rcon_port = max(1, min(int(self.rcon_port), 65_535))
        for field_name, limit in (("startup_command", 256), ("jvm_arguments", 128), ("startup_arguments", 128), ("tags", 20)):
            value = getattr(self, field_name)
            if not isinstance(value, list):
                raise ValueError(f"{field_name} должен быть списком")
            if len(value) > limit:
                raise ValueError(f"{field_name} содержит слишком много элементов")
        self.startup_command = self._clean_arguments(self.startup_command, "startup_command")
        self.jvm_arguments = self._clean_arguments(self.jvm_arguments, "jvm_arguments")
        self.startup_arguments = self._clean_arguments(self.startup_arguments, "startup_arguments")
        self.tags = [str(item).strip()[:32] for item in self.tags if isinstance(item, str) and str(item).strip()][:20]
        if not isinstance(self.backup, dict):
            raise ValueError("backup должен быть объектом")
        self.backup = dict(self.backup)
        self.console_mode = self.console_mode if self.console_mode in {"rcon", "tmux"} else "rcon"
        if self.managed_service:
            self.rcon_host = "127.0.0.1"
            self.console_mode = "rcon"
        self.shutdown_command = str(self.shutdown_command or "stop").strip().lstrip("/")[:128]
        if not self.shutdown_command or any(character in self.shutdown_command for character in "\r\n\0"):
            raise ValueError("Некорректная команда остановки")
        self.java = str(self.java or "/usr/bin/java").strip()[:4096]
        self.notes = str(self.notes or "")[:4000]
        self.icon = str(self.icon or "")[:512]
        self.rcon_password = str(self.rcon_password or "")[:512]
        if any(character in self.rcon_password for character in "\r\n\0"):
            raise ValueError("Некорректный пароль RCON")

    @staticmethod
    def _clean_arguments(values: list[Any], name: str) -> list[str]:
        result: list[str] = []
        for item in values:
            if not isinstance(item, str) or not item or len(item) > 2048 or any(character in item for character in "\r\n\0"):
                raise ValueError(f"Некорректный элемент {name}")
            result.append(item)
        return result

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "InstanceProfile":
        fields = cls.__dataclass_fields__
        return cls(**{key: value[key] for key in fields if key in value})

    def to_disk(self) -> dict[str, Any]:
        return asdict(self)

    def to_public(self) -> dict[str, Any]:
        result = asdict(self)
        result.pop("rcon_password", None)
        result["rcon_configured"] = bool(self.rcon_password and not self.rcon_password.startswith("REPLACE_"))
        return result


def detect_pack(directory: Path) -> dict[str, Any]:
    """Best-effort metadata detection; never executes bundled scripts."""

    result: dict[str, Any] = {
        "minecraft_version": "unknown", "loader": "unknown", "loader_version": "unknown",
        "pack_version": "unknown", "startup_candidates": [], "server_jar": None,
    }
    candidates: list[list[str]] = []
    jars = sorted(directory.glob("*.jar")) if directory.is_dir() else []
    for jar in jars[:200]:
        lower = jar.name.lower()
        if "neoforge" in lower:
            result["loader"] = "NeoForge"
            match = re.search(r"neoforge[-_]?([0-9.]+)", lower)
            if match:
                result["loader_version"] = match.group(1).rstrip(".")
        elif "forge" in lower:
            result["loader"] = "Forge"
            match = re.search(r"forge[-_]?([0-9.]+)", lower)
            if match:
                result["loader_version"] = match.group(1).rstrip(".")
        elif "fabric" in lower:
            result["loader"] = "Fabric"
        elif "quilt" in lower:
            result["loader"] = "Quilt"
        version_match = re.search(r"(?:server|forge|minecraft)[-_](1\.\d+(?:\.\d+)?)", lower)
        if version_match and result["minecraft_version"] == "unknown":
            result["minecraft_version"] = version_match.group(1)
        if lower in {"server.jar", "minecraft_server.jar", "fabric-server-launch.jar"} or "server" in lower:
            result["server_jar"] = jar.name
    if result["server_jar"]:
        candidates.append(["/usr/bin/java", "-jar", str(result["server_jar"]), "nogui"])
    if not candidates:
        argument_files = [
            *directory.glob("libraries/net/minecraftforge/forge/*/unix_args.txt"),
            *directory.glob("libraries/net/neoforged/neoforge/*/unix_args.txt"),
        ] if directory.is_dir() else []
        if argument_files:
            selected = sorted(argument_files)[-1]
            candidates.append(["/usr/bin/java", f"@{selected.relative_to(directory).as_posix()}", "nogui"])

    for name in ("manifest.json", "minecraftinstance.json", "version.json"):
        path = directory / name
        if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        minecraft = data.get("minecraft") if isinstance(data.get("minecraft"), dict) else {}
        version = minecraft.get("version") or data.get("minecraftVersion") or data.get("id")
        if isinstance(version, str) and len(version) < 40:
            result["minecraft_version"] = version
        pack_version = data.get("version") or data.get("versionInfo")
        if isinstance(pack_version, str) and len(pack_version) < 80:
            result["pack_version"] = pack_version
        loaders = minecraft.get("modLoaders") if isinstance(minecraft.get("modLoaders"), list) else []
        if loaders and isinstance(loaders[0], dict):
            loader_id = str(loaders[0].get("id", ""))
            if "forge" in loader_id.lower():
                result["loader"] = "NeoForge" if "neo" in loader_id.lower() else "Forge"
            elif "fabric" in loader_id.lower():
                result["loader"] = "Fabric"
            elif "quilt" in loader_id.lower():
                result["loader"] = "Quilt"
            result["loader_version"] = loader_id.split("-", 1)[-1] or "unknown"

    for script in ("run.sh", "start.sh", "ServerStart.sh", "startserver.sh"):
        if (directory / script).is_file():
            result.setdefault("scripts_requiring_review", []).append(script)

    result["startup_candidates"] = candidates
    return result


class InstanceStore:
    """Thread-safe profile store with a legacy single-instance migration."""

    def __init__(self, path: Path, minecraft_root: Path, legacy: dict[str, Any] | None = None) -> None:
        self.path = path
        self.minecraft_root = minecraft_root.resolve(strict=False)
        self.policy = PathPolicy(self.minecraft_root)
        self._lock = threading.RLock()
        self._profiles: dict[str, InstanceProfile] = {}
        self._selected: str | None = None
        self._load(legacy or {})

    def _legacy_profile(self, legacy: dict[str, Any]) -> InstanceProfile | None:
        directory = str(legacy.get("directory", "")).strip()
        if not directory:
            return None
        service = str(legacy.get("service", "dragonfyre.service"))
        identifier = re.sub(r"[^a-z0-9_-]+", "-", service.removesuffix(".service").lower()).strip("-") or "minecraft"
        return InstanceProfile(
            id=identifier[:48],
            name=str(legacy.get("name", identifier)).strip() or identifier,
            directory=directory,
            service=service,
            startup_reviewed=True,
            log_file=str(legacy.get("log_file", Path(directory) / "logs/latest.log")),
            console_mode=str(legacy.get("console_mode", "rcon")),
            rcon_host=str(legacy.get("rcon_host", "127.0.0.1")),
            rcon_port=int(legacy.get("rcon_port", 25575)),
            rcon_password=str(legacy.get("rcon_password", "")),
            tmux_session=str(legacy.get("tmux_session", identifier)),
            managed_service=False,
        )

    def _load(self, legacy: dict[str, Any]) -> None:
        data: dict[str, Any] = {}
        if self.path.is_file():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                data = {}
        values = data.get("instances") if isinstance(data.get("instances"), list) else []
        for value in values:
            if not isinstance(value, dict):
                continue
            try:
                profile = InstanceProfile.from_dict(value)
                self._validate_directory(profile.directory)
            except (TypeError, ValueError, OSError, PermissionError):
                continue
            self._profiles[profile.id] = profile
        legacy_profile = self._legacy_profile(legacy)
        if legacy_profile and legacy_profile.id not in self._profiles:
            # Existing installations may live below /opt/minecraft while a
            # custom minecraft_root is still absent. Accept the exact legacy
            # directory and use its parent as the root on first migration.
            try:
                self._validate_directory(legacy_profile.directory)
            except PermissionError:
                self.minecraft_root = Path(legacy_profile.directory).resolve(strict=False).parent
                self.policy = PathPolicy(self.minecraft_root)
                self._validate_directory(legacy_profile.directory)
            self._profiles[legacy_profile.id] = legacy_profile
        requested_selected = str(data.get("selected") or "")
        self._selected = requested_selected if requested_selected in self._profiles else next(iter(self._profiles), None)
        # Always rewrite the stores on startup.  Besides repairing a stale
        # selected id, this creates the secret-free runner view introduced in
        # Agent 2 without requiring the owner to edit an existing profile.
        self.save()

    def _validate_directory(self, value: str) -> Path:
        return secure_path_within(self.minecraft_root, value)

    def save(self) -> None:
        with self._lock:
            payload = {"schema": 1, "selected": self._selected, "instances": [item.to_disk() for item in self._profiles.values()]}
            # The complete store contains RCON credentials and is readable
            # only by the Agent account.  The minecraft service gets a second,
            # deliberately minimal view containing only launch parameters.
            atomic_write_bytes(self.path, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"), mode=0o600)
            runner_fields = {
                "id", "directory", "startup_reviewed", "startup_command", "java",
                "jvm_arguments", "startup_arguments", "ram_min_mb", "ram_max_mb",
            }
            runner_payload = {
                "schema": 1,
                "instances": [
                    {key: value for key, value in item.to_disk().items() if key in runner_fields}
                    for item in self._profiles.values()
                ],
            }
            atomic_write_bytes(
                self.path.with_name("runner-instances.json"),
                json.dumps(runner_payload, ensure_ascii=False, indent=2).encode("utf-8"),
                mode=0o640,
            )

    @property
    def selected_id(self) -> str | None:
        with self._lock:
            return self._selected

    def select(self, instance_id: str) -> None:
        identifier = validate_instance_id(instance_id)
        with self._lock:
            if identifier not in self._profiles:
                raise KeyError(identifier)
            self._selected = identifier
            self.save()

    def list(self) -> list[InstanceProfile]:
        with self._lock:
            return [InstanceProfile.from_dict(item.to_disk()) for item in self._profiles.values()]

    def get(self, instance_id: str | None = None) -> InstanceProfile:
        identifier = validate_instance_id(instance_id or self._selected)
        with self._lock:
            if identifier not in self._profiles:
                raise KeyError(f"Сборка {identifier} не найдена")
            return InstanceProfile.from_dict(self._profiles[identifier].to_disk())

    def put(self, profile: InstanceProfile, *, replace: bool = True) -> None:
        self._validate_directory(profile.directory)
        with self._lock:
            if not replace and profile.id in self._profiles:
                raise FileExistsError(profile.id)
            self._profiles[profile.id] = InstanceProfile.from_dict(profile.to_disk())
            if not self._selected:
                self._selected = profile.id
            self.save()

    def patch(
        self,
        instance_id: str,
        changes: dict[str, Any],
        *,
        validator: Callable[[InstanceProfile, InstanceProfile], None] | None = None,
        prepare: Callable[[InstanceProfile], None] | None = None,
    ) -> InstanceProfile:
        with self._lock:
            current = self.get(instance_id)
            protected = {"id", "directory", "service", "managed_service", "installed_at", "launch_count", "last_launch_at"}
            data = current.to_disk()
            for key, value in changes.items():
                if key in data and key not in protected and key != "rcon_password":
                    data[key] = value
            if isinstance(changes.get("rcon_password"), str) and changes["rcon_password"]:
                data["rcon_password"] = changes["rcon_password"]
            updated = InstanceProfile.from_dict(data)
            if validator:
                validator(updated, current)
            if prepare:
                prepare(updated)
            self.put(updated)
            return updated

    def remove(self, instance_id: str) -> InstanceProfile:
        identifier = validate_instance_id(instance_id)
        with self._lock:
            profile = self._profiles.pop(identifier)
            if self._selected == identifier:
                self._selected = next(iter(self._profiles), None)
            self.save()
            return profile

    def record_launch(self, instance_id: str) -> None:
        with self._lock:
            profile = self._profiles[validate_instance_id(instance_id)]
            profile.launch_count += 1
            profile.last_launch_at = int(time.time())
            self.save()

    def discover_directories(self) -> list[dict[str, Any]]:
        known = {Path(item.directory).resolve(strict=False) for item in self.list()}
        discovered: list[dict[str, Any]] = []
        if not self.minecraft_root.is_dir():
            return discovered
        with os.scandir(self.minecraft_root) as entries:
            for entry in entries:
                try:
                    path = Path(entry.path).resolve(strict=False)
                    if not entry.is_dir(follow_symlinks=False) or path in known:
                        continue
                    path.relative_to(self.minecraft_root)
                except (OSError, ValueError):
                    continue
                detected = detect_pack(path)
                if detected.get("server_jar") or detected.get("scripts_requiring_review"):
                    discovered.append({"directory": str(path), "name": entry.name, **detected})
        return discovered[:100]
