import assert from "node:assert/strict";
import test from "node:test";
import { gzipSync } from "node:zlib";
import worker from "../worker/src/index.js";
import {
  AGENT_ONLINE_MAX_AGE_MS,
  boundedJobResult,
  completeMultipart,
  filterEventsForSession,
  filterStatusForSession,
  normalizeEvent,
  normalizeFilename,
  normalizeJobPayload,
  normalizeRelativePath,
  redactJobValue,
  routeSync,
} from "../worker/src/control_plane.js";

class TestApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

class FakeStatement {
  constructor(db, sql) {
    this.db = db;
    this.sql = sql.replace(/\s+/g, " ").trim();
    this.args = [];
  }

  bind(...args) {
    this.args = args;
    return this;
  }

  async first() {
    return this.db.execute(this.sql, this.args, "first");
  }

  async run() {
    return this.db.execute(this.sql, this.args, "run");
  }

  async all() {
    return this.db.execute(this.sql, this.args, "all");
  }
}

class FakeD1 {
  constructor() {
    this.users = [];
    this.audit = [];
    this.commands = [];
    this.powerStatus = null;
    this.agentStatus = null;
  }

  prepare(sql) {
    return new FakeStatement(this, sql);
  }

  async batch(statements) {
    return Promise.all(statements.map((statement) => statement.run()));
  }

  execute(sql, args, mode) {
    if (sql.startsWith("SELECT COUNT(*) AS count FROM users")) return { count: this.users.length };
    if (sql.startsWith("SELECT * FROM users WHERE username")) {
      const username = String(args[0]).toLowerCase();
      return this.users.find((user) => user.username.toLowerCase() === username) ?? null;
    }
    if (sql.startsWith("SELECT * FROM users WHERE id")) {
      return this.users.find((user) => user.id === args[0]) ?? null;
    }
    if (sql.startsWith("SELECT id, username, role FROM users WHERE id")) {
      const user = this.users.find((item) => item.id === args[0]);
      return user ? { id: user.id, username: user.username, role: user.role } : null;
    }
    if (sql.startsWith("INSERT INTO users")) {
      const [
        id,
        username,
        password_salt,
        password_hash,
        password_iterations,
        role,
        permissions,
        enabled,
        token_version,
        created_at,
        updated_at,
      ] = args;
      if (this.users.some((item) => item.username.toLowerCase() === String(username).toLowerCase())) {
        throw new Error("UNIQUE constraint failed");
      }
      this.users.push({
        id,
        username,
        password_salt,
        password_hash,
        password_iterations,
        role,
        permissions,
        enabled,
        token_version,
        created_at,
        updated_at,
        failed_logins: 0,
        locked_until: null,
        last_login_at: null,
      });
      return { success: true };
    }
    if (sql.startsWith("UPDATE users SET failed_logins = 0")) {
      const [last_login_at, updated_at, id] = args;
      const user = this.users.find((item) => item.id === id);
      user.failed_logins = 0;
      user.locked_until = null;
      user.last_login_at = last_login_at;
      user.updated_at = updated_at;
      return { success: true };
    }
    if (sql.startsWith("UPDATE users SET role = ?")) {
      const [role, permissions, enabled, updated_at, id] = args;
      const user = this.users.find((item) => item.id === id);
      user.role = role;
      user.permissions = permissions;
      user.enabled = enabled;
      user.token_version += 1;
      user.updated_at = updated_at;
      return { success: true };
    }
    if (sql.startsWith("INSERT INTO audit_log")) {
      this.audit.push({ actor_id: args[0], action: args[1] });
      return { success: true };
    }
    if (sql.startsWith("SELECT id, type, payload, requested_by, status, created_at, claimed_at FROM command_queue")) {
      return [...this.commands]
        .reverse()
        .find((command) => command.type === "safe_power_off" && ["pending", "claimed"].includes(command.status)) ?? null;
    }
    if (sql.startsWith("SELECT id,type FROM jobs WHERE status IN")) return null;
    if (sql.startsWith("SELECT id, type, payload, requested_by, created_at FROM command_queue")) return { results: [] };
    if (sql.startsWith("SELECT * FROM jobs WHERE status='pending'")) return { results: [] };
    if (sql.startsWith("SELECT id FROM jobs WHERE cancel_requested=1")) return { results: [] };
    if (sql.startsWith("INSERT INTO command_queue")) {
      const [id, type, payload, requested_by, status, created_at] = args;
      if (type === "safe_power_off" && this.commands.some(
        (command) => command.type === "safe_power_off" && ["pending", "claimed"].includes(command.status),
      )) {
        throw new Error("UNIQUE constraint failed: idx_command_queue_one_active_safe_power_off");
      }
      this.commands.push({ id, type, payload, requested_by, status, created_at, claimed_at: null });
      return { success: true };
    }
    if (sql.startsWith("SELECT name, on_state, online_state, updated_at FROM power_status")) {
      return this.powerStatus;
    }
    if (sql.startsWith("SELECT status, updated_at FROM agent_status")) return this.agentStatus;
    if (sql.startsWith("INSERT INTO power_status")) {
      const [name, on_state, online_state, updated_at] = args;
      this.powerStatus = { name, on_state, online_state, updated_at };
      return { success: true };
    }
    throw new Error(`FakeD1 does not implement: ${sql}`);
  }
}

