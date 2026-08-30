"""Direct SSH Minecraft instance manager used by the desktop client.

The Debian host keeps one public Minecraft endpoint and one tmux console.  A
small root-side registry selects which isolated pack directory the existing
``dragonfyre.service`` starts.  The program below is sent over the verified SSH
channel, so an existing v2 installation needs no separate Agent upgrade.
"""

from __future__ import annotations

import base64
import json
import shlex
import textwrap
from typing import Any


MAX_MANAGER_RESPONSE_BYTES = 2 * 1024 * 1024


MANAGED_INSTANCE_RUNNER_PROGRAM = r'''#!/usr/bin/env python3
import json
import os
import shutil
import sys
from pathlib import Path

STORE = Path("/var/lib/server-control-minecraft/instances.json")
ROOT = Path("/opt/minecraft").resolve(strict=True)
TMUX_RUNNER = Path("/opt/server-control/current/minecraft_tmux_runner.py")

def fail(message):
    print("Server Control instance runner: " + message, file=sys.stderr, flush=True)
    raise SystemExit(2)

try:
    data = json.loads(STORE.read_text(encoding="utf-8"))
except Exception as error:
    fail("cannot read instance registry: " + str(error))

active = str(data.get("active") or "")
profiles = data.get("instances") if isinstance(data.get("instances"), list) else []
profile = next((item for item in profiles if isinstance(item, dict) and item.get("id") == active), None)
if not profile:
    fail("active instance is not selected")

directory = Path(str(profile.get("directory") or "")).resolve(strict=True)
try:
    directory.relative_to(ROOT)
except ValueError:
    fail("instance directory is outside /opt/minecraft")

command = profile.get("startup_command")
if not isinstance(command, list) or not command or not all(
    isinstance(item, str) and item and len(item) <= 2048 and "\0" not in item and "\n" not in item
    for item in command
):
    fail("invalid startup command")

try:
    ram_min = max(256, min(int(profile.get("ram_min_mb", 2048)), 131072))
    ram_max = max(ram_min, min(int(profile.get("ram_max_mb", 8192)), 131072))
except (TypeError, ValueError):
    fail("invalid RAM limits")

environment = dict(os.environ)
environment.update({
    "HOME": str(directory),
    "USER": "minecraft",
    "LOGNAME": "minecraft",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "SERVER_CONTROL_RAM_MIN": str(ram_min) + "M",
    "SERVER_CONTROL_RAM_MAX": str(ram_max) + "M",
    "MIN_RAM": str(ram_min) + "M",
    "MAX_RAM": str(ram_max) + "M",
    "RAM_MIN": str(ram_min) + "M",
    "RAM_MAX": str(ram_max) + "M",
})

if Path(command[0]).name == "java":
    executable = command[0] if os.path.isabs(command[0]) else (shutil.which(command[0]) or "")
    if not executable:
        fail("Java executable is unavailable")
    arguments = [item for item in command[1:] if not item.lower().startswith(("-xms", "-xmx"))]
    command = [executable, "-Xms%dM" % ram_min, "-Xmx%dM" % ram_max, *arguments]
else:
    wrapper_dir = Path("/run/server-control-minecraft/java-wrapper")
    wrapper_dir.mkdir(parents=True, exist_ok=True)
    wrapper = wrapper_dir / "java"
    wrapper.write_text(
        "#!/usr/bin/python3\n"
        "import os,sys\n"
        "args=[x for x in sys.argv[1:] if not x.lower().startswith(('-xms','-xmx'))]\n"
        "os.execv('/usr/bin/java',['/usr/bin/java','-Xms%dM','-Xmx%dM',*args])\n" % (ram_min, ram_max),
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    environment["PATH"] = str(wrapper_dir) + ":/usr/local/bin:/usr/bin:/bin"

if not TMUX_RUNNER.is_file():
    fail("minecraft_tmux_runner.py is unavailable; reinstall the v2 console package")

exit_file = "/run/server-control-minecraft/dragonfyre.exit"
runner = [
    "/usr/bin/python3", str(TMUX_RUNNER),
    "--session", "dragonfyre",
    "--workdir", str(directory),
    "--exit-file", exit_file,
    "--", *command,
]
os.execve(runner[0], runner, environment)
'''.strip()


