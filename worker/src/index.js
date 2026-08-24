import {
  CONTROL_PERMISSIONS,
  CONTROL_ROLE_PERMISSIONS,
  claimAgentJobs,
  filterStatusForSession,
  normalizeEvent,
  notifyHeartbeatTransitions,
  routeAgentControlPlane,
  routeControlPlane,
  runScheduledMaintenance,
} from "./control_plane.js";

const encoder = new TextEncoder();
const decoder = new TextDecoder();

const PASSWORD_ITERATIONS = 100_000;
const ACCESS_TOKEN_TTL_SECONDS = 12 * 60 * 60;
const AGENT_COMMAND_BATCH = 20;
const COMMAND_CLAIM_STALE_AFTER_MS = 15 * 60 * 1000;
const AGENT_ONLINE_MAX_AGE_MS = 10 * 1000;
const POWER_STATUS_CACHE_MAX_AGE_MS = 3 * 1000;
const YANDEX_REQUEST_TIMEOUT_MS = 5 * 1000;
const MAX_CONSOLE_EVENTS_PER_PUSH = 100;
const MAX_CONSOLE_MESSAGE_LENGTH = 8_000;
const MAX_SHELL_COMMAND_LENGTH = 512;
const MAX_MINECRAFT_COMMAND_LENGTH = 256;

const PERMISSIONS = new Set([
  "power_view",
  "power_control",
  "server_view",
  "server_command",
  "minecraft_view",
  "minecraft_command",
  "user_manage",
  ...CONTROL_PERMISSIONS,
]);

const ROLE_PERMISSIONS = {
  owner: [
    "power_view",
    "power_control",
    "server_view",
    "server_command",
    "minecraft_view",
    "minecraft_command",
    "user_manage",
    ...CONTROL_ROLE_PERMISSIONS.owner,
  ],
  admin: [
    "power_view",
    "power_control",
    "server_view",
    "server_command",
    "minecraft_view",
    "minecraft_command",
    ...CONTROL_ROLE_PERMISSIONS.admin,
  ],
  user: ["minecraft_view", "minecraft_command", ...CONTROL_ROLE_PERMISSIONS.operator],
};

class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

function decodeOpaqueId(value) {
  let decoded;
  try { decoded = decodeURIComponent(value); } catch { throw new ApiError(400, "invalid_id", "Некорректный идентификатор."); }
  if (!/^[A-Za-z0-9_.:-]{1,128}$/.test(decoded)) throw new ApiError(400, "invalid_id", "Некорректный идентификатор.");
  return decoded;
}

export default {
  async fetch(request, env, ctx) {
    if (request.method === "OPTIONS") {
      return new Response(null, {
        status: 204,
        headers: {
          "access-control-allow-methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
          "access-control-allow-headers": "authorization, content-type, x-bootstrap-key, x-agent-key",
          "access-control-max-age": "86400",
        },
      });
    }

    try {
      return await route(request, env, ctx);
    } catch (error) {
      if (error instanceof ApiError) {
        return json({ error: error.code, message: error.message }, error.status);
      }
      console.error("Unhandled request error", error);
      return json({ error: "internal_error", message: "Внутренняя ошибка сервиса." }, 500);
    }
  },
  async scheduled(_controller, env, ctx) {
    ctx.waitUntil(runScheduledMaintenance(env, { ApiError, json, readJson, safeJson, requirePermission, requireAnyPermission, addAudit }));
  },
};

async function route(request, env, ctx) {
  const url = new URL(request.url);
  const { pathname } = url;
  const { method } = request;

  if (method === "GET" && pathname === "/health") {
    return json({ ok: true, service: "server-control-hub", time: Date.now() });
  }

  if (method === "POST" && pathname === "/v1/setup") {
    return setupOwner(request, env);
  }

  if (method === "POST" && pathname === "/v1/login") {
    return login(request, env);
  }

  if (pathname.startsWith("/v1/agent/")) {
    return routeAgent(request, env, pathname, ctx);
  }

  const session = await requireSession(request, env);

  const controlPlaneResponse = await routeControlPlane(request, env, pathname, url, session, {
    ApiError,
    json,
    readJson,
    safeJson,
    requirePermission,
    requireAnyPermission,
    addAudit,
    enqueueCommand,
  });
  if (controlPlaneResponse) return controlPlaneResponse;

  if (method === "GET" && pathname === "/v1/me") {
    return json({ user: publicUser(session.user) });
  }

  if (method === "GET" && pathname === "/v1/power/status") {
    requireAnyPermission(session, ["power_view", "power_control", "server.view", "server.power"]);
    return json({ power: await getYandexPowerStatus(env) });
  }

  if (method === "POST" && pathname === "/v1/power/action") {
    requireAnyPermission(session, ["power_control", "server.power"]);
    return powerAction(request, env, session);
  }

  if (method === "GET" && pathname === "/v1/server/status") {
    requireAnyPermission(session, ["server_view", "minecraft_view", "server.view", "minecraft.view"]);
    const record = await env.DB.prepare("SELECT status, updated_at FROM agent_status WHERE id = 'primary'").first();
    if (!record) {
      return json({ online: false, status: null, updated_at: null });
    }
    const updatedAt = Number(record.updated_at);
    const ageMs = Math.max(0, Date.now() - updatedAt);
    return json({
      online: ageMs < AGENT_ONLINE_MAX_AGE_MS,
      status: filterStatusForSession(safeJson(record.status, {}), session),
      updated_at: updatedAt,
      age_ms: ageMs,
    });
  }

  if (method === "GET" && pathname === "/v1/server/logs") {
    requirePermission(session, "server_view");
    return getConsoleEvents(env, "server", url.searchParams.get("after"), url.searchParams.get("latest") === "1");
  }

  if (method === "GET" && pathname === "/v1/minecraft/logs") {
    requirePermission(session, "minecraft_view");
    return getConsoleEvents(env, "minecraft", url.searchParams.get("after"), url.searchParams.get("latest") === "1");
  }

  if (method === "POST" && pathname === "/v1/server/command") {
    requirePermission(session, "server_command");
    return queueShellCommand(request, env, session);
  }

  if (method === "POST" && pathname === "/v1/server/action") {
    requirePermission(session, "server_command");
    return queueServerAction(request, env, session);
  }

  if (method === "POST" && pathname === "/v1/minecraft/command") {
    requirePermission(session, "minecraft_command");
    return queueMinecraftCommand(request, env, session);
  }

  if (method === "POST" && pathname === "/v1/minecraft/action") {
    requirePermission(session, "minecraft_command");
    return queueMinecraftAction(request, env, session);
  }

  if (method === "GET" && pathname === "/v1/admin/users") {
    requireAnyPermission(session, ["user_manage", "users.manage"]);
    return listUsers(env);
  }

  if (method === "POST" && pathname === "/v1/admin/users") {
    requireAnyPermission(session, ["user_manage", "users.manage"]);
    return createUser(request, env, session);
  }

  const userMatch = pathname.match(/^\/v1\/admin\/users\/([^/]+)$/);
  if (userMatch && method === "PATCH") {
    requireAnyPermission(session, ["user_manage", "users.manage"]);
    return updateUser(request, env, session, decodeOpaqueId(userMatch[1]));
  }

  const passwordMatch = pathname.match(/^\/v1\/admin\/users\/([^/]+)\/password$/);
  if (passwordMatch && method === "POST") {
    requireAnyPermission(session, ["user_manage", "users.manage"]);
    return resetPassword(request, env, session, decodeOpaqueId(passwordMatch[1]));
  }

  const revokeMatch = pathname.match(/^\/v1\/admin\/users\/([^/]+)\/revoke$/);
  if (revokeMatch && method === "POST") {
    requireAnyPermission(session, ["user_manage", "users.manage"]);
    return revokeUserSessions(env, session, decodeOpaqueId(revokeMatch[1]));
  }

  if (userMatch && method === "DELETE") {
    requireAnyPermission(session, ["user_manage", "users.manage"]);
    return deleteUser(env, session, decodeOpaqueId(userMatch[1]));
  }

  throw new ApiError(404, "not_found", "Маршрут не найден.");
}

