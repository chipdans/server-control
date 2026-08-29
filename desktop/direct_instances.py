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

    def atomic_write(path, content, mode=0o640, group=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, mode)
            if group:
                os.chown(temporary, 0, grp.getgrnam(group).gr_gid)
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
        path = directory / "server.properties"
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

    def set_property(directory, key, value):
        path = directory / "server.properties"
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines() if path.is_file() else []
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
        path.write_text("\n".join(output) + "\n", encoding="utf-8")

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

    def dispatch(payload, data):
        action = str(payload.get("action") or "list")
        if action == "list":
            return public_list(data)

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