REMOTE_INSTANCE_MANAGER_PROGRAM = textwrap.dedent(
    r"""
    import fcntl
    import hashlib
    import json
    import os
    import pwd
    import grp
    import re
    import shutil
    import stat
    import subprocess
    import sys
    import tempfile
    import time
    import uuid
    import zipfile
    from pathlib import Path

    ROOT = Path("/opt/minecraft")
    INSTANCES_ROOT = ROOT / "instances"
    CONFIG_DIR = Path("/etc/server-control")
    STATE_DIR = Path("/var/lib/server-control-minecraft")
    STORE = STATE_DIR / "instances.json"
    LEGACY_STORE = CONFIG_DIR / "minecraft-instances.json"
    LOCK = Path("/run/lock/server-control-instances.lock")
    SERVICE = Path("/etc/systemd/system/dragonfyre.service")
    SERVICE_BACKUP = Path("/etc/systemd/system/dragonfyre.service.pre-instance-manager")
    RUNTIME = Path("/opt/server-control/current/managed_instance_runner.py")
    TMUX_RUNNER = Path("/opt/server-control/current/minecraft_tmux_runner.py")
    INSTANCE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
    MAX_ARCHIVE_BYTES = 12 * 1024 * 1024 * 1024
    MAX_EXTRACTED_BYTES = 40 * 1024 * 1024 * 1024
    MAX_ARCHIVE_FILES = 250000
    MAX_PROPERTIES_BYTES = 1024 * 1024
    MAX_TRANSLATION_TASKS = 250000
    MAX_TRANSLATION_ARCHIVE_BYTES = 512 * 1024 * 1024
    MAX_LANG_FILE_BYTES = 8 * 1024 * 1024
    MAX_QUEST_FILE_BYTES = 16 * 1024 * 1024
    MAX_QUEST_FILES = 10000

    RUNNER_PROGRAM = __RUNNER_PROGRAM__

    SERVICE_UNIT = '''[Unit]
    Description=Server Control active Minecraft instance (direct tmux console)
    Wants=network-online.target
    After=network-online.target

    [Service]
    Type=simple
    User=minecraft
    Group=minecraft
    Environment=HOME=/opt/minecraft
    Environment=SHELL=/bin/bash
    RuntimeDirectory=server-control-minecraft
    RuntimeDirectoryMode=0750
    ExecStart=/usr/bin/python3 /opt/server-control/current/managed_instance_runner.py
    Restart=on-failure
    RestartSec=10
    TimeoutStopSec=180
    KillMode=mixed
    LimitNOFILE=65536
    UMask=0007
    NoNewPrivileges=true
    PrivateTmp=false
    ProtectHome=true
    ProtectSystem=full
    ReadWritePaths=/opt/minecraft /run/server-control-minecraft

    [Install]
    WantedBy=multi-user.target
    '''

    def atomic_write(path, content, mode=0o640, group=None, user=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, mode)
            if group or user:
                uid = pwd.getpwnam(user).pw_uid if user else 0
                gid = grp.getgrnam(group).gr_gid if group else -1
                os.chown(temporary, uid, gid)
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except OSError:
                pass

    def command(arguments, timeout=240, check=True):
        result = subprocess.run(
            arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False,
            env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"},
        )
        if check and result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Команда завершилась с ошибкой")
        return result

    def valid_id(value):
        value = str(value or "").strip().lower()
        if not INSTANCE_ID.fullmatch(value):
            raise ValueError("ID должен содержать только a-z, 0-9, _ и -")
        return value

    def safe_directory(value, must_exist=True):
        path = Path(str(value or "")).resolve(strict=must_exist)
        try:
            path.relative_to(ROOT.resolve(strict=True))
        except ValueError:
            raise ValueError("Папка сборки должна находиться внутри /opt/minecraft")
        if path in {ROOT.resolve(), INSTANCES_ROOT.resolve(strict=False)}:
            raise ValueError("Нельзя использовать корневую папку Minecraft")
        return path

    def read_store():
        source = STORE if STORE.is_file() else LEGACY_STORE
        if source.is_file():
            try:
                data = json.loads(source.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                data = {}
        else:
            data = {}
        values = data.get("instances") if isinstance(data.get("instances"), list) else []
        instances = []
        seen = set()
        for item in values:
            if not isinstance(item, dict):
                continue
            try:
                identifier = valid_id(item.get("id"))
                directory = str(safe_directory(item.get("directory"), must_exist=False))
            except (ValueError, OSError):
                continue
            if identifier in seen:
                continue
            seen.add(identifier)
            value = dict(item)
            value["id"] = identifier
            value["directory"] = directory
            instances.append(value)
        active = str(data.get("active") or "")
        if active not in seen:
            active = instances[0]["id"] if instances else ""
        return {"schema": 1, "active": active, "instances": instances}

    def save_store(data):
        payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        atomic_write(STORE, payload, mode=0o640, group="minecraft")

    def properties_port(directory):
        path = Path(directory) / "server.properties"
        if not path.is_file():
            return 25565
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("server-port="):
                try:
                    value = int(line.partition("=")[2].strip())
                    if 1 <= value <= 65535:
                        return value
                except ValueError:
                    pass
        return 25565

    def safe_properties_path(directory):
        directory = safe_directory(directory)
        path = directory / "server.properties"
        if path.is_symlink():
            raise ValueError("server.properties не должен быть символической ссылкой")
        try:
            path.resolve(strict=False).relative_to(directory)
        except ValueError:
            raise ValueError("Некорректный путь server.properties")
        if path.exists() and not path.is_file():
            raise ValueError("server.properties не является обычным файлом")
        return path

    def read_properties(directory):
        path = safe_properties_path(directory)
        if not path.is_file():
            return ""
        if path.stat().st_size > MAX_PROPERTIES_BYTES:
            raise ValueError("server.properties больше допустимого размера 1 MB")
        return path.read_text(encoding="utf-8-sig", errors="replace")

    def parse_properties(content):
        values = {}
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", "!")):
                continue
            key, separator, value = line.partition("=")
            if not separator:
                key, separator, value = line.partition(":")
            key = key.strip()
            if separator and key:
                values[key] = value.strip()
        return values

    def write_properties(directory, content):
        if not isinstance(content, str) or "\0" in content:
            raise ValueError("Некорректное содержимое server.properties")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_PROPERTIES_BYTES:
            raise ValueError("server.properties больше допустимого размера 1 MB")
        normalized = content.replace("\r\n", "\n").replace("\r", "\n")
        if normalized and not normalized.endswith("\n"):
            normalized += "\n"
        path = safe_properties_path(directory)
        atomic_write(path, normalized, mode=0o660, group="minecraft", user="minecraft")
        return normalized

    def set_property(directory, key, value):
        content = read_properties(directory)
        lines = content.splitlines()
        output = []
        found = False
        for line in lines:
            if line.startswith(key + "="):
                if not found:
                    output.append(key + "=" + str(value))
                    found = True
            else:
                output.append(line)
        if not found:
            output.append(key + "=" + str(value))
        write_properties(directory, "\n".join(output) + "\n")

    def detect(directory):
        start = []
        for script in ("start.sh", "run.sh", "ServerStart.sh", "startserver.sh", "server-start.sh"):
            if (directory / script).is_file():
                start = ["/bin/bash", script]
                break
        jars = sorted(path for path in directory.glob("*.jar") if path.is_file())
        loader = "unknown"
        loader_version = "unknown"
        minecraft_version = "unknown"
        server_jar = None
        for jar in jars[:300]:
            lower = jar.name.lower()
            if "neoforge" in lower:
                loader = "NeoForge"
            elif "forge" in lower and loader == "unknown":
                loader = "Forge"
            elif "fabric" in lower and loader == "unknown":
                loader = "Fabric"
            version = re.search(r"(?:minecraft|server|forge)[-_](1\.\d+(?:\.\d+)?)", lower)
            if version and minecraft_version == "unknown":
                minecraft_version = version.group(1)
            loader_match = re.search(r"(?:neo)?forge[-_]?([0-9][0-9.\-]+)", lower)
            if loader_match and loader_version == "unknown":
                loader_version = loader_match.group(1).strip(".-")
            if lower in {"server.jar", "minecraft_server.jar", "fabric-server-launch.jar"}:
                server_jar = jar.name
        for manifest_name in ("manifest.json", "minecraftinstance.json"):
            manifest = directory / manifest_name
            if not manifest.is_file() or manifest.stat().st_size > 2 * 1024 * 1024:
                continue
            try:
                data = json.loads(manifest.read_text(encoding="utf-8-sig"))
            except Exception:
                continue
            minecraft = data.get("minecraft") if isinstance(data.get("minecraft"), dict) else {}
            version = minecraft.get("version") or data.get("minecraftVersion")
            if isinstance(version, str) and len(version) < 40:
                minecraft_version = version
            mod_loaders = minecraft.get("modLoaders") if isinstance(minecraft.get("modLoaders"), list) else []
            if mod_loaders and isinstance(mod_loaders[0], dict):
                loader_id = str(mod_loaders[0].get("id") or "")
                if "neoforge" in loader_id.lower():
                    loader = "NeoForge"
                elif "forge" in loader_id.lower():
                    loader = "Forge"
                elif "fabric" in loader_id.lower():
                    loader = "Fabric"
                loader_version = loader_id.split("-", 1)[-1] or loader_version
        if not start:
            argument_files = sorted([
                *directory.glob("libraries/net/minecraftforge/forge/*/unix_args.txt"),
                *directory.glob("libraries/net/neoforged/neoforge/*/unix_args.txt"),
            ])
            if argument_files:
                start = ["/usr/bin/java", "@" + argument_files[-1].relative_to(directory).as_posix(), "nogui"]
            elif server_jar:
                start = ["/usr/bin/java", "-jar", server_jar, "nogui"]
        if not start:
            raise ValueError("Не найден start.sh, run.sh, Forge args или server.jar")
        return {
            "startup_command": start,
            "minecraft_version": minecraft_version,
            "loader": loader,
            "loader_version": loader_version,
            "port": properties_port(directory),
        }

    def profile(identifier, name, directory, ram_min=2048, ram_max=8192, port=None):
        detected = detect(directory)
        return {
            "id": valid_id(identifier),
            "name": str(name or identifier).strip()[:80] or identifier,
            "directory": str(directory),
            "startup_command": detected["startup_command"],
            "ram_min_mb": max(256, min(int(ram_min), 131072)),
            "ram_max_mb": max(max(256, min(int(ram_min), 131072)), min(int(ram_max), 131072)),
            "port": int(port if port is not None else detected["port"]),
            "minecraft_version": detected["minecraft_version"],
            "loader": detected["loader"],
            "loader_version": detected["loader_version"],
            "created_at": int(time.time()),
            "last_started_at": None,
            "managed_directory": str(directory).startswith(str(INSTANCES_ROOT) + os.sep),
        }

    def ensure_runtime(data):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        minecraft_gid = grp.getgrnam("minecraft").gr_gid
        os.chown(STATE_DIR, 0, minecraft_gid)
        os.chmod(STATE_DIR, 0o750)
        INSTANCES_ROOT.mkdir(parents=True, exist_ok=True)
        if not TMUX_RUNNER.is_file():
            raise RuntimeError("Не найден minecraft_tmux_runner.py. Сначала установите консоль Server Control v2.")
        if not data["instances"]:
            legacy = ROOT / "dragonfyre"
            if legacy.is_dir():
                value = profile("dragonfyre", "Dragonfyre", legacy, 2048, 8192)
                data["instances"].append(value)
                data["active"] = "dragonfyre"
                save_store(data)
        if not STORE.is_file():
            save_store(data)
        runtime_text = RUNNER_PROGRAM.rstrip() + "\n"
        if not RUNTIME.is_file() or RUNTIME.read_text(encoding="utf-8", errors="replace") != runtime_text:
            atomic_write(RUNTIME, runtime_text, mode=0o755)
        service_text = "\n".join(line[4:] if line.startswith("    ") else line for line in SERVICE_UNIT.strip().splitlines()) + "\n"
        current = SERVICE.read_text(encoding="utf-8", errors="replace") if SERVICE.is_file() else ""
        if current != service_text:
            if SERVICE.is_file() and not SERVICE_BACKUP.exists():
                shutil.copy2(SERVICE, SERVICE_BACKUP)
            atomic_write(SERVICE, service_text, mode=0o644)
            command(["/usr/bin/systemctl", "daemon-reload"], timeout=30)
            command(["/usr/bin/systemctl", "enable", "dragonfyre.service"], timeout=30)

    def service_state():
        result = command([
            "/usr/bin/systemctl", "show", "dragonfyre.service",
            "--property=ActiveState,SubState,MainPID,Result,NRestarts,ExecMainStatus", "--no-pager",
        ], timeout=15, check=False)
        values = {}
        for line in result.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value
        try:
            pid = int(values.get("MainPID", "0") or 0) or None
        except ValueError:
            pid = None
        try:
            restart_count = int(values.get("NRestarts", "0") or 0)
        except ValueError:
            restart_count = 0
        try:
            exit_status = int(values.get("ExecMainStatus", "0") or 0)
        except ValueError:
            exit_status = 0
        return {
            "active_state": values.get("ActiveState", "unknown"),
            "sub_state": values.get("SubState", "unknown"),
            "pid": pid,
            "result": values.get("Result", "unknown"),
            "restart_count": restart_count,
            "exit_status": exit_status,
        }

    def public_list(data):
        state = service_state()
        values = []
        for item in data["instances"]:
            value = dict(item)
            value["exists"] = Path(value["directory"]).is_dir()
            value["active"] = value["id"] == data["active"]
            restart_loop = state["sub_state"] == "auto-restart" and state["restart_count"] > 0
            value["state"] = ("failed" if restart_loop else state["active_state"]) if value["active"] else "inactive"
            value["service_sub_state"] = state["sub_state"] if value["active"] else "dead"
            value["pid"] = state["pid"] if value["active"] else None
            values.append(value)
        return {"ok": True, "active_id": data["active"], "instances": values, "service": state}

    def find(data, identifier):
        identifier = valid_id(identifier)
        value = next((item for item in data["instances"] if item["id"] == identifier), None)
        if not value:
            raise KeyError("Сборка не найдена: " + identifier)
        return value

    def ensure_unique(data, identifier):
        identifier = valid_id(identifier)
        if any(item["id"] == identifier for item in data["instances"]):
            raise FileExistsError("Сборка с таким ID уже существует")
        target = INSTANCES_ROOT / identifier
        if target.exists():
            raise FileExistsError("Папка этой сборки уже существует")
        return identifier, target

    def stop_service():
        state = service_state()["active_state"]
        if state not in {"inactive", "failed", "unknown"}:
            command(["/usr/bin/systemctl", "stop", "dragonfyre.service"], timeout=220)

    def set_owner(directory):
        uid = pwd.getpwnam("minecraft").pw_uid
        gid = grp.getgrnam("minecraft").gr_gid
        for root, directories, files in os.walk(directory, followlinks=False):
            os.chown(root, uid, gid)
            for name in directories:
                path = Path(root) / name
                if not path.is_symlink():
                    os.chown(path, uid, gid)
            for name in files:
                path = Path(root) / name
                if not path.is_symlink():
                    os.chown(path, uid, gid)

    def safe_extract(archive, destination):
        if archive.stat().st_size > MAX_ARCHIVE_BYTES:
            raise ValueError("ZIP больше допустимого размера 12 GB")
        with zipfile.ZipFile(archive) as source:
            members = source.infolist()
            if len(members) > MAX_ARCHIVE_FILES:
                raise ValueError("В ZIP слишком много файлов")
            total = 0
            for member in members:
                name = member.filename.replace("\\", "/")
                path = Path(name)
                mode = member.external_attr >> 16
                total += max(0, int(member.file_size))
                if total > MAX_EXTRACTED_BYTES:
                    raise ValueError("Распакованный ZIP превышает 40 GB")
                if not name or name.startswith("/") or "\0" in name or ".." in path.parts:
                    raise ValueError("ZIP содержит небезопасный путь")
                if stat.S_ISLNK(mode):
                    raise ValueError("Символические ссылки в ZIP запрещены")
            source.extractall(destination)

    def flatten_single_root(staging):
        children = [path for path in staging.iterdir() if path.name not in {"__MACOSX"}]
        directories = [path for path in children if path.is_dir()]
        files = [path for path in children if path.is_file()]
        if len(directories) == 1 and not files:
            inner = directories[0]
            temporary = staging.with_name(staging.name + "-flat")
            inner.rename(temporary)
            shutil.rmtree(staging)
            temporary.rename(staging)

    def safe_export_name(value):
        result = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(value or "")).strip("._")
        return result[:120] or "unknown"

    def write_export_json(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def parse_language_payload(payload, suffix):
        text = payload.decode("utf-8-sig", "replace")
        if suffix.casefold() == ".json":
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                return {}
            if not isinstance(value, dict):
                return {}
            return {str(key): str(item) for key, item in value.items() if isinstance(item, (str, int, float, bool))}
        values = {}
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", "!", "//")):
                continue
            key, separator, value = line.partition("=")
            if not separator:
                key, separator, value = line.partition(":")
            key = key.strip().strip('"\'')
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                try:
                    value = json.loads(value) if value[0] == '"' else value[1:-1]
                except json.JSONDecodeError:
                    value = value[1:-1]
            if separator and key:
                values[key] = str(value)
        return values

    def normalized_translation(value):
        text = re.sub(r"§.", "", str(value or ""))
        text = re.sub(r"%(?:\d+\$)?[a-zA-Z%]", "", text)
        text = re.sub(r"\{[^{}]*\}", "", text)
        return re.sub(r"\s+", " ", text).strip().casefold()

    def looks_english(value):
        text = normalized_translation(value)
        latin_words = re.findall(r"[a-zA-Z]{3,}", text)
        if not latin_words:
            return False
        if re.search(r"[а-яё]", text, re.IGNORECASE):
            return len(latin_words) >= 2 or sum(len(word) for word in latin_words) >= 12
        if not re.search(r"\s", text) and re.fullmatch(r"[a-z0-9_.:/+@#-]+", text, re.IGNORECASE) and re.search(r"[_.:/]", text):
            return False
        return True

    def translation_reason(source, current):
        if current is None:
            return "missing" if looks_english(source) else ""
        source_normalized = normalized_translation(source)
        current_normalized = normalized_translation(current)
        if source_normalized and source_normalized == current_normalized and looks_english(source):
            return "identical_to_english"
        if looks_english(current):
            return "contains_english"
        return ""

    def append_translation_task(tasks, task):
        if len(tasks) >= MAX_TRANSLATION_TASKS:
            return False
        value = dict(task)
        value["task_id"] = "translation-%06d" % (len(tasks) + 1)
        tasks.append(value)
        return True

    def scan_mod_translations(directory, export_root, tasks):
        mods_dir = directory / "mods"
        summaries = []
        jars_scanned = 0
        lang_files_scanned = 0
        errors = []
        if not mods_dir.is_dir():
            return {"jars_scanned": 0, "lang_files_scanned": 0, "incomplete": [], "errors": []}
        for jar_index, jar in enumerate(sorted(mods_dir.glob("*.jar"), key=lambda path: path.name.casefold())[:2000], start=1):
            if jar.is_symlink() or not jar.is_file():
                continue
            jars_scanned += 1
            try:
                with zipfile.ZipFile(jar) as source_zip:
                    names = set(source_zip.namelist())
                    english_files = sorted(
                        name for name in names
                        if re.fullmatch(r"assets/[^/]+/lang/en_us\.(?:json|lang)", name, re.IGNORECASE)
                    )
                    for english_name in english_files:
                        info = source_zip.getinfo(english_name)
                        if info.file_size > MAX_LANG_FILE_BYTES:
                            errors.append(jar.name + ": " + english_name + " больше 8 MB")
                            continue
                        lang_files_scanned += 1
                        suffix = Path(english_name).suffix.casefold()
                        english = parse_language_payload(source_zip.read(info), suffix)
                        if not english:
                            continue
                        namespace = english_name.split("/")[1]
                        russian_candidates = [
                            english_name.replace("en_us" + suffix, "ru_ru" + suffix),
                            english_name.rsplit("/", 1)[0] + "/ru_ru.json",
                            english_name.rsplit("/", 1)[0] + "/ru_ru.lang",
                        ]
                        russian = {}
                        russian_name = ""
                        for candidate in russian_candidates:
                            if candidate not in names:
                                continue
                            candidate_info = source_zip.getinfo(candidate)
                            if candidate_info.file_size <= MAX_LANG_FILE_BYTES:
                                russian = parse_language_payload(source_zip.read(candidate_info), Path(candidate).suffix)
                                russian_name = candidate
                                break
                        missing = {}
                        reason_counts = {"missing": 0, "identical_to_english": 0, "contains_english": 0}
                        for key, english_text in english.items():
                            current = russian.get(key)
                            reason = translation_reason(english_text, current)
                            if not reason:
                                continue
                            reason_counts[reason] += 1
                            missing[key] = english_text
                            append_translation_task(tasks, {
                                "kind": "mod_language",
                                "reason": reason,
                                "source_file": jar.name + "!/" + english_name,
                                "target_file": jar.name + "!/" + english_name.replace("en_us", "ru_ru"),
                                "namespace": namespace,
                                "key": key,
                                "source_text": english_text,
                                "current_text": current,
                            })
                        if not missing:
                            continue
                        target = export_root / "mods" / ("%03d_%s" % (jar_index, safe_export_name(jar.stem))) / safe_export_name(namespace)
                        write_export_json(target / "en_us.json", english)
                        write_export_json(target / "current_ru_ru.json", russian)
                        write_export_json(target / "translation_template_ru_ru.json", {**russian, **missing})
                        summary = {
                            "jar": jar.name,
                            "namespace": namespace,
                            "english_file": english_name,
                            "russian_file": russian_name or None,
                            "english_keys": len(english),
                            "russian_keys": len(russian),
                            "needs_translation": len(missing),
                            "reasons": reason_counts,
                            "template": str((target / "translation_template_ru_ru.json").relative_to(export_root)),
                        }
                        summaries.append(summary)
            except (OSError, zipfile.BadZipFile, KeyError, RuntimeError) as error:
                errors.append(jar.name + ": " + str(error))
        return {
            "jars_scanned": jars_scanned,
            "lang_files_scanned": lang_files_scanned,
            "incomplete": summaries,
            "errors": errors[:200],
        }

    def quest_candidate_files(directory):
        roots = [
            directory / "config" / "ftbquests",
            directory / "defaultconfigs" / "ftbquests",
            directory / "world" / "serverconfig" / "ftbquests",
            directory / "config" / "betterquesting",
            directory / "kubejs" / "data",
        ]
        candidates = []
        seen = set()
        for root in roots:
            if not root.is_dir() or root.is_symlink():
                continue
            try:
                iterator = root.rglob("*")
                for path in iterator:
                    if len(candidates) >= MAX_QUEST_FILES:
                        return candidates, True
                    if path.is_symlink() or not path.is_file() or path.suffix.casefold() not in {".snbt", ".json", ".json5", ".lang"}:
                        continue
                    relative = path.relative_to(directory).as_posix()
                    lowered = relative.casefold()
                    if root == directory / "kubejs" / "data" and not any(token in lowered for token in ("ftbquest", "/quests/", "/quest/")):
                        continue
                    if relative not in seen:
                        seen.add(relative)
                        candidates.append(path)
            except OSError:
                continue
        return candidates, False

    def quest_json_strings(value, path=()):
        results = []
        if isinstance(value, dict):
            for key, item in value.items():
                results.extend(quest_json_strings(item, (*path, str(key))))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                results.extend(quest_json_strings(item, (*path, str(index))))
        elif isinstance(value, str) and looks_english(value):
            technical = {"id", "type", "icon", "item", "fluid", "entity", "command", "dependency", "filename", "path"}
            if not path or not any(part.casefold() in technical for part in path[-2:]):
                results.append((".".join(path), value))
        return results

    def quest_text_strings(content):
        results = []
        technical_fields = {"id", "type", "icon", "item", "fluid", "entity", "command", "dependency", "filename", "path", "shape"}
        quoted = re.compile(r'"((?:\\.|[^"\\])*)"')
        for line_number, line in enumerate(content.splitlines(), start=1):
            field_match = re.match(r"\s*([a-zA-Z0-9_.-]+)\s*[:=]", line)
            field = field_match.group(1) if field_match else ""
            if field.casefold() in technical_fields:
                continue
            for match in quoted.finditer(line):
                if line[match.end():].lstrip().startswith(":"):
                    continue
                try:
                    value = json.loads('"' + match.group(1) + '"')
                except json.JSONDecodeError:
                    value = match.group(1).replace('\\"', '"')
                if looks_english(value):
                    results.append((line_number, field, value))
        return results

    def scan_quest_translations(directory, export_root, tasks):
        candidates, file_limit_reached = quest_candidate_files(directory)
        files_scanned = 0
        files_with_tasks = 0
        strings_found = 0
        errors = []
        copied = []
        language_sets = []
        handled = set()
        candidate_set = {str(path): path for path in candidates}
        for english_path in candidates:
            if english_path.stem.casefold() != "en_us":
                continue
            try:
                if english_path.stat().st_size > MAX_LANG_FILE_BYTES:
                    continue
                english = parse_language_payload(english_path.read_bytes(), english_path.suffix)
            except OSError:
                continue
            if not english:
                continue
            russian_path = None
            for suffix in (english_path.suffix, ".json", ".snbt", ".lang"):
                candidate = english_path.with_name("ru_ru" + suffix)
                if str(candidate) in candidate_set:
                    russian_path = candidate
                    break
            russian = {}
            if russian_path is not None:
                try:
                    if russian_path.stat().st_size <= MAX_LANG_FILE_BYTES:
                        russian = parse_language_payload(russian_path.read_bytes(), russian_path.suffix)
                except OSError:
                    russian = {}
            relative = english_path.relative_to(directory).as_posix()
            missing = {}
            reasons = {"missing": 0, "identical_to_english": 0, "contains_english": 0}
            for key, english_text in english.items():
                current = russian.get(key)
                reason = translation_reason(english_text, current)
                if not reason:
                    continue
                reasons[reason] += 1
                missing[key] = english_text
                append_translation_task(tasks, {
                    "kind": "quest_language",
                    "reason": reason,
                    "source_file": relative,
                    "target_file": str(english_path.with_name("ru_ru" + english_path.suffix).relative_to(directory).as_posix()),
                    "key": key,
                    "source_text": english_text,
                    "current_text": current,
                })
            handled.add(str(english_path))
            if russian_path is not None:
                handled.add(str(russian_path))
            files_scanned += 1 + (1 if russian_path is not None else 0)
            if not missing:
                continue
            files_with_tasks += 1
            strings_found += len(missing)
            target = export_root / "quests" / "lang" / ("%03d_%s" % (len(language_sets) + 1, safe_export_name(english_path.parent.name)))
            write_export_json(target / "en_us.json", english)
            write_export_json(target / "current_ru_ru.json", russian)
            write_export_json(target / "translation_template_ru_ru.json", {**russian, **missing})
            language_sets.append({
                "english_file": relative,
                "russian_file": russian_path.relative_to(directory).as_posix() if russian_path is not None else None,
                "needs_translation": len(missing),
                "reasons": reasons,
                "template": str((target / "translation_template_ru_ru.json").relative_to(export_root)),
            })
        for path in candidates:
            if str(path) in handled:
                continue
            try:
                if path.stat().st_size > MAX_QUEST_FILE_BYTES:
                    errors.append(path.relative_to(directory).as_posix() + ": файл больше 16 MB")
                    continue
                content = path.read_text(encoding="utf-8-sig", errors="replace")
            except OSError as error:
                errors.append(str(path) + ": " + str(error))
                continue
            files_scanned += 1
            relative = path.relative_to(directory).as_posix()
            found = []
            if path.suffix.casefold() == ".json":
                try:
                    parsed = json.loads(content)
                except json.JSONDecodeError:
                    parsed = None
                if parsed is not None:
                    found = [(None, field, text) for field, text in quest_json_strings(parsed)]
            if not found:
                found = quest_text_strings(content)
            if not found:
                continue
            files_with_tasks += 1
            strings_found += len(found)
            target = export_root / "quests" / "source" / Path(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            copied.append(relative)
            for line_number, field, text in found:
                append_translation_task(tasks, {
                    "kind": "quest_text",
                    "reason": "contains_english",
                    "source_file": relative,
                    "line": line_number,
                    "field": field or None,
                    "source_text": text,
                    "current_text": None,
                })
        return {
            "files_scanned": files_scanned,
            "files_with_tasks": files_with_tasks,
            "strings_found": strings_found,
            "source_files": copied,
            "language_sets": language_sets,
            "file_limit_reached": file_limit_reached,
            "errors": errors[:200],
        }

    def build_translation_export(value):
        directory = safe_directory(value["directory"])
        for stale in Path("/var/tmp").glob("server-control-translation-*.zip"):
            try:
                if re.fullmatch(r"server-control-translation-[0-9a-f]{32}\.zip", stale.name) and time.time() - stale.stat().st_mtime > 86400:
                    stale.unlink()
            except OSError:
                pass
        export_path = Path("/var/tmp/server-control-translation-" + uuid.uuid4().hex + ".zip")
        tasks = []
        with tempfile.TemporaryDirectory(prefix="server-control-translation-") as temporary:
            export_root = Path(temporary) / "translation-export"
            export_root.mkdir(parents=True)
            mods = scan_mod_translations(directory, export_root, tasks)
            quests = scan_quest_translations(directory, export_root, tasks)
            manifest = {
                "format": "server-control-translation-export-v1",
                "generated_at": int(time.time()),
                "instance": {
                    "id": value["id"],
                    "name": value.get("name"),
                    "minecraft_version": value.get("minecraft_version"),
                    "loader": value.get("loader"),
                    "loader_version": value.get("loader_version"),
                },
                "statistics": {
                    "translation_tasks": len(tasks),
                    "task_limit_reached": len(tasks) >= MAX_TRANSLATION_TASKS,
                    "mods": mods,
                    "quests": quests,
                },
            }
            write_export_json(export_root / "manifest.json", manifest)
            write_export_json(export_root / "translation_tasks.json", tasks)
            report = [
                "# Проверка перевода Minecraft-сборки",
                "",
                "Сборка: %s (`%s`)" % (value.get("name") or value["id"], value["id"]),
                "",
                "- Просканировано JAR модов: %d" % mods["jars_scanned"],
                "- Модов/пространств с неполным переводом: %d" % len(mods["incomplete"]),
                "- Просканировано файлов квестов: %d" % quests["files_scanned"],
                "- Файлов квестов с английским текстом: %d" % quests["files_with_tasks"],
                "- Всего заданий на перевод: %d" % len(tasks),
                "",
                "## Неполные переводы модов",
                "",
            ]
            for item in mods["incomplete"]:
                report.append("- `%s` / `%s`: %d строк → `%s`" % (
                    item["jar"], item["namespace"], item["needs_translation"], item["template"],
                ))
            report.extend([
                "",
                "## Как использовать архив",
                "",
                "Передайте весь ZIP для перевода. Ключи JSON, task_id, пути и формат файлов менять нельзя; переводится только текст.",
                "Файлы `translation_template_ru_ru.json` содержат существующий русский перевод и английские строки, которые нужно заменить.",
                "В `quests/source` лежат только файлы квестов, где найден вероятно непереведённый текст.",
                "",
            ])
            (export_root / "REPORT.md").write_text("\n".join(report), encoding="utf-8")
            (export_root / "README.txt").write_text(
                "Отправьте этот ZIP в ChatGPT и попросите перевести все задания из translation_tasks.json.\n"
                "Сохраняйте ключи, task_id, управляющие коды, плейсхолдеры (%s, %1$s, {0}) и структуру файлов.\n",
                encoding="utf-8",
            )
            temporary_zip = export_path.with_suffix(".tmp")
            try:
                with zipfile.ZipFile(temporary_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as output:
                    for path in sorted(export_root.rglob("*")):
                        if path.is_file() and not path.is_symlink():
                            output.write(path, (Path("translation-export") / path.relative_to(export_root)).as_posix())
                if temporary_zip.stat().st_size > MAX_TRANSLATION_ARCHIVE_BYTES:
                    raise ValueError("Архив перевода больше допустимого размера 512 MB")
                os.chmod(temporary_zip, 0o644)
                os.replace(temporary_zip, export_path)
            finally:
                try:
                    temporary_zip.unlink()
                except OSError:
                    pass
        return {
            "archive": str(export_path),
            "size": export_path.stat().st_size,
            "tasks": len(tasks),
            "mods_incomplete": len(mods["incomplete"]),
            "quest_files": quests["files_with_tasks"],
            "task_limit_reached": len(tasks) >= MAX_TRANSLATION_TASKS,
        }

    def dispatch(payload, data):
        action = str(payload.get("action") or "list")
        if action == "list":
            return public_list(data)

        if action == "properties_get":
            value = find(data, payload.get("id"))
            directory = safe_directory(value["directory"])
            content = read_properties(directory)
            return {
                "ok": True,
                "instance_id": value["id"],
                "content": content,
                "values": parse_properties(content),
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "active": value["id"] == data["active"],
                "service": service_state(),
            }

        if action == "properties_set":
            value = find(data, payload.get("id"))
            directory = safe_directory(value["directory"])
            previous_content = read_properties(directory)
            previous_sha256 = hashlib.sha256(previous_content.encode("utf-8")).hexdigest()
            previous_port = int(value.get("port", 25565))
            expected = str(payload.get("expected_sha256") or "")
            if expected and expected != previous_sha256:
                raise RuntimeError("server.properties изменился после открытия. Откройте настройки заново, чтобы не потерять новые данные.")
            updated = write_properties(directory, payload.get("content"))
            values = parse_properties(updated)
            try:
                configured_port = int(values.get("server-port", value.get("port", 25565)))
            except (TypeError, ValueError):
                configured_port = int(value.get("port", 25565))
            if 1 <= configured_port <= 65535:
                value["port"] = configured_port
                save_store(data)
            running = value["id"] == data["active"] and service_state()["active_state"] in {"active", "activating"}
            restarted = False
            if payload.get("restart") is True and running:
                result = command(["/usr/bin/systemctl", "restart", "dragonfyre.service"], timeout=220, check=False)
                if result.returncode != 0:
                    write_properties(directory, previous_content)
                    value["port"] = previous_port
                    save_store(data)
                    command(["/usr/bin/systemctl", "restart", "dragonfyre.service"], timeout=220, check=False)
                    raise RuntimeError(result.stderr.strip() or "Настройки сохранены, но Minecraft не удалось перезапустить")
                time.sleep(2)
                restarted_state = service_state()
                restart_loop = restarted_state["sub_state"] == "auto-restart" and restarted_state["restart_count"] > 0
                if restarted_state["active_state"] in {"failed", "inactive"} or restart_loop:
                    journal = command(
                        ["/usr/bin/journalctl", "-u", "dragonfyre.service", "-n", "12", "--no-pager"],
                        timeout=30,
                        check=False,
                    ).stdout.strip()
                    write_properties(directory, previous_content)
                    value["port"] = previous_port
                    save_store(data)
                    command(["/usr/bin/systemctl", "reset-failed", "dragonfyre.service"], timeout=30, check=False)
                    command(["/usr/bin/systemctl", "restart", "dragonfyre.service"], timeout=220, check=False)
                    raise RuntimeError("Новые настройки вызвали ошибку запуска и были отменены.\n" + journal[-6000:])
                restarted = True
            return {
                "ok": True,
                "properties_saved": True,
                "restart_required": running and not restarted,
                "restarted": restarted,
                "content_sha256": hashlib.sha256(updated.encode("utf-8")).hexdigest(),
                **public_list(data),
            }

        if action == "translation_scan":
            value = find(data, payload.get("id"))
            result = build_translation_export(value)
            return {"ok": True, "instance_id": value["id"], **result}

        if action == "translation_cleanup":
            archive = Path(str(payload.get("archive") or ""))
            if not re.fullmatch(r"/var/tmp/server-control-translation-[0-9a-f]{32}\.zip", str(archive)):
                raise ValueError("Некорректный путь архива перевода")
            try:
                archive.unlink()
            except FileNotFoundError:
                pass
            return {"ok": True}

        if action == "import_existing":
            identifier = valid_id(payload.get("id"))
            if any(item["id"] == identifier for item in data["instances"]):
                raise FileExistsError("Сборка с таким ID уже зарегистрирована")
            directory = safe_directory(payload.get("directory"))
            value = profile(identifier, payload.get("name"), directory, payload.get("ram_min_mb", 2048), payload.get("ram_max_mb", 8192))
            data["instances"].append(value)
            if not data["active"]:
                data["active"] = identifier
            save_store(data)
            return {"ok": True, "instance": value, **public_list(data)}

        if action == "import_zip":
            identifier, target = ensure_unique(data, payload.get("id"))
            archive = Path(str(payload.get("archive") or "")).resolve(strict=True)
            if not re.fullmatch(r"server-control-[0-9a-f]{32}\.zip", archive.name):
                raise ValueError("Некорректный временный архив")
            if payload.get("accept_eula") is not True:
                raise ValueError("Для установки нужно принять Minecraft EULA")
            staging = INSTANCES_ROOT / ("." + identifier + ".import-" + uuid.uuid4().hex)
            staging.mkdir(parents=True, exist_ok=False)
            try:
                safe_extract(archive, staging)
                flatten_single_root(staging)
                detected = detect(staging)
                port = int(payload.get("port", detected["port"]))
                if not 1 <= port <= 65535:
                    raise ValueError("Порт должен быть от 1 до 65535")
                set_property(staging, "server-port", port)
                (staging / "eula.txt").write_text("eula=true\n", encoding="utf-8")
                set_owner(staging)
                staging.rename(target)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
            finally:
                try:
                    archive.unlink()
                except OSError:
                    pass
            value = profile(identifier, payload.get("name"), target, payload.get("ram_min_mb", 2048), payload.get("ram_max_mb", 8192), port)
            data["instances"].append(value)
            if not data["active"]:
                data["active"] = identifier
            save_store(data)
            return {"ok": True, "instance": value, **public_list(data)}

        if action == "clone":
            source = find(data, payload.get("source_id"))
            if source["id"] == data["active"] and service_state()["active_state"] in {"active", "activating", "deactivating"}:
                raise RuntimeError("Сначала остановите активную сборку, чтобы получить целостную копию")
            identifier, target = ensure_unique(data, payload.get("id"))
            source_dir = safe_directory(source["directory"])
            shutil.copytree(source_dir, target, symlinks=False, ignore=shutil.ignore_patterns("session.lock", "*.lck"))
            set_owner(target)
            value = dict(source)
            value.update({
                "id": identifier,
                "name": str(payload.get("name") or identifier).strip()[:80] or identifier,
                "directory": str(target),
                "created_at": int(time.time()),
                "last_started_at": None,
                "managed_directory": True,
            })
            data["instances"].append(value)
            save_store(data)
            return {"ok": True, "instance": value, **public_list(data)}

        if action == "update":
            value = find(data, payload.get("id"))
            ram_min = int(payload.get("ram_min_mb", value.get("ram_min_mb", 2048)))
            ram_max = int(payload.get("ram_max_mb", value.get("ram_max_mb", 8192)))
            port = int(payload.get("port", value.get("port", 25565)))
            if ram_min < 256 or ram_max < ram_min or ram_max > 131072:
                raise ValueError("Проверьте минимальную и максимальную RAM")
            if not 1 <= port <= 65535:
                raise ValueError("Порт должен быть от 1 до 65535")
            value.update({
                "name": str(payload.get("name") or value["id"]).strip()[:80] or value["id"],
                "ram_min_mb": ram_min,
                "ram_max_mb": ram_max,
                "port": port,
            })
            directory = safe_directory(value["directory"])
            set_property(directory, "server-port", port)
            if payload.get("redetect") is True:
                value.update(detect(directory))
                value["port"] = port
            save_store(data)
            return {"ok": True, "instance": value, **public_list(data)}

        if action in {"start", "restart"}:
            value = find(data, payload.get("id"))
            safe_directory(value["directory"])
            previous = data["active"]
            state = service_state()["active_state"]
            previous_was_running = state in {"active", "activating", "deactivating"}
            switching = previous != value["id"]
            if switching and previous_was_running:
                stop_service()
            data["active"] = value["id"]
            value["last_started_at"] = int(time.time())
            save_store(data)
            command(["/usr/bin/systemctl", "reset-failed", "dragonfyre.service"], timeout=30, check=False)
            verb = "restart" if action == "restart" and not switching and state in {"active", "activating"} else "start"
            result = command(["/usr/bin/systemctl", verb, "dragonfyre.service"], timeout=220, check=False)
            if result.returncode != 0:
                data["active"] = previous
                save_store(data)
                if previous and previous != value["id"] and previous_was_running:
                    command(["/usr/bin/systemctl", "start", "dragonfyre.service"], timeout=220, check=False)
                raise RuntimeError(result.stderr.strip() or "Minecraft не удалось запустить; выбрана предыдущая сборка")
            time.sleep(2)
            started = service_state()
            restart_loop = started["sub_state"] == "auto-restart" and started["restart_count"] > 0
            if started["active_state"] in {"failed", "inactive"} or restart_loop:
                journal = command(
                    ["/usr/bin/journalctl", "-u", "dragonfyre.service", "-n", "12", "--no-pager"],
                    timeout=30,
                    check=False,
                ).stdout.strip()
                if switching:
                    data["active"] = previous
                    save_store(data)
                    command(["/usr/bin/systemctl", "reset-failed", "dragonfyre.service"], timeout=30, check=False)
                    if previous and previous_was_running:
                        command(["/usr/bin/systemctl", "start", "dragonfyre.service"], timeout=220, check=False)
                raise RuntimeError(
                    "Сборка завершилась сразу после запуска."
                    + (" Предыдущая сборка восстановлена.\n" if switching else "\n")
                    + journal[-6000:]
                )
            return {"ok": True, "switched": switching, **public_list(data)}

        if action == "stop":
            value = find(data, payload.get("id"))
            if value["id"] == data["active"]:
                stop_service()
            return {"ok": True, **public_list(data)}

        if action == "delete":
            value = find(data, payload.get("id"))
            if value["id"] == data["active"]:
                raise RuntimeError("Активную сборку нельзя удалить. Сначала переключитесь на другую.")
            deleted_files = False
            if payload.get("delete_files") is True:
                directory = safe_directory(value["directory"])
                try:
                    directory.relative_to(INSTANCES_ROOT.resolve(strict=True))
                except ValueError:
                    raise ValueError("Файлы импортированной вручную сборки автоматически не удаляются")
                shutil.rmtree(directory)
                deleted_files = True
            data["instances"] = [item for item in data["instances"] if item["id"] != value["id"]]
            save_store(data)
            return {"ok": True, "deleted_files": deleted_files, **public_list(data)}

        raise ValueError("Неизвестное действие менеджера сборок")

    def main():
        if os.geteuid() != 0:
            raise PermissionError("Менеджер должен запускаться через sudo")
        try:
            payload = json.loads(sys.argv[1]) if len(sys.argv) == 2 else {"action": "list"}
            if not isinstance(payload, dict):
                raise ValueError("Некорректный запрос")
            LOCK.parent.mkdir(parents=True, exist_ok=True)
            with LOCK.open("a+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                data = read_store()
                ensure_runtime(data)
                result = dispatch(payload, data)
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        except Exception as error:
            print(str(error), file=sys.stderr)
            raise SystemExit(1)

    if __name__ == "__main__":
        main()
    """
).strip().replace("__RUNNER_PROGRAM__", repr(MANAGED_INSTANCE_RUNNER_PROGRAM))


_REMOTE_MANAGER_CODE = "import base64;exec(compile(base64.b64decode(" + repr(
    base64.b64encode(REMOTE_INSTANCE_MANAGER_PROGRAM.encode("utf-8")).decode("ascii")
) + "),'<server-control-instances>','exec'))"


def manager_command(payload: dict[str, Any]) -> str:
    """Return a shell-safe, non-interactive manager command."""

    value = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return "sudo -n /usr/bin/python3 -c " + shlex.quote(_REMOTE_MANAGER_CODE) + " " + shlex.quote(value)
