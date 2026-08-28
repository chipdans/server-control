const JOB_ACTIVE = new Set(["pending", "claimed", "running"]);
const JOB_TYPES = new Set([
  "instance_create", "instance_update", "instance_delete", "instance_duplicate", "instance_install_vanilla",
  "instance_start", "instance_stop", "instance_restart", "instance_kill", "instance_update_files",
  "minecraft_command", "player_action", "file_list", "file_read", "file_write", "file_search", "file_operation",
  "backup_list", "backup_create", "backup_restore", "backup_delete", "backup_duplicate", "log_read",
  "backup_export",
  "service_action", "server_reboot", "server_shutdown", "agent_update", "transfer_import", "transfer_export",
]);

export const CONTROL_PERMISSIONS = [
  "status.view", "terminal.linux", "terminal.minecraft",
  "server.view", "server.power", "server.reboot", "server.services", "server.processes",
  "minecraft.view", "minecraft.start", "minecraft.stop", "minecraft.restart", "minecraft.kill",
  "minecraft.console", "minecraft.players", "minecraft.instances.manage", "minecraft.settings",
  "minecraft.files.read", "minecraft.files.write", "minecraft.backups", "minecraft.restore", "minecraft.delete",
  "logs.view", "users.manage", "audit.view", "settings.manage", "updates.manage",
];

export const CONTROL_ROLE_PERMISSIONS = {
  owner: [...CONTROL_PERMISSIONS],
  admin: CONTROL_PERMISSIONS.filter((item) => !["server.power", "minecraft.delete", "users.manage"].includes(item)),
  operator: [
    "server.view", "minecraft.view", "minecraft.start", "minecraft.stop", "minecraft.restart",
    "minecraft.console", "minecraft.players", "logs.view",
  ],
  file_manager: ["server.view", "minecraft.view", "minecraft.files.read", "minecraft.files.write", "logs.view"],
  viewer: ["server.view", "minecraft.view", "minecraft.files.read", "logs.view"],
};

const TYPE_PERMISSIONS = {
  instance_create: "minecraft.instances.manage",
  instance_update: "minecraft.settings",
  instance_delete: "minecraft.delete",
  instance_duplicate: "minecraft.instances.manage",
  instance_install_vanilla: "minecraft.instances.manage",
  instance_start: "minecraft.start",
  instance_stop: "minecraft.stop",
  instance_restart: "minecraft.restart",
  instance_kill: "minecraft.kill",
  instance_update_files: "minecraft.instances.manage",
  minecraft_command: "minecraft.console",
  player_action: "minecraft.players",
  file_list: "minecraft.files.read",
  file_read: "minecraft.files.read",
  file_search: "minecraft.files.read",
  file_write: "minecraft.files.write",
  file_operation: "minecraft.files.write",
  backup_list: "minecraft.backups",
  backup_create: "minecraft.backups",
  backup_restore: "minecraft.restore",
  backup_delete: "minecraft.backups",
  backup_duplicate: "minecraft.restore",
  backup_export: "minecraft.backups",
  log_read: "logs.view",
  service_action: "server.services",
  server_reboot: "server.reboot",
  server_shutdown: "server.power",
  agent_update: "updates.manage",
  transfer_import: "minecraft.files.write",
  transfer_export: "minecraft.files.read",
};

const INSTANCE_ID_RE = /^[a-z0-9][a-z0-9_-]{0,47}$/;
const MAX_JOB_PAYLOAD_BYTES = 256 * 1024;
const TRANSFER_TTL_MS = 24 * 60 * 60 * 1000;
const MAX_TRANSFER_SIZE = 50 * 1024 * 1024 * 1024;
export const AGENT_ONLINE_MAX_AGE_MS = 45 * 1000;

const SYNC_EVENT_LIMIT = 250;
const SYNC_JOB_LIMIT = 100;
const SYNC_NOTIFICATION_LIMIT = 100;
const SYNC_JOB_JSON_LIMIT = 32 * 1024;

const SENSITIVE_JOB_KEYS = /(?:password|token|secret|authorization|content|command|output|lines|save_output|private_key|api_key)/i;

export function redactJobValue(value, key = "", depth = 0) {
  if (SENSITIVE_JOB_KEYS.test(key)) return "[скрыто]";
  if (depth >= 4) return "[сокращено]";
  if (Array.isArray(value)) return value.slice(0, 30).map((item) => redactJobValue(item, "", depth + 1));
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).slice(0, 80).map(([name, item]) => [name, redactJobValue(item, name, depth + 1)]),
    );
  }
  if (typeof value === "string" && value.length > 1000) return `${value.slice(0, 1000)}…`;
  return value;
}

export function boundedJobResult(value) {
  const serialized = JSON.stringify(value);
  if (new TextEncoder().encode(serialized).byteLength <= 240 * 1024) return serialized;
  return JSON.stringify({ truncated: true, message: "Результат операции превышает лимит хранения; используйте журнал или скачивание." });
}

function jobJson(row, parseJson, includeSensitive = false) {
  if (!row) return null;
  const payload = parseJson(row.payload, {});
  const result = parseJson(row.result, null);
  return {
    id: String(row.id),
    type: String(row.type),
    payload: includeSensitive ? payload : redactJobValue(payload),
    requested_by: row.requested_by || null,
    instance_id: row.instance_id || null,
    status: String(row.status),
    progress: Number(row.progress || 0),
    stage: row.stage || "",
    message: row.message || "",
    result: includeSensitive ? result : redactJobValue(result),
    error_code: row.error_code || null,
    lock_key: row.lock_key || null,
    cancel_requested: Boolean(row.cancel_requested),
    created_at: Number(row.created_at),
    started_at: row.started_at ? Number(row.started_at) : null,
    completed_at: row.completed_at ? Number(row.completed_at) : null,
    updated_at: Number(row.updated_at),
  };
}

function normalizeInstanceId(value, ApiError, required = true) {
  if ((value === null || value === undefined || value === "") && !required) return null;
  const id = typeof value === "string" ? value.trim().toLowerCase() : "";
  if (!INSTANCE_ID_RE.test(id)) throw new ApiError(400, "invalid_instance_id", "Некорректный идентификатор сборки.");
  return id;
}

function decodeOpaqueId(value, ApiError) {
  let decoded;
  try { decoded = decodeURIComponent(value); } catch { throw new ApiError(400, "invalid_id", "Некорректный идентификатор."); }
  if (!/^[A-Za-z0-9_.:-]{1,128}$/.test(decoded)) throw new ApiError(400, "invalid_id", "Некорректный идентификатор.");
  return decoded;
}

export function normalizeRelativePath(value, ApiError, allowEmpty = true) {
  if (typeof value !== "string") throw new ApiError(400, "invalid_path", "Путь должен быть строкой.");
  const raw = value.replaceAll("\\", "/").trim();
  if (!raw && allowEmpty) return "";
  if (!raw || raw.startsWith("/") || /^[A-Za-z]:/.test(raw) || raw.includes("\0")) {
    throw new ApiError(400, "invalid_path", "Разрешён только относительный путь внутри сборки.");
  }
  const parts = raw.split("/").filter((item) => item && item !== ".");
  if (parts.some((item) => item === "..")) throw new ApiError(400, "invalid_path", "Выход за пределы сборки запрещён.");
  if (parts.length > 64 || parts.some((item) => item.length > 255)) {
    throw new ApiError(400, "invalid_path", "Путь слишком длинный.");
  }
  return parts.join("/");
}

export function normalizeFilename(value, ApiError, required = true) {
  const name = typeof value === "string" ? value.trim() : "";
  if (!name && !required) return "";
  if (!name || name === "." || name === ".." || name.length > 255 || /[\/\\\0]/.test(name)) {
    throw new ApiError(400, "invalid_filename", "Некорректное имя файла.");
  }
  return name;
}