async function setupOwner(request, env) {
  const configuredKey = requiredSecret(env, "BOOTSTRAP_KEY");
  const submittedKey = request.headers.get("x-bootstrap-key") || "";
  if (!constantTimeTextEqual(submittedKey, configuredKey)) {
    throw new ApiError(401, "invalid_bootstrap_key", "Неверный ключ первоначальной настройки.");
  }

  const count = await env.DB.prepare("SELECT COUNT(*) AS count FROM users").first();
  if (Number(count.count) !== 0) {
    throw new ApiError(409, "already_configured", "Владелец уже создан.");
  }

  const body = await readJson(request);
  const username = validateUsername(body.username);
  const password = validatePassword(body.password);
  const passwordRecord = await createPasswordRecord(password);
  const now = Date.now();
  const user = {
    id: crypto.randomUUID(),
    username,
    role: "owner",
    permissions: ROLE_PERMISSIONS.owner,
    enabled: 1,
    token_version: 1,
    created_at: now,
    updated_at: now,
  };

  try {
    await env.DB.prepare(
      `INSERT INTO users (
        id, username, password_salt, password_hash, password_iterations,
        role, permissions, enabled, token_version, created_at, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    )
      .bind(
        user.id,
        user.username,
        passwordRecord.salt,
        passwordRecord.hash,
        passwordRecord.iterations,
        user.role,
        JSON.stringify(user.permissions),
        user.enabled,
        user.token_version,
        user.created_at,
        user.updated_at,
      )
      .run();
  } catch (error) {
    if (String(error?.message || error).includes("UNIQUE")) {
      throw new ApiError(409, "already_configured", "Владелец уже создан.");
    }
    throw error;
  }

  await addAudit(env, user.id, "owner.bootstrap", { username });
  const token = await issueAccessToken(env, user);
  return json({ token, user: publicUser(user) }, 201);
}

async function login(request, env) {
  const body = await readJson(request);
  const username = validateUsername(body.username);
  const password = typeof body.password === "string" ? body.password : "";
  const row = await env.DB.prepare("SELECT * FROM users WHERE username = ? COLLATE NOCASE").bind(username).first();
  const now = Date.now();

  // Do not reveal whether the username exists, is disabled or has a bad password.
  const invalid = () => new ApiError(401, "invalid_credentials", "Неверный логин или пароль.");
  if (!row || !Number(row.enabled)) {
    throw invalid();
  }
  if (row.locked_until && Number(row.locked_until) > now) {
    throw new ApiError(429, "temporarily_locked", "Слишком много попыток. Повторите позже.");
  }

  const verified = await verifyPassword(password, row);
  if (!verified) {
    const failures = Number(row.failed_logins || 0) + 1;
    const lockedUntil = failures >= 6 ? now + 10 * 60 * 1000 : null;
    await env.DB.prepare(
      "UPDATE users SET failed_logins = ?, locked_until = ?, updated_at = ? WHERE id = ?",
    )
      .bind(failures, lockedUntil, now, row.id)
      .run();
    throw invalid();
  }

  await env.DB.prepare(
    "UPDATE users SET failed_logins = 0, locked_until = NULL, last_login_at = ?, updated_at = ? WHERE id = ?",
  )
    .bind(now, now, row.id)
    .run();

  const user = normalizeDbUser({ ...row, last_login_at: now, updated_at: now });
  const token = await issueAccessToken(env, user);
  await addAudit(env, user.id, "user.login", {});
  return json({ token, user: publicUser(user) });
}

async function routeAgent(request, env, pathname, ctx) {
  requireAgent(request, env);

  const controlPlaneResponse = await routeAgentControlPlane(request, env, pathname, {
    ApiError,
    json,
    readJson,
    safeJson,
    addAudit,
  });
  if (controlPlaneResponse) return controlPlaneResponse;

  if (request.method === "GET" && pathname === "/v1/agent/sync") {
    const commands = await claimAgentCommandsData(env);
    const jobs = await claimAgentJobs(env, { safeJson });
    return json({ ...commands, ...jobs, server_time: Date.now() });
  }

  if (request.method === "POST" && pathname === "/v1/agent/heartbeat") {
    const body = await readJson(request, 512 * 1024);
    const status = {
      server: isObject(body.server) ? body.server : {},
      minecraft: isObject(body.minecraft) ? body.minecraft : {},
      instances: Array.isArray(body.instances) ? body.instances.slice(0, 100) : [],
      selected_instance_id: typeof body.selected_instance_id === "string" ? body.selected_instance_id.slice(0, 48) : null,
      storage: isObject(body.storage) ? body.storage : {},
      system: isObject(body.system) ? body.system : {},
      processes: Array.isArray(body.processes) ? body.processes.slice(0, 200) : [],
      services: Array.isArray(body.services) ? body.services.slice(0, 100) : [],
      java: Array.isArray(body.java) ? body.java.slice(0, 50) : [],
      backups: Array.isArray(body.backups) ? body.backups.slice(0, 500) : [],
      health: isObject(body.health) ? body.health : {},
      protocol_version: Number(body.protocol_version) || 1,
      agent_version: typeof body.agent_version === "string" ? body.agent_version.slice(0, 64) : "unknown",
    };
    const now = Date.now();
    const [previousResult] = await env.DB.batch([
      env.DB.prepare("SELECT status FROM agent_status WHERE id = 'primary'"),
      env.DB.prepare(
        `INSERT INTO agent_status (id, status, updated_at) VALUES ('primary', ?, ?)
         ON CONFLICT(id) DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at`,
      ).bind(JSON.stringify(status), now),
    ]);
    const previous = previousResult?.results?.[0];
    const previousStatus = previous ? safeJson(previous.status, {}) : {};
    const notifications = notifyHeartbeatTransitions(env, previousStatus, status).catch((error) => {
      console.error("Heartbeat transition notification failed", error);
    });
    if (ctx?.waitUntil) ctx.waitUntil(notifications);
    else await notifications;
    return json({ ok: true, server_time: now });
  }

  if (request.method === "POST" && pathname === "/v1/agent/events") {
    const body = await readJson(request, 1024 * 1024);
    const events = Array.isArray(body.events) ? body.events : [];
    if (events.length > MAX_CONSOLE_EVENTS_PER_PUSH) {
      throw new ApiError(400, "too_many_events", "Слишком много событий за один запрос.");
    }
    const now = Date.now();
    const statements = [];
    for (const event of events) {
      const normalized = normalizeEvent(event);
      if (!normalized) continue;
      statements.push(
        env.DB.prepare("INSERT INTO console_events (kind, message, created_at, instance_id, source, level) VALUES (?, ?, ?, ?, ?, ?)").bind(
          normalized.kind,
          normalized.message,
          now,
          normalized.instance_id,
          normalized.source,
          normalized.level,
        ),
      );
    }
    if (statements.length) await env.DB.batch(statements);
    return json({ ok: true, accepted: statements.length });
  }

  if (request.method === "GET" && pathname === "/v1/agent/commands") {
    return claimAgentCommands(env);
  }

  const resultMatch = pathname.match(/^\/v1\/agent\/commands\/([^/]+)\/result$/);
  if (request.method === "POST" && resultMatch) {
    return completeAgentCommand(request, env, decodeOpaqueId(resultMatch[1]));
  }

  throw new ApiError(404, "not_found", "Маршрут агента не найден.");
}

async function powerAction(request, env, session) {
  const body = await readJson(request);
  const state = body.state;
  if (!["on", "off"].includes(state)) {
    throw new ApiError(400, "invalid_state", "Укажите состояние on или off.");
  }

  if (state === "on") {
    const power = await setYandexPower(env, true);
    await addAudit(env, session.user.id, "power.on", {});
    return json({ ok: true, mode: "direct", power });
  }

  if (body.force === true) {
    if (session.user.role !== "owner") {
      throw new ApiError(403, "owner_required", "Принудительное отключение доступно только владельцу.");
    }
    const power = await setYandexPower(env, false);
    await addAudit(env, session.user.id, "power.off.force", {});
    return json({ ok: true, mode: "forced", power });
  }

  const activeOperation = await env.DB.prepare(
    "SELECT id,type FROM jobs WHERE status IN ('pending','claimed','running') ORDER BY created_at LIMIT 1",
  ).first();
  if (activeOperation) {
    throw new ApiError(409, "operation_locked", `Сначала завершите операцию ${activeOperation.type}; безопасное отключение питания не начато.`);
  }

  let command = await getActiveSafePowerOff(env);
  if (command) {
    await addAudit(env, session.user.id, "power.off.safe_already_requested", { command_id: command.id });
    return json({ ok: true, mode: "safe", command, already_pending: true }, 202);
  }

  try {
    command = await enqueueCommand(env, "safe_power_off", {}, session.user.id);
  } catch (error) {
    // The partial unique index protects two clicks that arrive at the same time.
    command = await getActiveSafePowerOff(env);
    if (!command) throw error;
    await addAudit(env, session.user.id, "power.off.safe_already_requested", { command_id: command.id });
    return json({ ok: true, mode: "safe", command, already_pending: true }, 202);
  }
  await addAudit(env, session.user.id, "power.off.safe_requested", { command_id: command.id });
  return json({ ok: true, mode: "safe", command }, 202);
}

async function queueShellCommand(request, env, session) {
  const body = await readJson(request);
  if (typeof body.command !== "string" || !body.command.trim() || body.command.length > MAX_SHELL_COMMAND_LENGTH) {
    throw new ApiError(400, "invalid_command", `Команда должна быть не длиннее ${MAX_SHELL_COMMAND_LENGTH} символов.`);
  }
  const command = await enqueueCommand(env, "shell_command", { command: body.command.trim() }, session.user.id);
  await addAudit(env, session.user.id, "server.command", { command_id: command.id });
  return json({ ok: true, command }, 202);
}

async function queueServerAction(request, env, session) {
  const body = await readJson(request);
  const allowed = new Set(["status", "reboot", "shutdown", "backup"]);
  if (!allowed.has(body.action)) {
    throw new ApiError(400, "invalid_action", "Недопустимое действие сервера.");
  }
  if (["reboot", "shutdown"].includes(body.action) && session.user.role !== "owner") {
    throw new ApiError(403, "owner_required", "Перезагрузка и выключение доступны только владельцу.");
  }
  const command = await enqueueCommand(env, `server_${body.action}`, {}, session.user.id);
  await addAudit(env, session.user.id, `server.${body.action}`, { command_id: command.id });
  return json({ ok: true, command }, 202);
}

async function queueMinecraftCommand(request, env, session) {
  const body = await readJson(request);
  if (typeof body.command !== "string" || !body.command.trim() || body.command.length > MAX_MINECRAFT_COMMAND_LENGTH) {
    throw new ApiError(400, "invalid_command", `Команда Minecraft должна быть не длиннее ${MAX_MINECRAFT_COMMAND_LENGTH} символов.`);
  }
  const command = await enqueueCommand(env, "minecraft_command", { command: body.command.trim() }, session.user.id);
  await addAudit(env, session.user.id, "minecraft.command", { command_id: command.id });
  return json({ ok: true, command }, 202);
}

async function queueMinecraftAction(request, env, session) {
  const body = await readJson(request);
  const allowed = new Set(["start", "stop", "restart", "status"]);
  if (!allowed.has(body.action)) {
    throw new ApiError(400, "invalid_action", "Недопустимое действие Minecraft.");
  }
  const command = await enqueueCommand(env, `minecraft_${body.action}`, {}, session.user.id);
  await addAudit(env, session.user.id, `minecraft.${body.action}`, { command_id: command.id });
  return json({ ok: true, command }, 202);
}

async function listUsers(env) {
  const result = await env.DB.prepare(
    `SELECT id, username, role, permissions, enabled, token_version, created_at, updated_at, last_login_at
     FROM users ORDER BY created_at ASC`,
  ).all();
  return json({ users: result.results.map((row) => publicUser(normalizeDbUser(row))) });
}

async function createUser(request, env, session) {
  const body = await readJson(request);
  const username = validateUsername(body.username);
  const password = validatePassword(body.password);
  const role = normalizeManagedRole(body.role, "user");
  const permissions = sanitizePermissions(body.permissions, ROLE_PERMISSIONS[role]);
  assertMayDelegateAccount(session, role, permissions);
  const passwordRecord = await createPasswordRecord(password);
  const now = Date.now();
  const user = {
    id: crypto.randomUUID(),
    username,
    role,
    permissions,
    enabled: 1,
    token_version: 1,
    created_at: now,
    updated_at: now,
    last_login_at: null,
  };

  try {
    await env.DB.prepare(
      `INSERT INTO users (
        id, username, password_salt, password_hash, password_iterations,
        role, permissions, enabled, token_version, created_at, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    )
      .bind(
        user.id,
        username,
        passwordRecord.salt,
        passwordRecord.hash,
        passwordRecord.iterations,
        role,
        JSON.stringify(permissions),
        1,
        1,
        now,
        now,
      )
      .run();
  } catch (error) {
    if (String(error.message || error).includes("UNIQUE")) {
      throw new ApiError(409, "username_taken", "Этот логин уже используется.");
    }
    throw error;
  }

  await addAudit(env, session.user.id, "user.create", { user_id: user.id, username, role });
  return json({ user: publicUser(user) }, 201);
}

async function updateUser(request, env, session, targetId) {
  const body = await readJson(request);
  const targetRow = await env.DB.prepare("SELECT * FROM users WHERE id = ?").bind(targetId).first();
  if (!targetRow) throw new ApiError(404, "user_not_found", "Пользователь не найден.");
  const target = normalizeDbUser(targetRow);
  if (target.role === "owner") {
    throw new ApiError(403, "owner_protected", "Аккаунт владельца нельзя изменить этим способом.");
  }
  assertMayManageAccount(session, target);

  const role = Object.prototype.hasOwnProperty.call(body, "role")
    ? normalizeManagedRole(body.role, target.role)
    : target.role;
  const permissions = Object.prototype.hasOwnProperty.call(body, "permissions")
    ? sanitizePermissions(body.permissions, ROLE_PERMISSIONS[role])
    : target.permissions;
  assertMayDelegateAccount(session, role, permissions);
  const enabled = Object.prototype.hasOwnProperty.call(body, "enabled") ? (body.enabled ? 1 : 0) : target.enabled ? 1 : 0;
  const now = Date.now();
  await env.DB.prepare(
    `UPDATE users
     SET role = ?, permissions = ?, enabled = ?, token_version = token_version + 1, updated_at = ?
     WHERE id = ?`,
  )
    .bind(role, JSON.stringify(permissions), enabled, now, targetId)
    .run();

  const updated = { ...target, role, permissions, enabled, token_version: target.token_version + 1, updated_at: now };
  await addAudit(env, session.user.id, enabled ? "user.update" : "user.disable", {
    user_id: targetId,
    username: target.username,
    role,
    permissions,
  });
  return json({ user: publicUser(updated) });
}

async function resetPassword(request, env, session, targetId) {
  const body = await readJson(request);
  const password = validatePassword(body.password);
  const targetRow = await env.DB.prepare("SELECT * FROM users WHERE id = ?").bind(targetId).first();
  if (!targetRow) throw new ApiError(404, "user_not_found", "Пользователь не найден.");
  const target = normalizeDbUser(targetRow);
  if (target.role === "owner" && target.id !== session.user.id) {
    throw new ApiError(403, "owner_protected", "Пароль владельца нельзя сбросить этим способом.");
  }
  if (target.id !== session.user.id) assertMayManageAccount(session, target);
  const passwordRecord = await createPasswordRecord(password);
  const now = Date.now();
  await env.DB.prepare(
    `UPDATE users
     SET password_salt = ?, password_hash = ?, password_iterations = ?, token_version = token_version + 1,
         failed_logins = 0, locked_until = NULL, updated_at = ?
     WHERE id = ?`,
  )
    .bind(passwordRecord.salt, passwordRecord.hash, passwordRecord.iterations, now, targetId)
    .run();
  await addAudit(env, session.user.id, "user.password_reset", { user_id: targetId, username: target.username });
  return json({ ok: true });
}

async function revokeUserSessions(env, session, targetId) {
  const targetRow = await env.DB.prepare("SELECT * FROM users WHERE id=?").bind(targetId).first();
  if (!targetRow) throw new ApiError(404, "user_not_found", "Пользователь не найден.");
  const target = normalizeDbUser(targetRow);
  if (target.role === "owner") throw new ApiError(403, "owner_protected", "Сеансы владельца отзываются только сменой собственного пароля.");
  assertMayManageAccount(session, target);
  await env.DB.prepare("UPDATE users SET token_version=token_version+1,updated_at=? WHERE id=?").bind(Date.now(), targetId).run();
  await addAudit(env, session.user.id, "user.sessions_revoke", { user_id: targetId, username: target.username });
  return json({ ok: true });
}

async function deleteUser(env, session, targetId) {
  const targetRow = await env.DB.prepare("SELECT * FROM users WHERE id=?").bind(targetId).first();
  if (!targetRow) throw new ApiError(404, "user_not_found", "Пользователь не найден.");
  const target = normalizeDbUser(targetRow);
  if (target.role === "owner" || target.id === session.user.id) throw new ApiError(403, "owner_protected", "Нельзя удалить владельца или текущую учётную запись.");
  assertMayManageAccount(session, target);
  await addAudit(env, session.user.id, "user.delete", { user_id: targetId, username: target.username });
  await env.DB.batch([
    env.DB.prepare("UPDATE command_queue SET requested_by=NULL WHERE requested_by=?").bind(targetId),
    env.DB.prepare("UPDATE jobs SET requested_by=NULL WHERE requested_by=?").bind(targetId),
    env.DB.prepare("UPDATE transfers SET requested_by=NULL WHERE requested_by=?").bind(targetId),
    env.DB.prepare("UPDATE settings SET updated_by=NULL WHERE updated_by=?").bind(targetId),
    env.DB.prepare("UPDATE audit_log SET actor_id=NULL WHERE actor_id=?").bind(targetId),
    env.DB.prepare("DELETE FROM users WHERE id=?").bind(targetId),
  ]);
  return json({ ok: true, deleted: targetId });
}

async function getConsoleEvents(env, kind, afterValue, latest = false) {
  const after = Math.max(0, Math.min(Number.parseInt(afterValue || "0", 10) || 0, Number.MAX_SAFE_INTEGER));
  if (latest && after === 0) {
    const result = await env.DB.prepare(
      `SELECT id, kind, message, created_at
       FROM (
         SELECT id, kind, message, created_at
         FROM console_events
         WHERE kind = ?
         ORDER BY id DESC
         LIMIT 100
       )
       ORDER BY id ASC`,
    )
      .bind(kind)
      .all();
    const events = result.results || [];
    return json({ events, next_after: events.length ? events[events.length - 1].id : after });
  }
  const result = await env.DB.prepare(
    "SELECT id, kind, message, created_at FROM console_events WHERE kind = ? AND id > ? ORDER BY id ASC LIMIT 100",
  )
    .bind(kind, after)
    .all();
  const events = result.results || [];
  return json({ events, next_after: events.length ? events[events.length - 1].id : after });
}

async function enqueueCommand(env, type, payload, requestedBy) {
  const command = {
    id: crypto.randomUUID(),
    type,
    payload,
    requested_by: requestedBy,
    status: "pending",
    created_at: Date.now(),
  };
  await env.DB.prepare(
    "INSERT INTO command_queue (id, type, payload, requested_by, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
  )
    .bind(command.id, command.type, JSON.stringify(command.payload), command.requested_by, command.status, command.created_at)
    .run();
  return command;
}

async function getActiveSafePowerOff(env) {
  return env.DB.prepare(
    "SELECT id, type, payload, requested_by, status, created_at, claimed_at FROM command_queue " +
      "WHERE type = 'safe_power_off' AND status IN ('pending', 'claimed') ORDER BY created_at DESC LIMIT 1",
  ).first();
}

async function claimAgentCommands(env) {
  return json(await claimAgentCommandsData(env));
}

async function claimAgentCommandsData(env) {
  const staleBefore = Date.now() - COMMAND_CLAIM_STALE_AFTER_MS;
  const result = await env.DB.prepare(
    `SELECT id, type, payload, requested_by, created_at
     FROM command_queue
     WHERE status = 'pending' OR (status = 'claimed' AND claimed_at < ?)
     ORDER BY created_at ASC LIMIT ?`,
  )
    .bind(staleBefore, AGENT_COMMAND_BATCH)
    .all();
  const commands = result.results || [];
  const claimedAt = Date.now();
  if (commands.length) {
    await env.DB.batch(
      commands.map((command) =>
        env.DB.prepare("UPDATE command_queue SET status = 'claimed', claimed_at = ? WHERE id = ?").bind(claimedAt, command.id),
      ),
    );
  }
  return {
    commands: commands.map((command) => ({ ...command, payload: safeJson(command.payload, {}) })),
    server_time: claimedAt,
  };
}

async function completeAgentCommand(request, env, commandId) {
  const body = await readJson(request);
  const status = body.status === "completed" ? "completed" : "failed";
  const resultPayload = isObject(body.result) ? body.result : { message: String(body.result || "") };
  const command = await env.DB.prepare("SELECT * FROM command_queue WHERE id = ?").bind(commandId).first();
  if (!command) throw new ApiError(404, "command_not_found", "Команда не найдена.");
  if (command.status === "completed" || command.status === "failed") {
    return json({ ok: true, already_finished: true });
  }

  if (status === "completed" && command.type === "safe_power_off" && resultPayload.ready_for_power_off === true) {
    try {
      resultPayload.power = await setYandexPower(env, false);
    } catch (error) {
      resultPayload.power_error = "Не удалось отключить умную розетку.";
      await addAudit(env, command.requested_by, "power.off.safe_failed", { command_id: commandId });
    }
  }

  const now = Date.now();
  await env.DB.prepare(
    "UPDATE command_queue SET status = ?, result = ?, completed_at = ? WHERE id = ?",
  )
    .bind(status, JSON.stringify(resultPayload), now, commandId)
    .run();
  await addAudit(env, command.requested_by, `agent.${command.type}.${status}`, { command_id: commandId });
  return json({ ok: true });
}

async function getYandexPowerStatus(env) {
  const cached = await getCachedPowerStatus(env);
  if (cached && Date.now() - cached.updated_at < POWER_STATUS_CACHE_MAX_AGE_MS) {
    return { ...cached, stale: false };
  }

  try {
    const fresh = await getLiveYandexPowerStatus(env);
    await saveCachedPowerStatus(env, fresh);
    return { ...fresh, stale: false };
  } catch (error) {
    if (!cached) throw error;
    console.warn("Yandex Smart Home is unavailable; returning cached power status");
    return { ...cached, stale: true };
  }
}

async function getLiveYandexPowerStatus(env) {
  const response = await yandexRequest(env, `devices/${encodeURIComponent(requiredSecret(env, "YANDEX_DEVICE_ID"))}`, {
    method: "GET",
  });
  const device = response.device || response;
  const capability = Array.isArray(device.capabilities)
    ? device.capabilities.find(
        (item) => item && item.type === "devices.capabilities.on_off" && item.state && item.state.instance === "on",
      )
    : null;
  return {
    name: device.name || "Питание сервера",
    device_id: requiredSecret(env, "YANDEX_DEVICE_ID"),
    on: capability ? Boolean(capability.state.value) : null,
    online: typeof device.state === "string" ? device.state === "online" : null,
    updated_at: Date.now(),
  };
}

async function getCachedPowerStatus(env) {
  const row = await env.DB.prepare(
    "SELECT name, on_state, online_state, updated_at FROM power_status WHERE id = 'primary'",
  ).first();
  if (!row) return null;
  return {
    name: row.name || "Питание сервера",
    device_id: requiredSecret(env, "YANDEX_DEVICE_ID"),
    on: row.on_state === null || row.on_state === undefined ? null : Boolean(row.on_state),
    online: row.online_state === null || row.online_state === undefined ? null : Boolean(row.online_state),
    updated_at: Number(row.updated_at) || 0,
  };
}

async function saveCachedPowerStatus(env, power) {
  const onState = power.on === true ? 1 : power.on === false ? 0 : null;
  const onlineState = power.online === true ? 1 : power.online === false ? 0 : null;
  await env.DB.prepare(
    `INSERT INTO power_status (id, name, on_state, online_state, updated_at) VALUES ('primary', ?, ?, ?, ?)
     ON CONFLICT(id) DO UPDATE SET name = excluded.name, on_state = excluded.on_state,
       online_state = excluded.online_state, updated_at = excluded.updated_at`,
  )
    .bind(String(power.name || "Питание сервера"), onState, onlineState, Number(power.updated_at) || Date.now())
    .run();
}

async function setYandexPower(env, on) {
  const deviceId = requiredSecret(env, "YANDEX_DEVICE_ID");
  const response = await yandexRequest(env, "devices/actions", {
    method: "POST",
    body: JSON.stringify({
      devices: [
        {
          id: deviceId,
          actions: [
            {
              type: "devices.capabilities.on_off",
              state: { instance: "on", value: on },
            },
          ],
        },
      ],
    }),
  });
  const deviceResult = Array.isArray(response.devices) ? response.devices.find((device) => device && device.id === deviceId) : null;
  const actionResult = Array.isArray(deviceResult?.capabilities)
    ? deviceResult.capabilities.find((capability) => capability?.type === "devices.capabilities.on_off")?.state?.action_result
    : null;
  if (actionResult && actionResult.status !== "DONE") {
    throw new ApiError(502, "smart_home_action_failed", "Умная розетка не подтвердила изменение состояния.");
  }
  const power = { on, accepted: true, response };
  try {
    await saveCachedPowerStatus(env, {
      name: "Питание сервера",
      on,
      online: true,
      updated_at: Date.now(),
    });
  } catch (error) {
    console.error("Could not cache Yandex power action", error);
  }
  return power;
}

async function yandexRequest(env, path, init) {
  const token = requiredSecret(env, "YANDEX_OAUTH_TOKEN");
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), YANDEX_REQUEST_TIMEOUT_MS);
  let response;
  try {
    response = await fetch(`https://api.iot.yandex.net/v1.0/${path}`, {
      ...init,
      signal: controller.signal,
      headers: {
        authorization: `Bearer ${token}`,
        "content-type": "application/json",
        accept: "application/json",
      },
    });
  } catch (error) {
    if (controller.signal.aborted) {
      throw new ApiError(504, "smart_home_timeout", "Умная розетка не ответила вовремя.");
    }
    console.error("Yandex Smart Home request failed", error);
    throw new ApiError(502, "smart_home_error", "Не удалось связаться с умной розеткой.");
  } finally {
    clearTimeout(timer);
  }
  const raw = await response.text();
  const payload = raw ? safeJson(raw, {}) : {};
  if (!response.ok) {
    console.error("Yandex Smart Home response", response.status, raw.slice(0, 512));
    throw new ApiError(502, "smart_home_error", "Не удалось связаться с умной розеткой.");
  }
  return payload;
}

