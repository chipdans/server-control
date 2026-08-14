import assert from "node:assert/strict";
import test from "node:test";
import worker from "../worker/src/index.js";

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