export function normalizeJobPayload(type, body, ApiError) {
  if (!JOB_TYPES.has(type)) throw new ApiError(400, "invalid_job_type", "Недопустимый тип операции.");
  const payload = body && typeof body === "object" && !Array.isArray(body) ? { ...body } : {};
  const instanceTypes = new Set([...JOB_TYPES].filter((item) => item.startsWith("instance_") || item.startsWith("file_") || item.startsWith("backup_") || ["minecraft_command", "player_action", "log_read", "transfer_import", "transfer_export"].includes(item)));
  if (instanceTypes.has(type)) payload.instance_id = normalizeInstanceId(payload.instance_id, ApiError);
  if (["file_list", "file_read", "file_write", "file_search", "file_operation", "transfer_import", "transfer_export"].includes(type)) {
    payload.path = normalizeRelativePath(payload.path || "", ApiError, true);
  }
  if (["instance_duplicate", "backup_duplicate"].includes(type)) {
    payload.new_instance_id = normalizeInstanceId(payload.new_instance_id, ApiError);
  }
  if (type === "instance_create") {
    const mode = ["empty", "upload", "import"].includes(payload.mode) ? payload.mode : "empty";
    payload.mode = mode;
    if (mode === "import") payload.existing_path = normalizeRelativePath(payload.existing_path || "", ApiError, false);
    if (mode === "upload" && !/^[A-Za-z0-9_.:-]{1,128}$/.test(String(payload.transfer_id || ""))) {
      throw new ApiError(400, "invalid_transfer", "Для импорта ZIP нужна корректная передача.");
    }
  }
  if (type === "instance_install_vanilla") {
    if (payload.accept_eula !== true) throw new ApiError(400, "eula_required", "Для Vanilla необходимо явно принять Minecraft EULA.");
    if (!/^(?:latest|\d+\.\d+(?:\.\d+)?)$/.test(String(payload.minecraft_version || "latest"))) {
      throw new ApiError(400, "invalid_version", "Некорректная версия Minecraft.");
    }
  }
  if (type === "instance_update_files") {
    if (!/^[A-Za-z0-9_.:-]{1,128}$/.test(String(payload.transfer_id || "")) || !/^[a-f0-9]{64}$/i.test(String(payload.transfer_sha256 || ""))) {
      throw new ApiError(400, "invalid_update", "Для обновления нужен ZIP с SHA-256.");
    }
    payload.transfer_sha256 = String(payload.transfer_sha256).toLowerCase();
  }
  if (type === "file_operation") {
    const actions = new Set(["create_folder", "create_file", "delete", "rename", "copy", "move", "duplicate", "archive", "extract_zip"]);
    if (!actions.has(payload.action)) throw new ApiError(400, "invalid_action", "Недопустимая файловая операция.");
    if (["create_folder", "create_file", "rename", "duplicate"].includes(payload.action)) {
      payload.name = normalizeFilename(payload.name, ApiError, payload.action !== "duplicate");
    }
    if (["copy", "move", "archive", "extract_zip"].includes(payload.action)) {
      payload.destination = normalizeRelativePath(payload.destination || "", ApiError, payload.action !== "archive");
    }
  }
  if (["backup_restore", "backup_delete", "backup_duplicate", "backup_export"].includes(type)) {
    const backupId = String(payload.backup_id || "").trim();
    if (!/^[A-Za-z0-9_.-]{1,120}$/.test(backupId)) throw new ApiError(400, "invalid_backup", "Некорректный идентификатор backup.");
    payload.backup_id = backupId;
  }
  if (["transfer_import", "transfer_export", "backup_export"].includes(type)) {
    const transferId = String(payload.transfer_id || "");
    if (!/^[A-Za-z0-9_.:-]{1,128}$/.test(transferId)) throw new ApiError(400, "invalid_transfer", "Некорректный идентификатор передачи.");
    payload.transfer_id = transferId;
  }
  if (type === "service_action") {
    if (!/^[A-Za-z0-9@_.:-]{1,128}\.service$/.test(String(payload.service || ""))) {
      throw new ApiError(400, "invalid_service", "Некорректное имя systemd-службы.");
    }
    if (!["start", "stop", "restart", "status"].includes(payload.action)) throw new ApiError(400, "invalid_action", "Недопустимое действие службы.");
  }
  if (type === "file_write") {
    if (typeof payload.content !== "string" || new TextEncoder().encode(payload.content).length > 192 * 1024) {
      throw new ApiError(413, "file_too_large", "В редакторе можно сохранять текстовые файлы до 192 КиБ. Для больших файлов используйте загрузку.");
    }
  }
  if (type === "minecraft_command") {
    const command = typeof payload.command === "string" ? payload.command.trim().replace(/^\/+/, "") : "";
    if (!command || command.length > 512 || /[\r\n\0]/.test(command)) throw new ApiError(400, "invalid_command", "Некорректная команда Minecraft.");
    payload.command = command;
  }
  const encoded = new TextEncoder().encode(JSON.stringify(payload));
  if (encoded.length > MAX_JOB_PAYLOAD_BYTES) throw new ApiError(413, "payload_too_large", "Параметры операции слишком велики.");
  return payload;
}

function lockKeyFor(type, payload) {
  const instanceId = payload.instance_id || "primary";
  if ([
    "instance_create", "instance_install_vanilla", "instance_start", "instance_stop", "instance_restart",
    "instance_kill", "instance_update", "instance_delete", "instance_duplicate", "instance_update_files",
    "file_write", "file_operation", "backup_create", "backup_restore", "backup_delete", "backup_duplicate",
    "backup_export", "transfer_import", "transfer_export",
  ].includes(type)) {
    return `instance:${instanceId}:exclusive`;
  }
  if (["server_reboot", "server_shutdown"].includes(type)) return "server:power";
  if (type === "agent_update") return "agent:update";
  if (type === "service_action" && payload.action !== "status") return `service:${payload.service}`;
  if (["transfer_import", "transfer_export", "backup_export"].includes(type)) return `transfer:${payload.transfer_id || instanceId}`;
  return null;
}

function sameLockedRequest(row, type, payload, safeJson) {
  if (!row || String(row.type) !== type) return false;
  return JSON.stringify(safeJson(row.payload, {})) === JSON.stringify(payload);
}

async function enqueueJob(env, type, body, session, h) {
  const payload = normalizeJobPayload(type, body, h.ApiError);
  h.requirePermission(session, TYPE_PERMISSIONS[type]);
  const now = Date.now();
  const safePowerOff = await env.DB.prepare(
    "SELECT id FROM command_queue WHERE type='safe_power_off' AND status IN ('pending','claimed') LIMIT 1",
  ).first();
  if (safePowerOff) throw new h.ApiError(409, "power_off_pending", "Сервер уже готовится к безопасному отключению питания.");
  if (type === "agent_update") {
    const activeJob = await env.DB.prepare(
      "SELECT id,type FROM jobs WHERE status IN ('pending','claimed','running') ORDER BY created_at LIMIT 1",
    ).first();
    if (activeJob) throw new h.ApiError(409, "operation_locked", `Перед обновлением Agent завершите операцию ${activeJob.type}.`);
  } else {
    const agentUpdate = await env.DB.prepare(
      "SELECT id FROM jobs WHERE lock_key='agent:update' AND status IN ('pending','claimed','running') LIMIT 1",
    ).first();
    if (agentUpdate) throw new h.ApiError(409, "operation_locked", "Agent сейчас обновляется; новые операции временно остановлены.");
  }
  if (["server_reboot", "server_shutdown"].includes(type)) {
    const activeInstanceJob = await env.DB.prepare(
      "SELECT id,type FROM jobs WHERE lock_key LIKE 'instance:%:exclusive' AND status IN ('pending','claimed','running') ORDER BY created_at LIMIT 1",
    ).first();
    if (activeInstanceJob) throw new h.ApiError(409, "operation_locked", `Сначала завершите операцию ${activeInstanceJob.type}.`);
  } else if (payload.instance_id) {
    const activePowerJob = await env.DB.prepare(
      "SELECT id,type FROM jobs WHERE lock_key='server:power' AND status IN ('pending','claimed','running') ORDER BY created_at LIMIT 1",
    ).first();
    if (activePowerJob) throw new h.ApiError(409, "operation_locked", "Linux-сервер уже готовится к перезагрузке или выключению.");
  }
  if (["server_reboot", "server_shutdown", "instance_kill", "instance_delete", "backup_restore"].includes(type)) {
    const recent = await env.DB.prepare(
      "SELECT id FROM jobs WHERE requested_by=? AND type=? AND created_at>? ORDER BY created_at DESC LIMIT 1",
    ).bind(session.user.id, type, now - 5_000).first();
    if (recent) throw new h.ApiError(429, "action_rate_limited", "Опасное действие уже запрашивалось несколько секунд назад.");
  }
  const lockKey = lockKeyFor(type, payload);
  const job = {
    id: crypto.randomUUID(), type, payload, requested_by: session.user.id,
    instance_id: payload.instance_id || null, status: "pending", progress: 0,
    stage: "queued", message: "Операция поставлена в очередь.", lock_key: lockKey,
    created_at: now, updated_at: now,
  };
  if (lockKey) {
    const active = await env.DB.prepare(
      "SELECT * FROM jobs WHERE lock_key = ? AND status IN ('pending','claimed','running') ORDER BY created_at DESC LIMIT 1",
    ).bind(lockKey).first();
    if (active) {
      if (sameLockedRequest(active, type, payload, h.safeJson)) return { job: jobJson(active, h.safeJson), reused: true };
      throw new h.ApiError(409, "operation_locked", `Сборка занята операцией ${active.type}. Дождитесь её завершения или отмените задачу.`);
    }
  }
  try {
    await env.DB.prepare(
      `INSERT INTO jobs (id,type,payload,requested_by,instance_id,status,progress,stage,message,lock_key,created_at,updated_at)
       VALUES (?,?,?,?,?,'pending',0,'queued',?,?,?,?)`,
    ).bind(job.id, type, JSON.stringify(payload), job.requested_by, job.instance_id, job.message, lockKey, now, now).run();
  } catch (error) {
    if (lockKey && String(error?.message || error).includes("UNIQUE")) {
      const active = await env.DB.prepare(
        "SELECT * FROM jobs WHERE lock_key = ? AND status IN ('pending','claimed','running') ORDER BY created_at DESC LIMIT 1",
      ).bind(lockKey).first();
      if (active && sameLockedRequest(active, type, payload, h.safeJson)) return { job: jobJson(active, h.safeJson), reused: true };
      if (active) throw new h.ApiError(409, "operation_locked", `Сборка занята операцией ${active.type}. Дождитесь её завершения или отмените задачу.`);
    }
    throw error;
  }
  await h.addAudit(env, session.user.id, `job.${type}.create`, { job_id: job.id, instance_id: job.instance_id }, { target: job.instance_id || type, request: h.request });
  return { job, reused: false };
}