async function requireSession(request, env) {
  const authorization = request.headers.get("authorization") || "";
  const match = authorization.match(/^Bearer\s+(.+)$/i);
  if (!match || match[1].length > 4096) throw new ApiError(401, "authentication_required", "Требуется вход в приложение.");
  const claims = await verifyJwt(match[1], requiredSecret(env, "JWT_SECRET"));
  if (!claims || !claims.sub || !claims.exp || Number(claims.exp) <= Math.floor(Date.now() / 1000)) {
    throw new ApiError(401, "invalid_session", "Сеанс истёк. Войдите снова.");
  }
  const row = await env.DB.prepare("SELECT * FROM users WHERE id = ?").bind(String(claims.sub)).first();
  if (!row || !Number(row.enabled) || Number(row.token_version) !== Number(claims.v)) {
    throw new ApiError(403, "access_revoked", "Доступ к программе отключён владельцем.");
  }
  return { user: normalizeDbUser(row), claims };
}

function requirePermission(session, permission) {
  if (!session.user.permissions.includes(permission)) {
    throw new ApiError(403, "permission_denied", "У вас нет прав для этого действия.");
  }
}

function requireAnyPermission(session, permissions) {
  if (!permissions.some((permission) => session.user.permissions.includes(permission))) {
    throw new ApiError(403, "permission_denied", "У вас нет прав для этого действия.");
  }
}

