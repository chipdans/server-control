"""Bounded, read-only Minecraft telemetry embedded in the SSH status program."""

REMOTE_PERFORMANCE_PROGRAM = r'''
import math

def proc_sample(pid, proc_root=Path("/proc")):
    try:
        text = read_text(proc_root / str(int(pid)) / "stat", 8192)
        name, separator, tail = text.rpartition(") ")
        fields = tail.split()
        if not separator or len(fields) < 22:
            return None
        return {
            "pid": int(pid), "name": name.partition("(")[2],
            "ticks": int(fields[11]) + int(fields[12]),
            "start_ticks": int(fields[19]),
            "memory_bytes": max(0, int(fields[21])) * os.sysconf("SC_PAGE_SIZE"),
        }
    except (OSError, ValueError, IndexError):
        return None

def find_java_process(service, proc_root=Path("/proc"), cgroup_root=Path("/sys/fs/cgroup")):
    # tmux can detach from MainPID. Its JVM stays in the systemd service cgroup.
    candidates = set()
    group = service.get("ControlGroup", "")
    if group and group != "/":
        try:
            directory = (cgroup_root / group.lstrip("/")).resolve(strict=True)
            directory.relative_to(cgroup_root.resolve(strict=True))
            for index, (root, directories, _files) in enumerate(os.walk(directory)):
                if index >= 64 or len(candidates) >= 1024:
                    break
                directories.sort()
                candidates.update(int(pid) for pid in read_text(Path(root) / "cgroup.procs", 16384).split() if pid.isdecimal())
        except (OSError, ValueError):
            pass
    # Also support a non-detaching wrapper on hosts without unified cgroups.
    pending = [str(service.get("MainPID") or "0")]
    visited = set()
    while pending and len(visited) < 1024:
        pid = pending.pop()
        if not pid.isdecimal() or pid == "0" or pid in visited:
            continue
        visited.add(pid)
        candidates.add(int(pid))
        pending.extend(read_text(proc_root / pid / "task" / pid / "children", 16384).split())
    java = []
    for pid in sorted(candidates)[:1024]:
        sample = proc_sample(pid, proc_root)
        if sample and sample["name"] == "java":
            java.append(sample)
    # Do not attribute an arbitrary JVM to Minecraft if the service has several.
    return java[0] if len(java) == 1 else None

def java_metrics(sample, previous, clock, cpu_count):
    if not sample:
        return {"pid": None, "cpu_percent": None, "memory_bytes": None}
    percent = None
    if previous.get("pid") == sample["pid"] and previous.get("start_ticks") == sample["start_ticks"]:
        elapsed = clock - previous.get("clock", clock)
        ticks = sample["ticks"] - previous.get("ticks", sample["ticks"])
        if 0.1 <= elapsed <= 120 and ticks >= 0:
            percent = round(min(100.0, 100.0 * ticks / os.sysconf("SC_CLK_TCK") / elapsed / cpu_count), 1)
    return {"pid": sample["pid"], "cpu_percent": percent, "memory_bytes": sample["memory_bytes"], "cpu_count": cpu_count}

def parse_forge_performance(output):
    # Only the overall summary represents the whole server, not one dimension.
    output = re.sub(r"§.", "", output)
    matches = re.findall(
        r"Overall:\s*Mean tick time:\s*([0-9]+(?:[.,][0-9]+)?)\s*ms\.\s*Mean TPS:\s*([0-9]+(?:[.,][0-9]+)?)",
        output, re.IGNORECASE,
    )
    if not matches:
        return None
    mspt, tps = (float(value.replace(",", ".")) for value in matches[-1])
    if not math.isfinite(mspt) or not math.isfinite(tps) or not 0 <= tps <= 20:
        return None
    return {"tps": tps, "mspt": mspt}

def rcon_performance(host, port, password, loader):
    # Fixed diagnostic commands only. No shell, remote endpoint or persisted secret.
    deadline = time.monotonic() + 3.0
    with socket.create_connection((host, port), timeout=2) as stream:
        def remaining():
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("telemetry deadline")
            stream.settimeout(left)

        def exact(count):
            output = bytearray()
            while len(output) < count:
                remaining()
                chunk = stream.recv(count - len(output))
                if not chunk:
                    raise OSError("short telemetry response")
                output.extend(chunk)
            return bytes(output)

        def receive():
            length = struct.unpack("<i", exact(4))[0]
            if not 10 <= length <= 65536:
                raise ValueError("invalid telemetry frame")
            body = exact(length)
            if body[-2:] != b"\0\0":
                raise ValueError("invalid telemetry terminator")
            request_id, packet_type = struct.unpack("<ii", body[:8])
            if request_id == -1:
                raise PermissionError("telemetry authentication")
            return request_id, packet_type, body[8:-2].decode("utf-8", "replace")

        def send(request_id, packet_type, payload):
            encoded = payload.encode("utf-8") + b"\0\0"
            remaining()
            stream.sendall(struct.pack("<iii", len(encoded) + 8, request_id, packet_type) + encoded)

        send(1, 3, password)
        for _ in range(4):
            request_id, packet_type, _payload = receive()
            if request_id == 1 and packet_type == 2:
                break
        else:
            raise PermissionError("telemetry authentication")

        commands = ["neoforge tps", "forge tps"] if "neoforge" in loader.casefold() else ["forge tps", "neoforge tps"]
        for index, command in enumerate(commands):
            command_id = 10 + index * 2
            send(command_id, 2, command)
            send(command_id + 1, 2, "")
            chunks, size = [], 0
            for _ in range(128):
                request_id, packet_type, payload = receive()
                if request_id == command_id + 1 and packet_type == 0:
                    result = parse_forge_performance("".join(chunks))
                    if result:
                        return dict(result, status="ok", source=command)
                    break
                if request_id != command_id or packet_type != 0:
                    raise ValueError("unexpected telemetry response")
                size += len(payload)
                if size > 65536:
                    raise ValueError("oversized telemetry response")
                chunks.append(payload)
            else:
                raise ValueError("too many telemetry frames")
    return {"status": "unsupported"}

def rcon_properties(directory):
    values = {}
    for line in read_text(directory / "server.properties", 1048576).splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() in ("enable-rcon", "rcon.port", "rcon.password"):
            # Minecraft's Properties.store escapes punctuation and non-ASCII.
            value = re.sub(r"\\u([0-9a-fA-F]{4})|\\(.)", lambda match: chr(int(match[1], 16)) if match[1] else {"n": "\n", "r": "\r", "t": "\t", "f": "\f"}.get(match[2], match[2]), value.lstrip())
            values[key.strip()] = value
    return values

def measure_performance(directory, host, local_addresses, loader):
    properties = rcon_properties(directory)
    if properties.get("enable-rcon", "false").casefold() != "true":
        return {"status": "disabled"}
    password = properties.get("rcon.password", "")
    if not password or len(password) > 4096 or "\0" in password:
        return {"status": "unconfigured"}
    if host not in {"127.0.0.1", "::1", *local_addresses}:
        return {"status": "unavailable"}
    try:
        port = int(properties.get("rcon.port", "25575"))
        if not 1 <= port <= 65535:
            return {"status": "unconfigured"}
        return rcon_performance(host, port, password, loader)
    except PermissionError:
        return {"status": "auth_failed"}
    except TimeoutError:
        return {"status": "timeout"}
    except (OSError, ValueError):
        return {"status": "unavailable"}

def collect_minecraft_telemetry(service, profile, state, session_id, host, local_addresses, cache_path):
    import fcntl

    directory = Path(profile["directory"])
    sample = find_java_process(service) if state in ("RUNNING", "STARTING", "STOPPING") else None
    clock = time.monotonic()
    cpu_count = max(1, os.cpu_count() or 1)
    process = java_metrics(sample, {}, clock, cpu_count)
    unavailable = {"status": "starting" if state == "STARTING" else "stopped" if state != "RUNNING" else "unavailable"}
    try:
        cache_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(cache_path.with_suffix(".lock"), "a") as lock:
            # Two desktop clients must not double the query rate or overwrite samples.
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                cache = json.loads(read_text(cache_path))
                if not isinstance(cache, dict):
                    cache = {}
            except ValueError:
                cache = {}
            key = [session_id, str(directory), sample["pid"] if sample else None, sample["start_ticks"] if sample else None]
            if not session_id or cache.get("key") != key:
                cache = {}
            previous = cache.get("process", {})
            process = java_metrics(sample, previous, clock, cpu_count)
            if not previous or clock - previous.get("clock", 0) >= 1:
                cache["process"] = dict(sample, clock=clock) if sample else {}
            performance = cache.get("performance", {})
            if state != "RUNNING":
                performance = unavailable
                cache.pop("probe_clock", None)
            elif "probe_clock" not in cache or clock < cache["probe_clock"] or clock >= cache["probe_clock"] + cache.get("probe_interval", 30):
                performance = measure_performance(directory, host, local_addresses, str(profile.get("loader") or ""))
                if performance.get("status") == "ok":
                    performance["measured_at"] = int(time.time() * 1000)
                cache["probe_clock"] = time.monotonic()
                cache["probe_interval"] = 300 if performance.get("status") == "unsupported" else 30
            if not performance:
                performance = unavailable
            age = max(0, int(time.monotonic() - cache.get("probe_clock", clock)))
            performance["age_seconds"] = age
            cache.update(key=key, performance=performance)
            temporary = cache_path.with_name(cache_path.name + "." + str(os.getpid()))
            temporary.write_text(json.dumps(cache), encoding="utf-8")
            os.replace(temporary, cache_path)
            return process, performance
    except (OSError, ValueError, TypeError):
        # Diagnostics must not interrupt service readiness, SSH or power status.
        return process, unavailable
'''