function inferLevel(message) {
  const lower = String(message || "").toLowerCase();
  if (/\b(error|fatal|exception|failed|ошибка)\b/.test(lower)) return "ERROR";
  if (/\b(warn|warning|предупреж)\b/.test(lower)) return "WARN";
  if (/\b(debug|trace)\b/.test(lower)) return "DEBUG";
  return "INFO";
}

async function createNotification(env, type, severity, title, message, target = null, userId = null) {
  await env.DB.prepare(
    "INSERT INTO notifications (user_id,type,severity,title,message,target,created_at) VALUES (?,?,?,?,?,?,?)",
  ).bind(userId || null, type, severity, String(title).slice(0, 160), String(message).slice(0, 1000), target, Date.now()).run();
}

function sessionPermissionSet(session) {
  return new Set(Array.isArray(session?.user?.permissions) ? session.user.permissions : []);
}

export function filterStatusForSession(status, session) {
  const source = status && typeof status === "object" && !Array.isArray(status) ? status : {};
  const permissions = sessionPermissionSet(session);
  const has = (...names) => names.some((name) => permissions.has(name));
  const simpleStatusAccess = has("status.view");
  const minecraftAccess = simpleStatusAccess || has("minecraft_view", "minecraft_command")
    || [...permissions].some((name) => name.startsWith("minecraft."));
  const serverAccess = simpleStatusAccess || has("server_view", "server_command", "power_view", "power_control")
    || [...permissions].some((name) => name.startsWith("server."));
  const result = {
    protocol_version: source.protocol_version,
    agent_version: source.agent_version,
    health: source.health && typeof source.health === "object" ? source.health : {},
  };
  if (serverAccess) {
    result.server = source.server || {};
    result.storage = source.storage || {};
    result.system = source.system || {};
    result.processes = Array.isArray(source.processes) ? source.processes : [];
    result.services = Array.isArray(source.services) ? source.services : [];
  }
  if (minecraftAccess) {
    result.minecraft = source.minecraft || {};
    result.instances = Array.isArray(source.instances) ? source.instances : [];
    result.selected_instance_id = source.selected_instance_id || null;
    result.java = Array.isArray(source.java) ? source.java : [];
  }
  if (has("minecraft.backups", "minecraft.restore")) {
    result.backups = Array.isArray(source.backups) ? source.backups : [];
  }
  return result;
}

export function filterEventsForSession(events, session) {
  const permissions = sessionPermissionSet(session);
  const allLogs = permissions.has("logs.view");
  const serverLogs = allLogs || permissions.has("server.view") || permissions.has("server_view");
  const minecraftLogs = allLogs || permissions.has("minecraft.view") || permissions.has("minecraft_view")
    || permissions.has("minecraft.console") || permissions.has("minecraft_command");
  const auditLogs = permissions.has("audit.view");
  return (Array.isArray(events) ? events : []).filter((event) => (
    event?.kind === "minecraft" ? minecraftLogs : event?.kind === "audit" ? auditLogs : serverLogs
  ));
}

export async function routeSync(request, env, url, session, h) {
  const startedAt = Date.now();
  const after = Math.max(0, Number.parseInt(url.searchParams.get("after") || "0", 10) || 0);
  const jobsSince = Math.max(0, Number.parseInt(url.searchParams.get("jobs_since") || "0", 10) || 0);
  const notificationAfter = Math.max(0, Number.parseInt(url.searchParams.get("notification_after") || "0", 10) || 0);
  const statusAfter = Math.max(0, Number.parseInt(url.searchParams.get("status_after") || "0", 10) || 0);
  const statusStatement = env.DB.prepare("SELECT status, updated_at FROM agent_status WHERE id = 'primary'");
  const powerStatement = env.DB.prepare("SELECT name,on_state,online_state,updated_at FROM power_status WHERE id='primary'");
  const logStatement = after === 0
    ? env.DB.prepare(
      `SELECT * FROM (SELECT id,kind,message,created_at,instance_id,source,level FROM console_events ORDER BY id DESC LIMIT ?) ORDER BY id ASC`,
    ).bind(SYNC_EVENT_LIMIT)
    : env.DB.prepare(
      `SELECT id,kind,message,created_at,instance_id,source,level FROM console_events WHERE id > ? ORDER BY id ASC LIMIT ?`,
    ).bind(after, SYNC_EVENT_LIMIT);
  // The sync feed only needs safe summaries.  Full payload/result JSON stays
  // available from /v1/jobs/:id to the requesting user.  Capping it here
  // prevents one old file-read job from turning every UI refresh into a
  // multi-megabyte D1 response.
  const jobColumns = `id,type,
    CASE WHEN length(payload)<=${SYNC_JOB_JSON_LIMIT} THEN payload ELSE '{}' END AS payload,
    requested_by,instance_id,status,progress,stage,message,
    CASE WHEN result IS NULL OR length(result)<=${SYNC_JOB_JSON_LIMIT} THEN result
         ELSE '{"truncated":true,"message":"Полный результат доступен в задаче."}' END AS result,
    error_code,lock_key,cancel_requested,created_at,started_at,completed_at,updated_at`;
  const jobStatement = jobsSince === 0
    ? env.DB.prepare(`SELECT ${jobColumns} FROM jobs WHERE requested_by=? ORDER BY updated_at DESC LIMIT ?`).bind(session.user.id, SYNC_JOB_LIMIT)
    : env.DB.prepare(`SELECT ${jobColumns} FROM jobs WHERE requested_by=? AND updated_at>? ORDER BY updated_at ASC LIMIT ?`).bind(session.user.id, jobsSince, SYNC_JOB_LIMIT);
  const notificationStatement = notificationAfter === 0
    ? env.DB.prepare(
      `SELECT * FROM (SELECT n.*,CASE WHEN r.user_id IS NULL THEN 0 ELSE 1 END AS is_read
       FROM notifications n LEFT JOIN notification_reads r ON r.notification_id=n.id AND r.user_id=?
       WHERE n.user_id IS NULL OR n.user_id=?
       ORDER BY n.id DESC LIMIT ?) ORDER BY id ASC`,
    ).bind(session.user.id, session.user.id, SYNC_NOTIFICATION_LIMIT)
    : env.DB.prepare(
      `SELECT n.*,CASE WHEN r.user_id IS NULL THEN 0 ELSE 1 END AS is_read
       FROM notifications n LEFT JOIN notification_reads r ON r.notification_id=n.id AND r.user_id=?
       WHERE n.id>? AND (n.user_id IS NULL OR n.user_id=?) ORDER BY n.id ASC LIMIT ?`,
    ).bind(session.user.id, notificationAfter, session.user.id, SYNC_NOTIFICATION_LIMIT);

  // D1 batch turns five network round trips into one.  This is especially
  // important after an idle period when the desktop asks for initial history.
  const [statusResult, powerResult, logResult, jobResult, notificationResult] = await env.DB.batch([
    statusStatement,
    powerStatement,
    logStatement,
    jobStatement,
    notificationStatement,
  ]);
  const statusRow = statusResult?.results?.[0] || null;
  const powerRow = powerResult?.results?.[0] || null;
  const updatedAt = statusRow ? Number(statusRow.updated_at) : 0;
  const ageMs = updatedAt ? Math.max(0, Date.now() - updatedAt) : null;
  const allEvents = logResult.results || [];
  const events = filterEventsForSession(allEvents, session);
  const jobs = (jobResult.results || []).map((row) => jobJson(row, h.safeJson)).sort((a, b) => a.updated_at - b.updated_at);
  const notifications = notificationResult.results || [];
  const syncPermissions = sessionPermissionSet(session);
  const canViewPower = ["power_view", "power_control", "server.view", "server.power"].some((name) => syncPermissions.has(name));
  return h.json({
    protocol: { api: 2, minimum_client: "1.0.0", service: "server-control-hub" },
    server: {
      online: Boolean(statusRow && ageMs < AGENT_ONLINE_MAX_AGE_MS),
      status: statusRow && updatedAt > statusAfter ? filterStatusForSession(h.safeJson(statusRow.status, {}), session) : undefined,
      status_changed: Boolean(statusRow && updatedAt > statusAfter),
      updated_at: updatedAt || null, age_ms: ageMs,
    },
    power: powerRow && canViewPower ? {
      name: powerRow.name || "Питание сервера",
      on: powerRow.on_state === null || powerRow.on_state === undefined ? null : Boolean(powerRow.on_state),
      online: powerRow.online_state === null || powerRow.online_state === undefined ? null : Boolean(powerRow.online_state),
      updated_at: Number(powerRow.updated_at), stale: Date.now() - Number(powerRow.updated_at) > 15_000,
    } : null,
    events,
    next_after: allEvents.length ? Number(allEvents[allEvents.length - 1].id) : after,
    jobs,
    jobs_cursor: jobs.length ? Math.max(...jobs.map((item) => item.updated_at)) : jobsSince,
    notifications,
    notification_cursor: notifications.length ? Number(notifications[notifications.length - 1].id) : notificationAfter,
    server_time: Date.now(),
    sync_ms: Math.max(0, Date.now() - startedAt),
  });
}