function requireAgent(request, env) {
  const received = request.headers.get("x-agent-key") || "";
  if (!constantTimeTextEqual(received, requiredSecret(env, "AGENT_API_KEY"))) {
    throw new ApiError(401, "invalid_agent", "Агент не авторизован.");
  }
}

function requiredSecret(env, name) {
  if (!env[name] || typeof env[name] !== "string") {
    throw new ApiError(503, "not_configured", `Сервис не настроен: отсутствует ${name}.`);
  }
  return env[name];
}

function normalizeDbUser(row) {
  const role = String(row.role);
  const storedPermissions = sanitizePermissions(safeJson(row.permissions, ROLE_PERMISSIONS.user), ROLE_PERMISSIONS.user);
  // 0.3.x accounts only contain underscore-style grants. Upgrade those in
  // memory during a rolling deployment. Once dotted granular permissions are
  // saved, the explicit set is preserved exactly.
  const legacyGrant = !storedPermissions.some((item) => item.includes("."));
  const roleDefaults = ROLE_PERMISSIONS[role] || ROLE_PERMISSIONS.user;
  const permissions = role === "owner" || legacyGrant
    ? [...new Set([...storedPermissions, ...roleDefaults])]
    : storedPermissions;
  return {
    ...row,
    id: String(row.id),
    username: String(row.username),
    role,
    permissions,
    enabled: Number(row.enabled) === 1,
    token_version: Number(row.token_version),
    created_at: Number(row.created_at),
    updated_at: Number(row.updated_at),
    last_login_at: row.last_login_at ? Number(row.last_login_at) : null,
  };
}