async function call(env, method, path, body, token, extraHeaders = {}) {
  const headers = { "content-type": "application/json", ...extraHeaders };
  if (token) headers.authorization = `Bearer ${token}`;
  const request = new Request(`https://control.example${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const response = await worker.fetch(request, env);
  return { response, json: await response.json() };
}

test("gzip JSON requests are decompressed with the normal size guard", async () => {
  const env = {
    DB: new FakeD1(), JWT_SECRET: "x".repeat(48), BOOTSTRAP_KEY: "bootstrap-secret",
    AGENT_API_KEY: "agent-secret", YANDEX_OAUTH_TOKEN: "not-used", YANDEX_DEVICE_ID: "not-used",
  };
  const body = gzipSync(Buffer.from(JSON.stringify({
    username: "compressed-owner", password: "a secure compressed password",
  })));
  const response = await worker.fetch(new Request("https://control.example/v1/setup", {
    method: "POST",
    headers: {
      "content-type": "application/json", "content-encoding": "gzip", "x-bootstrap-key": "bootstrap-secret",
    },
    body,
  }), env);
  assert.equal(response.status, 201);
  assert.equal((await response.json()).user.username, "compressed-owner");
});

test("disabled user loses access even with an already issued token", async () => {
  const env = {
    DB: new FakeD1(),
    JWT_SECRET: "x".repeat(48),
    BOOTSTRAP_KEY: "bootstrap-secret",
    AGENT_API_KEY: "agent-secret",
    YANDEX_OAUTH_TOKEN: "not-used",
    YANDEX_DEVICE_ID: "not-used",
  };

  const ownerSetup = await call(
    env,
    "POST",
    "/v1/setup",
    { username: "owner", password: "a secure owner password" },
    undefined,
    { "x-bootstrap-key": "bootstrap-secret" },
  );
  assert.equal(ownerSetup.response.status, 201);
  const ownerToken = ownerSetup.json.token;

  const created = await call(
    env,
    "POST",
    "/v1/admin/users",
    { username: "player", password: "a secure player password", role: "user" },
    ownerToken,
  );
  assert.equal(created.response.status, 201);
  const playerId = created.json.user.id;

  const playerLogin = await call(
    env,
    "POST",
    "/v1/login",
    { username: "player", password: "a secure player password" },
  );
  assert.equal(playerLogin.response.status, 200);
  const playerToken = playerLogin.json.token;

  const disabled = await call(env, "PATCH", `/v1/admin/users/${playerId}`, { enabled: false }, ownerToken);
  assert.equal(disabled.response.status, 200);

  const blocked = await call(env, "GET", "/v1/me", undefined, playerToken);
  assert.equal(blocked.response.status, 403);
  assert.equal(blocked.json.error, "access_revoked");
});

test("a repeated safe power-off request reuses the active command", async () => {
  const env = {
    DB: new FakeD1(),
    JWT_SECRET: "x".repeat(48),
    BOOTSTRAP_KEY: "bootstrap-secret",
    AGENT_API_KEY: "agent-secret",
    YANDEX_OAUTH_TOKEN: "not-used",
    YANDEX_DEVICE_ID: "not-used",
  };

  const ownerSetup = await call(
    env,
    "POST",
    "/v1/setup",
    { username: "owner", password: "a secure owner password" },
    undefined,
    { "x-bootstrap-key": "bootstrap-secret" },
  );
  const ownerToken = ownerSetup.json.token;

  const first = await call(env, "POST", "/v1/power/action", { state: "off" }, ownerToken);
  const second = await call(env, "POST", "/v1/power/action", { state: "off" }, ownerToken);

  assert.equal(first.response.status, 202);
  assert.equal(second.response.status, 202);
  assert.equal(second.json.already_pending, true);
  assert.equal(second.json.command.id, first.json.command.id);
  assert.equal(env.DB.commands.length, 1);
});

test("power status is cached and remains visible during a Yandex timeout", async () => {
  const env = {
    DB: new FakeD1(),
    JWT_SECRET: "x".repeat(48),
    BOOTSTRAP_KEY: "bootstrap-secret",
    AGENT_API_KEY: "agent-secret",
    YANDEX_OAUTH_TOKEN: "token",
    YANDEX_DEVICE_ID: "device-id",
  };
  const originalFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    return new Response(JSON.stringify({
      device: {
        name: "Питание сервера",
        state: "online",
        capabilities: [{ type: "devices.capabilities.on_off", state: { instance: "on", value: true } }],
      },
    }), { status: 200 });
  };

  try {
    const ownerSetup = await call(
      env,
      "POST",
      "/v1/setup",
      { username: "owner", password: "a secure owner password" },
      undefined,
      { "x-bootstrap-key": "bootstrap-secret" },
    );
    const token = ownerSetup.json.token;

    const first = await call(env, "GET", "/v1/power/status", undefined, token);
    const second = await call(env, "GET", "/v1/power/status", undefined, token);
    assert.equal(first.response.status, 200);
    assert.equal(second.response.status, 200);
    assert.equal(first.json.power.on, true);
    assert.equal(calls, 1);

    env.DB.powerStatus.updated_at = 0;
    globalThis.fetch = async () => { throw new Error("network unavailable"); };
    const stale = await call(env, "GET", "/v1/power/status", undefined, token);
    assert.equal(stale.response.status, 200);
    assert.equal(stale.json.power.on, true);
    assert.equal(stale.json.power.stale, true);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("control-plane path and job validation reject traversal and unsafe services", () => {
  assert.equal(normalizeRelativePath("config/server.properties", TestApiError), "config/server.properties");
  assert.equal(normalizeFilename("server.properties", TestApiError), "server.properties");
  assert.throws(() => normalizeRelativePath("../../etc/passwd", TestApiError), (error) => error.code === "invalid_path");
  assert.throws(() => normalizeRelativePath("C:\\Windows\\win.ini", TestApiError), (error) => error.code === "invalid_path");
  assert.throws(() => normalizeFilename("../secret", TestApiError), (error) => error.code === "invalid_filename");
  assert.throws(
    () => normalizeJobPayload("service_action", { service: "../../ssh.service", action: "restart" }, TestApiError),
    (error) => error.code === "invalid_service",
  );
  const command = normalizeJobPayload("minecraft_command", { instance_id: "pack-1", command: "/list" }, TestApiError);
  assert.equal(command.command, "list");
});

test("zero-byte transfers complete without an impossible multipart upload", async () => {
  const updates = [];
  const puts = [];
  const env = {
    DB: {
      prepare(sql) {
        return {
          bind(...args) {
            return { async run() { updates.push({ sql, args }); return { success: true }; } };
          },
        };
      },
    },
    FILES: {
      async put(key, data, options) { puts.push({ key, size: data.byteLength, options }); },
    },
  };
  const result = await completeMultipart(
    env,
    { id: "empty", direction: "upload", size_bytes: 0, status: "created", expires_at: Date.now() + 60_000, object_key: "transfers/empty/file", sha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855" },
    "upload",
    { ApiError: TestApiError, json: (value, status = 200) => ({ value, status }) },
  );
  assert.equal(result.status, 200);
  assert.equal(result.value.transfer.status, "ready");
  assert.deepEqual(puts.map((item) => [item.key, item.size]), [["transfers/empty/file", 0]]);
  assert.equal(updates.length, 1);
});

test("event normalization bounds text and preserves useful severity", () => {
  const event = normalizeEvent({ kind: "minecraft", instance_id: "pack", source: "forge", message: ` ERROR ${"x".repeat(9000)}` });
  assert.equal(event.kind, "minecraft");
  assert.equal(event.instance_id, "pack");
  assert.equal(event.level, "ERROR");
  assert.equal(event.message.length, 8000);
  assert.equal(normalizeEvent({ message: "   " }), null);
});

test("realtime sync data is filtered by server-side permissions", () => {
  const status = {
    protocol_version: 2,
    agent_version: "2.0.0",
    server: { hostname: "private-host" },
    storage: { mounts: [{ mountpoint: "/" }] },
    processes: [{ command: "secret argument" }],
    minecraft: { id: "pack" },
    instances: [{ id: "pack" }],
    backups: [{ id: "backup" }],
  };
  const minecraftOnly = { user: { permissions: ["minecraft.view"] } };
  const visible = filterStatusForSession(status, minecraftOnly);
  assert.equal(visible.minecraft.id, "pack");
  assert.equal(visible.server, undefined);
  assert.equal(visible.processes, undefined);
  assert.equal(visible.backups, undefined);
  const events = filterEventsForSession(
    [{ kind: "server", message: "system" }, { kind: "minecraft", message: "game" }, { kind: "audit", message: "admin" }],
    minecraftOnly,
  );
  assert.deepEqual(events.map((event) => event.message), ["game"]);
});

test("realtime sync batches D1 reads and keeps a 15-second heartbeat online", async () => {
  const prepared = [];
  let batchCalls = 0;
  const statusUpdatedAt = Date.now() - 20_000;
  const env = {
    DB: {
      prepare(sql) {
        const statement = {
          sql: sql.replace(/\s+/g, " ").trim(),
          args: [],
          bind(...args) { this.args = args; return this; },
        };
        prepared.push(statement);
        return statement;
      },
      async batch(statements) {
        batchCalls += 1;
        assert.equal(statements.length, 5);
        return [
          { results: [{ status: JSON.stringify({ protocol_version: 2, agent_version: "2.0.4", server: { hostname: "server" } }), updated_at: statusUpdatedAt }] },
          { results: [] },
          { results: [{ id: 5, kind: "server", message: "ready", created_at: statusUpdatedAt, instance_id: null, source: "agent", level: "INFO" }] },
          { results: [{
            id: "job-1", type: "log_read", payload: "{}", requested_by: "owner", instance_id: "pack",
            status: "completed", progress: 100, stage: "done", message: "done",
            result: '{"truncated":true}', error_code: null, lock_key: null, cancel_requested: 0,
            created_at: 1, started_at: 2, completed_at: 3, updated_at: 4,
          }] },
          { results: [] },
        ];
      },
    },
  };
  const payload = await routeSync(
    new Request("https://control.example/v1/sync"),
    env,
    new URL("https://control.example/v1/sync?after=0&jobs_since=0&notification_after=0&status_after=0"),
    { user: { id: "owner", permissions: ["server.view", "logs.view"] } },
    {
      safeJson(value, fallback) { try { return JSON.parse(value); } catch { return fallback; } },
      json(value) { return value; },
    },
  );

  assert.equal(batchCalls, 1);
  assert.equal(prepared.length, 5);
  assert.equal(payload.server.online, true);
  assert.equal(payload.server.status.server.hostname, "server");
  assert.equal(payload.events[0].message, "ready");
  assert.equal(payload.jobs[0].result.truncated, true);
  assert.ok(payload.sync_ms >= 0);
  assert.equal(AGENT_ONLINE_MAX_AGE_MS, 45_000);
});

test("job summaries redact commands, file contents, credentials, and oversized results", () => {
  const redacted = redactJobValue({
    instance_id: "pack",
    command: "op private-player",
    content: "secret configuration",
    nested: { api_key: "do-not-leak", ordinary: "visible" },
  });
  assert.equal(redacted.instance_id, "pack");
  assert.equal(redacted.command, "[скрыто]");
  assert.equal(redacted.content, "[скрыто]");
  assert.equal(redacted.nested.api_key, "[скрыто]");
  assert.equal(redacted.nested.ordinary, "visible");
  const bounded = JSON.parse(boundedJobResult({ content: "x".repeat(300_000) }));
  assert.equal(bounded.truncated, true);
});

test("legacy status endpoint also enforces dotted granular permissions", async () => {
  const env = {
    DB: new FakeD1(),
    JWT_SECRET: "x".repeat(48),
    BOOTSTRAP_KEY: "bootstrap-secret",
    AGENT_API_KEY: "agent-secret",
  };
  const setup = await call(
    env,
    "POST",
    "/v1/setup",
    { username: "owner", password: "a secure owner password" },
    undefined,
    { "x-bootstrap-key": "bootstrap-secret" },
  );
  const created = await call(
    env,
    "POST",
    "/v1/admin/users",
    { username: "viewer", password: "a secure viewer password", role: "user", permissions: ["minecraft.view"] },
    setup.json.token,
  );
  assert.equal(created.response.status, 201);
  const login = await call(env, "POST", "/v1/login", { username: "viewer", password: "a secure viewer password" });
  env.DB.agentStatus = {
    status: JSON.stringify({
      protocol_version: 2,
      server: { hostname: "private-host" },
      processes: [{ command: "private command line" }],
      minecraft: { id: "pack", state: "RUNNING" },
      instances: [{ id: "pack", state: "RUNNING" }],
    }),
    updated_at: Date.now(),
  };
  const status = await call(env, "GET", "/v1/server/status", undefined, login.json.token);
  assert.equal(status.response.status, 200);
  assert.equal(status.json.status.minecraft.id, "pack");
  assert.equal(status.json.status.server, undefined);
  assert.equal(status.json.status.processes, undefined);
});

test("malformed tokens and oversized JSON are rejected without reaching D1", async () => {
  const env = {
    DB: new FakeD1(),
    JWT_SECRET: "x".repeat(48),
    BOOTSTRAP_KEY: "bootstrap-secret",
    AGENT_API_KEY: "agent-secret",
  };
  const malformed = await call(env, "GET", "/v1/me", undefined, "not-a-jwt");
  assert.equal(malformed.response.status, 401);
  const request = new Request("https://control.example/v1/login", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: `{"username":"${"x".repeat(40_000)}"}`,
  });
  const response = await worker.fetch(request, env);
  assert.equal(response.status, 413);
  assert.equal((await response.json()).error, "payload_too_large");
});

test("agent sync combines commands and jobs into one polling response", async () => {
  const env = {
    DB: new FakeD1(),
    JWT_SECRET: "x".repeat(48),
    BOOTSTRAP_KEY: "bootstrap-secret",
    AGENT_API_KEY: "agent-secret",
  };
  const request = new Request("https://control.example/v1/agent/sync", {
    headers: { "x-agent-key": "agent-secret" },
  });
  const response = await worker.fetch(request, env);
  const body = await response.json();
  assert.equal(response.status, 200);
  assert.deepEqual(body.commands, []);
  assert.deepEqual(body.jobs, []);
  assert.deepEqual(body.cancel, []);
});

test("delegated user management cannot escalate roles or permissions", async () => {
  const env = {
    DB: new FakeD1(),
    JWT_SECRET: "x".repeat(48),
    BOOTSTRAP_KEY: "bootstrap-secret",
    AGENT_API_KEY: "agent-secret",
  };
  const setup = await call(
    env,
    "POST",
    "/v1/setup",
    { username: "owner", password: "a secure owner password" },
    undefined,
    { "x-bootstrap-key": "bootstrap-secret" },
  );
  const managerCreated = await call(
    env,
    "POST",
    "/v1/admin/users",
    {
      username: "manager",
      password: "a secure manager password",
      role: "user",
      permissions: ["users.manage", "server.view"],
    },
    setup.json.token,
  );
  assert.equal(managerCreated.response.status, 201);
  const login = await call(env, "POST", "/v1/login", { username: "manager", password: "a secure manager password" });
  assert.equal(login.response.status, 200);

  const admin = await call(
    env,
    "POST",
    "/v1/admin/users",
    { username: "newadmin", password: "a secure admin password", role: "admin", permissions: ["server.view"] },
    login.json.token,
  );
  assert.equal(admin.response.status, 403);
  assert.equal(admin.json.error, "owner_required");

  const escalated = await call(
    env,
    "POST",
    "/v1/admin/users",
    { username: "poweruser", password: "a secure power password", role: "user", permissions: ["server.power"] },
    login.json.token,
  );
  assert.equal(escalated.response.status, 403);
  assert.equal(escalated.json.error, "permission_escalation");

  const allowed = await call(
    env,
    "POST",
    "/v1/admin/users",
    { username: "viewer", password: "a secure viewer password", role: "user", permissions: ["server.view"] },
    login.json.token,
  );
  assert.equal(allowed.response.status, 201);
});