async function routeInstances(request, env, pathname, session, h) {
  if (request.method === "GET" && pathname === "/v1/instances") {
    h.requireAnyPermission(session, ["minecraft.view", "minecraft_view"]);
    const row = await env.DB.prepare("SELECT status,updated_at FROM agent_status WHERE id='primary'").first();
    const status = row ? h.safeJson(row.status, {}) : {};
    const instances = Array.isArray(status.instances) ? status.instances : status.minecraft ? [status.minecraft] : [];
    return h.json({ instances, selected_instance_id: status.selected_instance_id || instances[0]?.id || null, updated_at: row ? Number(row.updated_at) : null });
  }
  if (request.method === "POST" && pathname === "/v1/instances") {
    const body = await h.readJson(request, 256 * 1024);
    const mode = ["empty", "vanilla", "upload", "import", "duplicate"].includes(body.mode) ? body.mode : "empty";
    const type = mode === "vanilla" ? "instance_install_vanilla" : mode === "duplicate" ? "instance_duplicate" : "instance_create";
    const result = await enqueueJob(env, type, { ...body, instance_id: body.instance_id }, session, { ...h, request });
    return h.json(result, result.reused ? 200 : 202);
  }
  const match = pathname.match(/^\/v1\/instances\/([^/]+)(?:\/(action|command))?$/);
  if (!match) return null;
  const instanceId = normalizeInstanceId(decodeOpaqueId(match[1], h.ApiError), h.ApiError);
  if (request.method === "PATCH" && !match[2]) {
    const body = await h.readJson(request, 256 * 1024);
    const result = await enqueueJob(env, "instance_update", { ...body, instance_id: instanceId }, session, { ...h, request });
    return h.json(result, result.reused ? 200 : 202);
  }
  if (request.method === "DELETE" && !match[2]) {
    const body = await h.readJson(request, 32 * 1024);
    if (body.confirm !== instanceId) throw new h.ApiError(400, "confirmation_required", "Для удаления введите идентификатор сборки.");
    const result = await enqueueJob(env, "instance_delete", { instance_id: instanceId, delete_files: body.delete_files === true }, session, { ...h, request });
    return h.json(result, result.reused ? 200 : 202);
  }
  if (request.method === "POST" && match[2] === "action") {
    const body = await h.readJson(request);
    const action = String(body.action || "");
    if (!["start", "stop", "restart", "kill", "duplicate", "update_files"].includes(action)) throw new h.ApiError(400, "invalid_action", "Недопустимое действие со сборкой.");
    const type = action === "duplicate" ? "instance_duplicate" : action === "update_files" ? "instance_update_files" : `instance_${action}`;
    const result = await enqueueJob(env, type, { ...body, instance_id: instanceId }, session, { ...h, request });
    return h.json(result, result.reused ? 200 : 202);
  }
  if (request.method === "POST" && match[2] === "command") {
    const body = await h.readJson(request);
    const result = await enqueueJob(env, "minecraft_command", { command: body.command, instance_id: instanceId }, session, { ...h, request });
    return h.json(result, 202);
  }
  return null;
}

async function routeJobs(request, env, pathname, url, session, h) {
  if (request.method === "GET" && pathname === "/v1/jobs") {
    const limit = Math.max(1, Math.min(200, Number.parseInt(url.searchParams.get("limit") || "100", 10) || 100));
    const allUsers = url.searchParams.get("all") === "1" && session.user.permissions.includes("audit.view");
    const query = allUsers
      ? env.DB.prepare("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?").bind(limit)
      : env.DB.prepare("SELECT * FROM jobs WHERE requested_by = ? ORDER BY created_at DESC LIMIT ?").bind(session.user.id, limit);
    const result = await query.all();
    return h.json({ jobs: (result.results || []).map((row) => jobJson(row, h.safeJson)) });
  }
  const match = pathname.match(/^\/v1\/jobs\/([^/]+)(?:\/(cancel))?$/);
  if (!match) return null;
  const id = decodeOpaqueId(match[1], h.ApiError);
  const row = await env.DB.prepare("SELECT * FROM jobs WHERE id = ?").bind(id).first();
  if (!row) throw new h.ApiError(404, "job_not_found", "Операция не найдена.");
  const canSeeAll = session.user.permissions.includes("audit.view");
  if (!canSeeAll && row.requested_by !== session.user.id) throw new h.ApiError(403, "permission_denied", "Нет доступа к этой операции.");
  const includeSensitive = row.requested_by === session.user.id;
  if (request.method === "GET" && !match[2]) return h.json({ job: jobJson(row, h.safeJson, includeSensitive) });
  if (request.method === "POST" && match[2] === "cancel") {
    if (!JOB_ACTIVE.has(String(row.status))) return h.json({ job: jobJson(row, h.safeJson, includeSensitive), already_finished: true });
    const now = Date.now();
    await env.DB.prepare(
      `UPDATE jobs SET cancel_requested=1,
       status=CASE WHEN status='pending' THEN 'cancelled' ELSE status END,
       stage=CASE WHEN status='pending' THEN 'cancelled' ELSE stage END,
       message=CASE WHEN status='pending' THEN 'Операция отменена до запуска.' ELSE 'Запрошена отмена…' END,
       completed_at=CASE WHEN status='pending' THEN ? ELSE completed_at END,
       updated_at=? WHERE id=?`,
    ).bind(now, now, id).run();
    await h.addAudit(env, session.user.id, "job.cancel", { job_id: id, type: row.type }, { target: row.instance_id || row.type, request });
    const updated = await env.DB.prepare("SELECT * FROM jobs WHERE id=?").bind(id).first();
    return h.json({ job: jobJson(updated, h.safeJson, includeSensitive) }, 202);
  }
  return null;
}

async function routeFiles(request, env, pathname, session, h) {
  if (pathname === "/v1/logs/read" && request.method === "POST") {
    const body = await h.readJson(request, 64 * 1024);
    const result = await enqueueJob(env, "log_read", body, session, { ...h, request });
    return h.json(result, result.reused ? 200 : 202);
  }
  const map = {
    "/v1/files/list": "file_list", "/v1/files/read": "file_read", "/v1/files/write": "file_write",
    "/v1/files/search": "file_search", "/v1/files/operation": "file_operation",
  };
  const type = map[pathname];
  if (!type || request.method !== "POST") return null;
  const body = await h.readJson(request, type === "file_write" ? 256 * 1024 : 64 * 1024);
  const result = await enqueueJob(env, type, body, session, { ...h, request });
  return h.json(result, result.reused ? 200 : 202);
}