function publicUser(user) {
  return {
    id: user.id,
    username: user.username,
    role: user.role,
    permissions: user.permissions,
    enabled: Boolean(user.enabled),
    created_at: user.created_at,
    updated_at: user.updated_at,
    last_login_at: user.last_login_at || null,
  };
}

function validateUsername(value) {
  if (typeof value !== "string") throw new ApiError(400, "invalid_username", "Введите логин.");
  const username = value.trim();
  if (!/^[\p{L}\p{N}_.-]{3,32}$/u.test(username)) {
    throw new ApiError(400, "invalid_username", "Логин: от 3 до 32 букв, цифр, _, . или -.");
  }
  return username;
}

function validatePassword(value) {
  if (typeof value !== "string" || value.length < 12 || value.length > 128) {
    throw new ApiError(400, "weak_password", "Пароль должен содержать от 12 до 128 символов.");
  }
  return value;
}

function sanitizePermissions(value, fallback) {
  if (!Array.isArray(value)) return [...fallback];
  return [...new Set(value.filter((item) => typeof item === "string" && PERMISSIONS.has(item)))];
}

function normalizeManagedRole(value, fallback = "user") {
  if (value === undefined || value === null || value === "") return fallback;
  if (!['admin', 'user'].includes(value)) {
    throw new ApiError(400, "invalid_role", "Допустимы роли admin и user; набор Operator/File Manager/Viewer задаётся точными разрешениями.");
  }
  return value;
}