async function routeBackupsAndAdmin(request, env, pathname, url, session, h) {
  if (request.method === "POST" && pathname === "/v1/server/action") {
    const body = await h.readJson(request);
    const action = String(body.action || "");
    if (!["status", "backup", "reboot", "shutdown"].includes(action)) throw new h.ApiError(400, "invalid_action", "Недопустимое действие Linux-сервера.");
    if (["status", "backup"].includes(action)) {
      h.requireAnyPermission(session, ["server_command", "server.services"]);
      if (typeof h.enqueueCommand !== "function") throw new h.ApiError(503, "legacy_queue_unavailable", "Очередь совместимости недоступна.");
      const command = await h.enqueueCommand(env, `server_${action}`, {}, session.user.id);
      await h.addAudit(env, session.user.id, `server.${action}`, { command_id: command.id }, { target: "server", request });
      return h.json({ ok: true, command }, 202);
    }
    const statusRow = await env.DB.prepare("SELECT status FROM agent_status WHERE id='primary'").first();
    const agentStatus = statusRow ? h.safeJson(statusRow.status, {}) : {};
    if (Number(agentStatus.protocol_version || 1) < 2 && typeof h.enqueueCommand === "function") {
      h.requireAnyPermission(session, ["server_command", action === "reboot" ? "server.reboot" : "server.power"]);
      const command = await h.enqueueCommand(env, `server_${action}`, {}, session.user.id);
      await h.addAudit(env, session.user.id, `server.${action}`, { command_id: command.id, compatibility: true }, { target: "server", request });
      return h.json({ ok: true, command, compatibility: true }, 202);
    }
    const result = await enqueueJob(env, action === "reboot" ? "server_reboot" : "server_shutdown", {}, session, { ...h, request });
    return h.json(result, result.reused ? 200 : 202);
  }
  if (request.method === "GET" && pathname === "/v1/backups") {
    h.requirePermission(session, "minecraft.backups");
    const row = await env.DB.prepare("SELECT status FROM agent_status WHERE id='primary'").first();
    const status = row ? h.safeJson(row.status, {}) : {};
    return h.json({ backups: Array.isArray(status.backups) ? status.backups : [] });
  }
  if (request.method === "POST" && pathname === "/v1/backups/action") {
    const body = await h.readJson(request);
    const action = String(body.action || "");
    if (!["list", "create", "restore", "delete", "duplicate"].includes(action)) throw new h.ApiError(400, "invalid_action", "Недопустимое действие с резервной копией.");
    const result = await enqueueJob(env, `backup_${action}`, body, session, { ...h, request });
    return h.json(result, result.reused ? 200 : 202);
  }
  if (request.method === "POST" && pathname === "/v1/backups/download") {
    h.requirePermission(session, "minecraft.backups");
    const body = await h.readJson(request);
    const instanceId = normalizeInstanceId(body.instance_id, h.ApiError);
    const backupId = String(body.backup_id || "").trim();
    if (!/^[A-Za-z0-9_.-]{1,120}$/.test(backupId)) throw new h.ApiError(400, "invalid_backup", "Некорректный идентификатор backup.");
    const now = Date.now();
    const id = crypto.randomUUID();
    const fileName = `${backupId}.zip`;
    const objectKey = `transfers/${id}/${encodeURIComponent(fileName)}`;
    await env.DB.prepare(
      `INSERT INTO transfers(id,direction,requested_by,instance_id,relative_path,file_name,object_key,size_bytes,sha256,status,created_at,updated_at,expires_at)
       VALUES(?,'download',?,?,?,?,?,0,NULL,'created',?,?,?)`,
    ).bind(id, session.user.id, instanceId, `@backup/${backupId}`, fileName, objectKey, now, now, now + TRANSFER_TTL_MS).run();
    const transfer = transferJson({ id, direction: "download", requested_by: session.user.id, instance_id: instanceId, relative_path: `@backup/${backupId}`, file_name: fileName, object_key: objectKey, size_bytes: 0, sha256: null, status: "created", created_at: now, updated_at: now, expires_at: now + TRANSFER_TTL_MS });
    const jobResult = await enqueueJob(env, "backup_export", { instance_id: instanceId, backup_id: backupId, transfer_id: id }, session, { ...h, request });
    return h.json({ transfer, ...jobResult }, 202);
  }
  if (request.method === "POST" && pathname === "/v1/players/action") {
    const body = await h.readJson(request);
    const action = String(body.action || "");
    if (!["kick", "ban", "pardon", "whitelist_add", "whitelist_remove", "op", "deop", "teleport"].includes(action)) {
      throw new h.ApiError(400, "invalid_action", "Недопустимое действие с игроком.");
    }
    const result = await enqueueJob(env, "player_action", body, session, { ...h, request });
    return h.json(result, 202);
  }
  if (request.method === "POST" && pathname === "/v1/services/action") {
    const body = await h.readJson(request);
    if (!["start", "stop", "restart", "status"].includes(body.action)) throw new h.ApiError(400, "invalid_action", "Недопустимое действие службы.");
    const statusRow = await env.DB.prepare("SELECT status FROM agent_status WHERE id='primary'").first();
    const status = statusRow ? h.safeJson(statusRow.status, {}) : {};
    const instances = Array.isArray(status.instances) ? status.instances : status.minecraft ? [status.minecraft] : [];
    const minecraft = instances.find((item) => item?.service === body.service && item?.id);
    if (minecraft && body.action !== "status") {
      const result = await enqueueJob(env, `instance_${body.action}`, { instance_id: minecraft.id }, session, { ...h, request });
      return h.json(result, result.reused ? 200 : 202);
    }
    const result = await enqueueJob(env, "service_action", body, session, { ...h, request });
    return h.json(result, result.reused ? 200 : 202);
  }
  if (request.method === "POST" && pathname === "/v1/updates/agent") {
    const body = await h.readJson(request);
    const result = await enqueueJob(env, "agent_update", body, session, { ...h, request });
    return h.json(result, result.reused ? 200 : 202);
  }
  if (request.method === "GET" && pathname === "/v1/audit") {
    h.requirePermission(session, "audit.view");
    const after = Math.max(0, Number.parseInt(url.searchParams.get("after") || "0", 10) || 0);
    const result = after === 0
      ? await env.DB.prepare(
        `SELECT * FROM (SELECT a.id,a.action,a.details,a.created_at,a.target,a.result,a.ip,a.device,u.username
         FROM audit_log a LEFT JOIN users u ON u.id=a.actor_id ORDER BY a.id DESC LIMIT 200) ORDER BY id ASC`,
      ).all()
      : await env.DB.prepare(
        `SELECT a.id,a.action,a.details,a.created_at,a.target,a.result,a.ip,a.device,u.username
         FROM audit_log a LEFT JOIN users u ON u.id=a.actor_id WHERE a.id>? ORDER BY a.id ASC LIMIT 200`,
      ).bind(after).all();
    const events = (result.results || []).map((row) => ({ ...row, details: h.safeJson(row.details, {}) }));
    return h.json({ events, next_after: events.length ? Number(events[events.length - 1].id) : after });
  }
  if (request.method === "GET" && pathname === "/v1/settings") {
    h.requirePermission(session, "settings.manage");
    const result = await env.DB.prepare("SELECT key,value,updated_at FROM settings WHERE substr(key,1,1)!='_' ORDER BY key").all();
    return h.json({ settings: Object.fromEntries((result.results || []).map((row) => [row.key, h.safeJson(row.value, null)])) });
  }
  if (request.method === "PATCH" && pathname === "/v1/settings") {
    h.requirePermission(session, "settings.manage");
    const body = await h.readJson(request);
    const allowed = new Set([
      "console_retention_days", "job_retention_days", "notification_retention_days", "auto_cleanup",
      "backup_schedule_hours", "restart_schedule_hours", "disk_warning_percent", "disk_critical_percent",
    ]);
    const entries = Object.entries(body).filter(([key]) => allowed.has(key)).map(([key, value]) => {
      if (key === "auto_cleanup") return [key, Boolean(value)];
      const number = Number(value);
      if (!Number.isInteger(number)) throw new h.ApiError(400, "invalid_settings", `${key}: требуется целое число.`);
      const ranges = {
        console_retention_days: [1, 365], job_retention_days: [1, 365], notification_retention_days: [1, 730],
        backup_schedule_hours: [0, 8760], restart_schedule_hours: [0, 8760],
        disk_warning_percent: [50, 99], disk_critical_percent: [51, 100],
      };
      const [minimum, maximum] = ranges[key] || [0, Number.MAX_SAFE_INTEGER];
      if (number < minimum || number > maximum) throw new h.ApiError(400, "invalid_settings", `${key}: значение вне допустимого диапазона.`);
      return [key, number];
    });
    if (!entries.length) throw new h.ApiError(400, "invalid_settings", "Нет разрешённых настроек для сохранения.");
    const merged = Object.fromEntries(entries);
    const warning = Number(merged.disk_warning_percent ?? await settingValue(env, "disk_warning_percent", 85));
    const critical = Number(merged.disk_critical_percent ?? await settingValue(env, "disk_critical_percent", 95));
    if (warning >= critical) throw new h.ApiError(400, "invalid_settings", "Критический порог диска должен быть выше предупреждения.");
    const now = Date.now();
    await env.DB.batch(entries.map(([key, value]) => env.DB.prepare(
      `INSERT INTO settings(key,value,updated_by,updated_at) VALUES(?,?,?,?)
       ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_by=excluded.updated_by,updated_at=excluded.updated_at`,
    ).bind(key, JSON.stringify(value), session.user.id, now)));
    await h.addAudit(env, session.user.id, "settings.update", { keys: entries.map(([key]) => key) }, { target: "settings", request });
    return h.json({ ok: true });
  }
  return null;
}

async function routeNotifications(request, env, pathname, url, session, h) {
  if (request.method === "GET" && pathname === "/v1/notifications") {
    const after = Math.max(0, Number.parseInt(url.searchParams.get("after") || "0", 10) || 0);
    const result = await env.DB.prepare(
      `SELECT n.*,CASE WHEN r.user_id IS NULL THEN 0 ELSE 1 END AS is_read FROM notifications n
       LEFT JOIN notification_reads r ON r.notification_id=n.id AND r.user_id=?
       WHERE n.id>? AND (n.user_id IS NULL OR n.user_id=?) ORDER BY n.id ASC LIMIT 200`,
    ).bind(session.user.id, after, session.user.id).all();
    return h.json({ notifications: result.results || [] });
  }
  if (request.method === "POST" && pathname === "/v1/notifications/read") {
    const body = await h.readJson(request);
    const ids = Array.isArray(body.ids) ? body.ids.map(Number).filter((id) => Number.isSafeInteger(id) && id > 0).slice(0, 200) : [];
    if (ids.length) {
      const now = Date.now();
      await env.DB.batch(ids.map((id) => env.DB.prepare(
        "INSERT OR IGNORE INTO notification_reads(notification_id,user_id,read_at) VALUES(?,?,?)",
      ).bind(id, session.user.id, now)));
    }
    return h.json({ ok: true, count: ids.length });
  }
  return null;
}

function transferJson(row) {
  if (!row) return null;
  return { ...row, size_bytes: Number(row.size_bytes || 0), created_at: Number(row.created_at), updated_at: Number(row.updated_at), expires_at: Number(row.expires_at) };
}

async function requireTransferAccess(env, id, session, h, writeAccess = false) {
  const row = await env.DB.prepare("SELECT * FROM transfers WHERE id=?").bind(id).first();
  if (!row) throw new h.ApiError(404, "transfer_not_found", "Передача не найдена.");
  const isOwner = session.user.role === "owner";
  const mayInspectAll = !writeAccess && session.user.permissions.includes("audit.view");
  if (row.requested_by !== session.user.id && !isOwner && !mayInspectAll) {
    throw new h.ApiError(403, "permission_denied", "Нет доступа к этой передаче.");
  }
  return row;
}

async function startMultipart(env, row, h) {
  if (!env.FILES) throw new h.ApiError(503, "file_storage_not_configured", "Хранилище больших файлов R2 не подключено.");
  if (row.multipart_upload_id) return row.multipart_upload_id;
  const upload = await env.FILES.createMultipartUpload(row.object_key, { customMetadata: { transfer_id: row.id, sha256: row.sha256 || "" } });
  await env.DB.prepare("UPDATE transfers SET multipart_upload_id=?,status='uploading',updated_at=? WHERE id=? AND multipart_upload_id IS NULL")
    .bind(upload.uploadId, Date.now(), row.id).run();
  const selected = await env.DB.prepare("SELECT multipart_upload_id FROM transfers WHERE id=?").bind(row.id).first();
  const selectedId = selected?.multipart_upload_id;
  if (!selectedId) {
    try { await upload.abort(); } catch { /* best-effort orphan cleanup */ }
    throw new h.ApiError(409, "multipart_not_started", "Не удалось начать передачу.");
  }
  if (selectedId !== upload.uploadId) {
    try { await upload.abort(); } catch { /* concurrent request won */ }
  }
  return selectedId;
}