function assertMayDelegateAccount(session, role, permissions) {
  if (session.user.role === "owner") return;
  if (role !== "user") {
    throw new ApiError(403, "owner_required", "Только владелец может назначать администраторов.");
  }
  const actorPermissions = new Set(session.user.permissions);
  if (permissions.some((permission) => !actorPermissions.has(permission))) {
    throw new ApiError(403, "permission_escalation", "Нельзя выдать разрешение, которого нет у вашей учётной записи.");
  }
  if (permissions.some((permission) => ["user_manage", "users.manage"].includes(permission))) {
    throw new ApiError(403, "owner_required", "Делегировать управление пользователями может только владелец.");
  }
}

function assertMayManageAccount(session, target) {
  if (session.user.role === "owner") return;
  const targetPermissions = new Set(target.permissions || []);
  if (target.role !== "user" || targetPermissions.has("user_manage") || targetPermissions.has("users.manage")) {
    throw new ApiError(403, "owner_required", "Этой учётной записью может управлять только владелец.");
  }
  const actorPermissions = new Set(session.user.permissions);
  if ([...targetPermissions].some((permission) => !actorPermissions.has(permission))) {
    throw new ApiError(403, "permission_escalation", "Нельзя изменять учётную запись с более широкими правами.");
  }
}

async function createPasswordRecord(password) {
  const saltBytes = crypto.getRandomValues(new Uint8Array(16));
  const hashBytes = await derivePassword(password, saltBytes, PASSWORD_ITERATIONS);
  return {
    salt: bytesToBase64Url(saltBytes),
    hash: bytesToBase64Url(hashBytes),
    iterations: PASSWORD_ITERATIONS,
  };
}