async function uploadTransferPart(request, env, row, partNumber, expectedDirection, h) {
  if (row.direction !== expectedDirection) throw new h.ApiError(409, "invalid_transfer_direction", "Направление передачи не соответствует операции.");
  if (!["created", "uploading"].includes(String(row.status)) || Number(row.expires_at) <= Date.now()) {
    throw new h.ApiError(409, "transfer_not_writable", "Передача завершена, отменена или истекла.");
  }
  if (!Number.isInteger(partNumber) || partNumber < 1 || partNumber > 10_000) throw new h.ApiError(400, "invalid_part", "Некорректный номер части.");
  const contentLength = Number(request.headers.get("content-length") || 0);
  if (!Number.isSafeInteger(contentLength) || contentLength <= 0 || contentLength > 16 * 1024 * 1024) {
    throw new h.ApiError(413, "invalid_part_size", "Часть файла должна быть от 1 байта до 16 МиБ.");
  }
  const existing = await env.DB.prepare(
    "SELECT COALESCE(SUM(size_bytes),0) AS total FROM transfer_parts WHERE transfer_id=? AND part_number!=?",
  ).bind(row.id, partNumber).first();
  const totalAfter = Number(existing?.total || 0) + contentLength;
  if (totalAfter > MAX_TRANSFER_SIZE || (Number(row.size_bytes) > 0 && totalAfter > Number(row.size_bytes))) {
    throw new h.ApiError(413, "transfer_too_large", "Части превышают заявленный размер передачи.");
  }
  const uploadId = await startMultipart(env, row, h);
  const upload = env.FILES.resumeMultipartUpload(row.object_key, uploadId);
  const part = await upload.uploadPart(partNumber, request.body);
  const size = Number(request.headers.get("content-length") || 0);
  await env.DB.prepare(
    `INSERT INTO transfer_parts(transfer_id,part_number,etag,size_bytes) VALUES(?,?,?,?)
     ON CONFLICT(transfer_id,part_number) DO UPDATE SET etag=excluded.etag,size_bytes=excluded.size_bytes`,
  ).bind(row.id, partNumber, part.etag, size).run();
  await env.DB.prepare("UPDATE transfers SET status='uploading',updated_at=? WHERE id=?").bind(Date.now(), row.id).run();
  return h.json({ ok: true, part_number: partNumber, etag: part.etag });
}

export async function completeMultipart(env, row, expectedDirection, h) {
  if (row.direction !== expectedDirection) throw new h.ApiError(409, "invalid_transfer_direction", "Направление передачи не соответствует операции.");
  if (!['uploading', 'created'].includes(String(row.status)) || Number(row.expires_at) <= Date.now()) {
    throw new h.ApiError(409, "transfer_not_writable", "Передача завершена, отменена или истекла.");
  }
  // Multipart uploads cannot contain zero parts. Empty configuration files
  // are nevertheless valid, so commit them as a normal zero-byte R2 object.
  if (!row.multipart_upload_id && Number(row.size_bytes) === 0 && env.FILES) {
    await env.FILES.put(row.object_key, new Uint8Array(), {
      customMetadata: { transfer_id: String(row.id), sha256: String(row.sha256 || "") },
    });
    const now = Date.now();
    await env.DB.prepare("UPDATE transfers SET status='ready',size_bytes=0,updated_at=? WHERE id=?").bind(now, row.id).run();
    return h.json({ ok: true, transfer: transferJson({ ...row, size_bytes: 0, status: "ready", updated_at: now }) });
  }
  if (!row.multipart_upload_id || !env.FILES) throw new h.ApiError(409, "multipart_not_started", "Загрузка ещё не начата.");
  const result = await env.DB.prepare("SELECT part_number,etag,size_bytes FROM transfer_parts WHERE transfer_id=? ORDER BY part_number").bind(row.id).all();
  const rows = result.results || [];
  const parts = rows.map((item) => ({ partNumber: Number(item.part_number), etag: item.etag }));
  if (!parts.length) throw new h.ApiError(409, "no_parts", "Не загружено ни одной части.");
  if (parts.some((item, index) => item.partNumber !== index + 1)) {
    throw new h.ApiError(409, "missing_part", "Части передачи должны идти подряд, начиная с 1.");
  }
  const uploadedBytes = rows.reduce((total, item) => total + Number(item.size_bytes || 0), 0);
  if (Number(row.size_bytes) > 0 && uploadedBytes !== Number(row.size_bytes)) {
    throw new h.ApiError(409, "size_mismatch", `Получено ${uploadedBytes} байт вместо ${row.size_bytes}.`);
  }
  const upload = env.FILES.resumeMultipartUpload(row.object_key, row.multipart_upload_id);
  await upload.complete(parts);
  const now = Date.now();
  await env.DB.prepare("UPDATE transfers SET status='ready',size_bytes=?,updated_at=? WHERE id=?").bind(uploadedBytes, now, row.id).run();
  return h.json({ ok: true, transfer: transferJson({ ...row, size_bytes: uploadedBytes, status: "ready", updated_at: now }) });
}

async function routeTransfers(request, env, pathname, session, h) {
  if (request.method === "GET" && pathname === "/v1/transfers") {
    h.requireAnyPermission(session, ["minecraft.files.read", "minecraft.files.write", "audit.view"]);
    const allUsers = new URL(request.url).searchParams.get("all") === "1" && session.user.permissions.includes("audit.view");
    const query = allUsers
      ? env.DB.prepare("SELECT * FROM transfers ORDER BY created_at DESC LIMIT 200")
      : env.DB.prepare("SELECT * FROM transfers WHERE requested_by=? ORDER BY created_at DESC LIMIT 200").bind(session.user.id);
    const result = await query.all();
    return h.json({ transfers: (result.results || []).map(transferJson) });
  }
  if (request.method === "POST" && pathname === "/v1/transfers") {
    const body = await h.readJson(request);
    const direction = body.direction === "download" ? "download" : "upload";
    h.requirePermission(session, direction === "upload" ? "minecraft.files.write" : "minecraft.files.read");
    const instanceId = normalizeInstanceId(body.instance_id, h.ApiError);
    const relativePath = normalizeRelativePath(body.path || "", h.ApiError, true);
    const fileName = String(body.file_name || relativePath.split("/").pop() || "transfer.bin").trim();
    normalizeFilename(fileName, h.ApiError);
    const size = Math.max(0, Number(body.size_bytes || 0));
    if (!Number.isSafeInteger(size) || size > MAX_TRANSFER_SIZE) throw new h.ApiError(413, "transfer_too_large", "Файл превышает лимит 50 ГиБ.");
    const sha256 = typeof body.sha256 === "string" && /^[a-f0-9]{64}$/i.test(body.sha256) ? body.sha256.toLowerCase() : null;
    if (direction === "upload" && !sha256) throw new h.ApiError(400, "hash_required", "Для загрузки требуется SHA-256.");
    const overwrite = direction === "upload" && body.overwrite === true ? 1 : 0;
    const now = Date.now();
    const id = crypto.randomUUID();
    const objectKey = `transfers/${id}/${encodeURIComponent(fileName)}`;
    await env.DB.prepare(
      `INSERT INTO transfers(id,direction,requested_by,instance_id,relative_path,file_name,object_key,size_bytes,sha256,overwrite,status,created_at,updated_at,expires_at)
       VALUES(?,?,?,?,?,?,?,?,?,?,'created',?,?,?)`,
    ).bind(id, direction, session.user.id, instanceId, relativePath, fileName, objectKey, size, sha256, overwrite, now, now, now + TRANSFER_TTL_MS).run();
    await h.addAudit(env, session.user.id, "transfer.create", { transfer_id: id, direction, instance_id: instanceId, path: relativePath, size }, { target: instanceId, request });
    const transfer = transferJson({ id, direction, requested_by: session.user.id, instance_id: instanceId, relative_path: relativePath, file_name: fileName, object_key: objectKey, size_bytes: size, sha256, overwrite, status: "created", created_at: now, updated_at: now, expires_at: now + TRANSFER_TTL_MS });
    if (direction === "download") {
      const jobResult = await enqueueJob(env, "transfer_export", { instance_id: instanceId, path: relativePath, transfer_id: id, file_name: fileName }, session, { ...h, request });
      return h.json({ transfer, ...jobResult }, 202);
    }
    return h.json({ transfer }, 201);
  }
  const match = pathname.match(/^\/v1\/transfers\/([^/]+)(?:\/(parts\/(\d+)|complete|commit|content|cancel))?$/);
  if (!match) return null;
  const id = decodeOpaqueId(match[1], h.ApiError);
  const action = match[2] || "";
  const row = await requireTransferAccess(env, id, session, h, request.method !== "GET");
  h.requirePermission(session, row.direction === "download" ? "minecraft.files.read" : "minecraft.files.write");
  if (request.method === "GET" && !action) return h.json({ transfer: transferJson(row) });
  if (request.method === "PUT" && action.startsWith("parts/")) return uploadTransferPart(request, env, row, Number(match[3]), "upload", h);
  if (request.method === "POST" && action === "complete") return completeMultipart(env, row, "upload", h);
  if (request.method === "POST" && action === "commit") {
    if (row.direction !== "upload" || row.status !== "ready") throw new h.ApiError(409, "transfer_not_ready", "Файл ещё не загружен.");
    const result = await enqueueJob(env, "transfer_import", { instance_id: row.instance_id, path: row.relative_path, transfer_id: row.id, file_name: row.file_name, size_bytes: Number(row.size_bytes), sha256: row.sha256, overwrite: Boolean(row.overwrite) }, session, { ...h, request });
    await env.DB.prepare("UPDATE transfers SET status='importing',updated_at=? WHERE id=?").bind(Date.now(), id).run();
    return h.json({ ...result, transfer: transferJson({ ...row, status: "importing", updated_at: Date.now() }) }, 202);
  }
  if (request.method === "GET" && action === "content") {
    if (row.direction !== "download" || row.status !== "ready" || !env.FILES) throw new h.ApiError(409, "transfer_not_ready", "Файл ещё не подготовлен.");
    const object = await env.FILES.get(row.object_key, { range: request.headers });
    if (!object) throw new h.ApiError(404, "transfer_content_missing", "Файл передачи не найден.");
    const headers = new Headers();
    object.writeHttpMetadata(headers);
    headers.set("etag", object.httpEtag);
    headers.set("content-disposition", `attachment; filename*=UTF-8''${encodeURIComponent(row.file_name)}`);
    headers.set("cache-control", "private, no-store");
    return new Response(object.body, { status: object.range ? 206 : 200, headers });
  }
  if (request.method === "POST" && action === "cancel") {
    if (row.multipart_upload_id && env.FILES) {
      try { await env.FILES.resumeMultipartUpload(row.object_key, row.multipart_upload_id).abort(); } catch { /* already completed */ }
    }
    if (env.FILES) await env.FILES.delete(row.object_key);
    await env.DB.prepare("UPDATE transfers SET status='cancelled',updated_at=? WHERE id=?").bind(Date.now(), id).run();
    return h.json({ ok: true });
  }
  return null;
}

export async function routeControlPlane(request, env, pathname, url, session, helpers) {
  const h = { ...helpers, request };
  if (request.method === "GET" && pathname === "/v1/sync") return routeSync(request, env, url, session, h);
  if (request.method === "GET" && pathname === "/v1/health") {
    const row = await env.DB.prepare("SELECT status,updated_at FROM agent_status WHERE id='primary'").first();
    const status = row ? h.safeJson(row.status, {}) : {};
    const visibleStatus = filterStatusForSession(status, session);
    const age = row ? Date.now() - Number(row.updated_at) : null;
    return h.json({ ok: true, protocol: { api: 2 }, backend: true, database: true, agent: Boolean(row && age < AGENT_ONLINE_MAX_AGE_MS), agent_age_ms: age, agent_version: status.agent_version || "unknown", agent_protocol: status.protocol_version || null, server: visibleStatus.server || null, minecraft: visibleStatus.minecraft || null });
  }
  let response = await routeInstances(request, env, pathname, session, h);
  if (response) return response;
  response = await routeJobs(request, env, pathname, url, session, h);
  if (response) return response;
  response = await routeFiles(request, env, pathname, session, h);
  if (response) return response;
  response = await routeBackupsAndAdmin(request, env, pathname, url, session, h);
  if (response) return response;
  response = await routeNotifications(request, env, pathname, url, session, h);
  if (response) return response;
  response = await routeTransfers(request, env, pathname, session, h);
  if (response) return response;
  return null;
}

export async function routeAgentControlPlane(request, env, pathname, helpers) {
  const h = { ...helpers, request };
  if (request.method === "GET" && pathname === "/v1/agent/jobs") {
    return h.json(await claimAgentJobs(env, h));
  }
  const progressMatch = pathname.match(/^\/v1\/agent\/jobs\/([^/]+)\/progress$/);
  if (request.method === "POST" && progressMatch) {
    const id = decodeOpaqueId(progressMatch[1], h.ApiError);
    const body = await h.readJson(request);
    const progress = Math.max(0, Math.min(100, Math.round(Number(body.progress || 0))));
    const now = Date.now();
    const row = await env.DB.prepare("SELECT status FROM jobs WHERE id=?").bind(id).first();
    if (!row) throw new h.ApiError(404, "job_not_found", "Операция не найдена.");
    if (!JOB_ACTIVE.has(String(row.status))) return h.json({ ok: true, already_finished: true, cancel_requested: Boolean(row.cancel_requested || row.status === "cancelled") });
    await env.DB.prepare(
      `UPDATE jobs SET status='running',progress=?,stage=?,message=?,heartbeat_at=?,started_at=COALESCE(started_at,?),updated_at=? WHERE id=?`,
    ).bind(progress, String(body.stage || "").slice(0, 100), String(body.message || "").slice(0, 1000), now, now, now, id).run();
    return h.json({ ok: true, cancel_requested: Boolean((await env.DB.prepare("SELECT cancel_requested FROM jobs WHERE id=?").bind(id).first())?.cancel_requested) });
  }
  const resultMatch = pathname.match(/^\/v1\/agent\/jobs\/([^/]+)\/result$/);
  if (request.method === "POST" && resultMatch) {
    const id = decodeOpaqueId(resultMatch[1], h.ApiError);
    const body = await h.readJson(request, 256 * 1024);
    const row = await env.DB.prepare("SELECT * FROM jobs WHERE id=?").bind(id).first();
    if (!row) throw new h.ApiError(404, "job_not_found", "Операция не найдена.");
    if (!JOB_ACTIVE.has(String(row.status))) return h.json({ ok: true, already_finished: true });
    const status = body.status === "cancelled" ? "cancelled" : body.status === "completed" ? "completed" : "failed";
    const now = Date.now();
    const result = body.result && typeof body.result === "object" ? body.result : { message: String(body.result || "") };
    const message = String(body.message || result.message || (status === "completed" ? "Операция выполнена." : "Операция завершилась с ошибкой.")).slice(0, 1000);
    const originalPayload = h.safeJson(row.payload, {});
    const storedPayload = JSON.stringify(redactJobValue(originalPayload));
    await env.DB.prepare(
      `UPDATE jobs SET status=?,progress=?,stage=?,message=?,payload=?,result=?,error_code=?,heartbeat_at=?,completed_at=?,updated_at=? WHERE id=?`,
    ).bind(status, status === "completed" ? 100 : Number(row.progress || 0), status, message, storedPayload, boundedJobResult(result), body.error_code || null, now, now, now, id).run();
    const severity = status === "completed" ? "success" : status === "cancelled" ? "warning" : "error";
    if (["transfer_export", "backup_export"].includes(String(row.type)) && result.transfer_id) {
      await env.DB.prepare("UPDATE transfers SET status=?,size_bytes=?,sha256=?,file_name=COALESCE(?,file_name),error=?,updated_at=? WHERE id=?")
        .bind(status === "completed" ? "ready" : "failed", Number(result.size || 0), result.sha256 || null, result.file_name || null, status === "completed" ? null : message, now, String(result.transfer_id)).run();
    } else if (["transfer_import", "instance_create", "instance_update_files"].includes(String(row.type)) && row.payload) {
      if (originalPayload.transfer_id) {
        await env.DB.prepare("UPDATE transfers SET status=?,error=?,updated_at=? WHERE id=?")
          .bind(status === "completed" ? "completed" : "failed", status === "completed" ? null : message, now, String(originalPayload.transfer_id)).run();
      }
    }
    await createNotification(env, `job.${status}`, severity, status === "completed" ? "Операция завершена" : status === "cancelled" ? "Операция отменена" : "Ошибка операции", `${row.type}: ${message}`, row.instance_id || row.type, row.requested_by);
    await h.addAudit(env, row.requested_by, `job.${row.type}.${status}`, { job_id: id, message, error_code: body.error_code || null }, { target: row.instance_id || row.type, result: status });
    return h.json({ ok: true });
  }
  const transferMatch = pathname.match(/^\/v1\/agent\/transfers\/([^/]+)(?:\/(parts\/(\d+)|complete|content))?$/);
  if (transferMatch) {
    const id = decodeOpaqueId(transferMatch[1], h.ApiError);
    const row = await env.DB.prepare("SELECT * FROM transfers WHERE id=?").bind(id).first();
    if (!row) throw new h.ApiError(404, "transfer_not_found", "Передача не найдена.");
    const action = transferMatch[2] || "";
    if (request.method === "GET" && !action) return h.json({ transfer: transferJson(row) });
    if (request.method === "PUT" && action.startsWith("parts/")) return uploadTransferPart(request, env, row, Number(transferMatch[3]), "download", h);
    if (request.method === "POST" && action === "complete") return completeMultipart(env, row, "download", h);
    if (request.method === "GET" && action === "content") {
      if (!env.FILES || row.status !== "ready") throw new h.ApiError(409, "transfer_not_ready", "Файл ещё не готов.");
      const object = await env.FILES.get(row.object_key, { range: request.headers });
      if (!object) throw new h.ApiError(404, "transfer_content_missing", "Файл передачи не найден.");
      const headers = new Headers({ "content-type": "application/octet-stream", "cache-control": "private, no-store" });
      object.writeHttpMetadata(headers);
      headers.set("etag", object.httpEtag);
      return new Response(object.body, { status: object.range ? 206 : 200, headers });
    }
  }
  return null;
}