async function verifyPassword(password, row) {
  if (typeof password !== "string") return false;
  const iterations = Math.max(1, Math.min(Number(row.password_iterations), 1_000_000));
  const calculated = await derivePassword(password, base64UrlToBytes(String(row.password_salt)), iterations);
  return constantTimeBytesEqual(calculated, base64UrlToBytes(String(row.password_hash)));
}

async function derivePassword(password, salt, iterations) {
  const key = await crypto.subtle.importKey("raw", encoder.encode(password), "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits(
    { name: "PBKDF2", salt, iterations, hash: "SHA-256" },
    key,
    256,
  );
  return new Uint8Array(bits);
}

async function issueAccessToken(env, user) {
  const now = Math.floor(Date.now() / 1000);
  return signJwt(
    {
      sub: user.id,
      usr: user.username,
      v: Number(user.token_version),
      iat: now,
      exp: now + ACCESS_TOKEN_TTL_SECONDS,
    },
    requiredSecret(env, "JWT_SECRET"),
  );
}

async function signJwt(payload, secret) {
  const header = bytesToBase64Url(encoder.encode(JSON.stringify({ alg: "HS256", typ: "JWT" })));
  const body = bytesToBase64Url(encoder.encode(JSON.stringify(payload)));
  const value = `${header}.${body}`;
  const signature = await hmac(value, secret);
  return `${value}.${bytesToBase64Url(signature)}`;
}

async function verifyJwt(token, secret) {
  try {
    const parts = token.split(".");
    if (parts.length !== 3) return null;
    const [headerPart, payloadPart, signaturePart] = parts;
    const header = safeJson(decoder.decode(base64UrlToBytes(headerPart)), null);
    if (!header || header.alg !== "HS256") return null;
    const expected = await hmac(`${headerPart}.${payloadPart}`, secret);
    if (!constantTimeBytesEqual(expected, base64UrlToBytes(signaturePart))) return null;
    return safeJson(decoder.decode(base64UrlToBytes(payloadPart)), null);
  } catch {
    return null;
  }
}

async function hmac(value, secret) {
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  return new Uint8Array(await crypto.subtle.sign("HMAC", key, encoder.encode(value)));
}

async function addAudit(env, actorId, action, details, options = {}) {
  const request = options.request;
  const ip = request?.headers?.get("cf-connecting-ip") || null;
  const device = request?.headers?.get("user-agent")?.slice(0, 200) || null;
  await env.DB.prepare(
    "INSERT INTO audit_log (actor_id, action, details, created_at, target, result, ip, device) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
  )
    .bind(actorId || null, action, JSON.stringify(details || {}).slice(0, 16_000), Date.now(), options.target || null, options.result || "success", ip, device)
    .run();
}

async function readJson(request, maxBytes = 32_768) {
  const contentLength = Number(request.headers.get("content-length") || "0");
  if (!Number.isFinite(contentLength) || contentLength < 0 || contentLength > maxBytes) {
    throw new ApiError(413, "payload_too_large", "Слишком большой запрос.");
  }
  try {
    if (!request.body) throw new Error("empty body");
    const reader = request.body.getReader();
    const chunks = [];
    let total = 0;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > maxBytes) {
        await reader.cancel();
        throw new ApiError(413, "payload_too_large", "Слишком большой запрос.");
      }
      chunks.push(value);
    }
    const bytes = new Uint8Array(total);
    let offset = 0;
    for (const chunk of chunks) {
      bytes.set(chunk, offset);
      offset += chunk.byteLength;
    }
    const body = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
    if (!isObject(body)) throw new Error("not an object");
    return body;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    throw new ApiError(400, "invalid_json", "Некорректный JSON-запрос.");
  }
}

function json(value, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });
}

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function safeJson(value, fallback) {
  try {
    return JSON.parse(value);
  } catch {
    return fallback;
  }
}

function bytesToBase64Url(bytes) {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function base64UrlToBytes(value) {
  if (typeof value !== "string" || !/^[A-Za-z0-9_-]*$/.test(value)) return new Uint8Array();
  const normalized = value.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (value.length % 4)) % 4);
  try {
    const binary = atob(normalized);
    return Uint8Array.from(binary, (character) => character.charCodeAt(0));
  } catch {
    return new Uint8Array();
  }
}

function constantTimeBytesEqual(left, right) {
  let difference = left.length ^ right.length;
  const length = Math.max(left.length, right.length);
  for (let index = 0; index < length; index += 1) {
    difference |= (left[index] || 0) ^ (right[index] || 0);
  }
  return difference === 0;
}

function constantTimeTextEqual(left, right) {
  return constantTimeBytesEqual(encoder.encode(left), encoder.encode(right));
}