export async function claimAgentJobs(env, h) {
  const staleBefore = Date.now() - 5 * 60 * 1000;
  const result = await env.DB.prepare(
    `SELECT * FROM jobs WHERE status='pending' OR (status IN ('claimed','running') AND heartbeat_at<?)
     ORDER BY created_at ASC LIMIT 10`,
  ).bind(staleBefore).all();
  const rows = result.results || [];
  const now = Date.now();
  if (rows.length) {
    await env.DB.batch(rows.map((row) => env.DB.prepare(
      `UPDATE jobs SET status='claimed',claimed_at=COALESCE(claimed_at,?),heartbeat_at=?,updated_at=?
       WHERE id=? AND status IN ('pending','claimed','running')`,
    ).bind(now, now, now, row.id)));
  }
  const cancellations = await env.DB.prepare(
    "SELECT id FROM jobs WHERE cancel_requested=1 AND (status IN ('claimed','running') OR (status='cancelled' AND updated_at>?)) LIMIT 100",
  ).bind(now - 5 * 60 * 1000).all();
  return {
    jobs: rows.map((row) => jobJson({ ...row, status: "claimed", claimed_at: row.claimed_at || now, heartbeat_at: now, updated_at: now }, h.safeJson, true)),
    cancel: (cancellations.results || []).map((row) => row.id),
    server_time: now,
  };
}

export function normalizeEvent(event) {
  if (!event || typeof event !== "object" || typeof event.message !== "string") return null;
  const kind = ["server", "minecraft", "audit"].includes(event.kind) ? event.kind : "server";
  const instanceId = event.instance_id && INSTANCE_ID_RE.test(String(event.instance_id)) ? String(event.instance_id) : null;
  const message = event.message.trim().slice(0, 8000);
  if (!message) return null;
  const level = ["INFO", "WARN", "ERROR", "DEBUG"].includes(event.level) ? event.level : inferLevel(message);
  return { kind, message, instance_id: instanceId, source: String(event.source || kind).slice(0, 64), level };
}

export async function notifyHeartbeatTransitions(env, oldStatus, newStatus) {
  const list = (status) => Array.isArray(status?.instances) && status.instances.length
    ? status.instances
    : status?.minecraft ? [status.minecraft] : [];
  const oldInstances = new Map(list(oldStatus).filter((item) => item?.id).map((item) => [String(item.id), item]));
  for (const current of list(newStatus)) {
    if (!current?.id) continue;
    const id = String(current.id);
    const previous = oldInstances.get(id) || {};
    const state = String(current.state || current.startup?.state || (current.active ? "STARTING" : "OFFLINE")).toUpperCase();
    const oldState = String(previous.state || previous.startup?.state || (previous.active ? "STARTING" : "OFFLINE")).toUpperCase();
    if (state === "RUNNING" && oldState !== "RUNNING") {
      await createNotification(env, "minecraft.started", "success", `${current.name || id} запущен`, "Minecraft готов принимать игроков.", id);
    } else if (state === "OFFLINE" && !["", "OFFLINE"].includes(oldState)) {
      await createNotification(env, "minecraft.stopped", "info", `${current.name || id} остановлен`, "Процесс Minecraft завершён.", id);
    }
    if (state === "CRASHED" && oldState !== "CRASHED") {
      await createNotification(env, "minecraft.crashed", "error", `${current.name || id} завершился с ошибкой`, current.crash?.summary || "Откройте журнал для подробностей.", id);
    }
  }
  const diskPercent = Number(newStatus?.server?.metrics?.filesystem?.percent);
  const oldDiskPercent = Number(oldStatus?.server?.metrics?.filesystem?.percent);
  // Configured thresholds cannot be lower than 50%. Avoid two D1 reads on
  // every high-frequency heartbeat while the disk is healthy.
  if (Number.isFinite(diskPercent) && diskPercent >= 50) {
    const configured = await env.DB.prepare(
      "SELECT key,value FROM settings WHERE key IN ('disk_warning_percent','disk_critical_percent')",
    ).all();
    const thresholds = Object.fromEntries((configured.results || []).map((row) => [row.key, safeScheduledJson(row.value, null)]));
    const warning = Number(thresholds.disk_warning_percent ?? 85);
    const critical = Number(thresholds.disk_critical_percent ?? 95);
    if (diskPercent >= warning && (!Number.isFinite(oldDiskPercent) || oldDiskPercent < warning)) {
      await createNotification(env, "storage.low", diskPercent >= critical ? "error" : "warning", "Заканчивается место на диске", `Занято ${diskPercent.toFixed(1)}%.`, "/");
    }
  }
}

async function settingValue(env, key, fallback) {
  const row = await env.DB.prepare("SELECT value FROM settings WHERE key=?").bind(key).first();
  return row ? safeScheduledJson(row.value, fallback) : fallback;
}

function safeScheduledJson(value, fallback) {
  try { return JSON.parse(value); } catch { return fallback; }
}

async function saveInternalSetting(env, key, value, now) {
  await env.DB.prepare(
    `INSERT INTO settings(key,value,updated_by,updated_at) VALUES(?,?,NULL,?)
     ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_by=NULL,updated_at=excluded.updated_at`,
  ).bind(key, JSON.stringify(value), now).run();
}

export async function runScheduledMaintenance(env, helpers) {
  const now = Date.now();
  const agentStatusRow = await env.DB.prepare("SELECT status,updated_at FROM agent_status WHERE id='primary'").first();
  const agentOffline = !agentStatusRow || now - Number(agentStatusRow.updated_at || 0) > 15_000;
  const offlineNotified = Boolean(await settingValue(env, "_agent_offline_notified", false));
  if (agentOffline && !offlineNotified) {
    await createNotification(env, "agent.offline", "error", "Agent offline", "Домашний сервер не отвечает Control Hub.", "agent");
    await saveInternalSetting(env, "_agent_offline_notified", true, now);
  } else if (!agentOffline && offlineNotified) {
    await createNotification(env, "agent.online", "success", "Связь с Agent восстановлена", "Состояние домашнего сервера снова обновляется.", "agent");
    await saveInternalSetting(env, "_agent_offline_notified", false, now);
  }
  const autoCleanup = Boolean(await settingValue(env, "auto_cleanup", true));
  if (autoCleanup) {
    const consoleDays = Math.max(1, Math.min(365, Number(await settingValue(env, "console_retention_days", 30)) || 30));
    const jobDays = Math.max(1, Math.min(365, Number(await settingValue(env, "job_retention_days", 30)) || 30));
    const notificationDays = Math.max(1, Math.min(730, Number(await settingValue(env, "notification_retention_days", 90)) || 90));
    const expired = await env.DB.prepare("SELECT id,object_key FROM transfers WHERE expires_at<? LIMIT 500").bind(now).all();
    if (env.FILES) {
      for (const row of expired.results || []) {
        try { await env.FILES.delete(row.object_key); } catch { /* cleanup retries next hour */ }
      }
    }
    const redactedResult = JSON.stringify({ redacted: true, message: "Подробный результат удалён по политике хранения." });
    await env.DB.batch([
      env.DB.prepare("DELETE FROM console_events WHERE created_at<?").bind(now - consoleDays * 86_400_000),
      env.DB.prepare("DELETE FROM jobs WHERE status IN ('completed','failed','cancelled') AND updated_at<?").bind(now - jobDays * 86_400_000),
      env.DB.prepare(
        `UPDATE jobs SET result=? WHERE type IN ('file_read','log_read','minecraft_command','player_action')
         AND status IN ('completed','failed','cancelled') AND updated_at<? AND result IS NOT NULL AND result!=?`,
      ).bind(redactedResult, now - 60 * 60 * 1000, redactedResult),
      env.DB.prepare("DELETE FROM notifications WHERE created_at<?").bind(now - notificationDays * 86_400_000),
      env.DB.prepare("DELETE FROM transfers WHERE expires_at<?").bind(now),
      env.DB.prepare("DELETE FROM command_queue WHERE status IN ('completed','failed') AND completed_at<?").bind(now - jobDays * 86_400_000),
    ]);
  }
  const owner = await env.DB.prepare("SELECT * FROM users WHERE role='owner' AND enabled=1 ORDER BY created_at LIMIT 1").first();
  const statusRow = agentStatusRow;
  const status = statusRow ? helpers.safeJson(statusRow.status, {}) : {};
  const instances = Array.isArray(status.instances) ? status.instances : status.minecraft ? [status.minecraft] : [];
  if (!owner || !instances.length) return;
  const session = { user: { ...owner, permissions: CONTROL_ROLE_PERMISSIONS.owner } };
  for (const [kind, type, key] of [["backup", "backup_create", "backup_schedule_hours"], ["restart", "instance_restart", "restart_schedule_hours"]]) {
    const hours = Math.max(0, Math.min(8760, Number(await settingValue(env, key, 0)) || 0));
    if (!hours) continue;
    const lastKey = `_last_${kind}_schedule_at`;
    const last = Number(await settingValue(env, lastKey, 0)) || 0;
    if (now - last < hours * 3_600_000) continue;
    for (const item of instances) {
      if (!item?.id || (type === "instance_restart" && !item.active)) continue;
      try {
        await enqueueJob(env, type, { instance_id: item.id, reason: "scheduled", comment: "Автоматическая задача" }, session, { ...helpers, request: null });
      } catch (error) {
        // An exclusive lock is expected when a manual operation overlaps the
        // scheduler. Continue with other instances and retry next interval.
        if (!(error instanceof helpers.ApiError) || ![409, 429].includes(error.status)) throw error;
      }
    }
    await saveInternalSetting(env, lastKey, now);
  }
}
