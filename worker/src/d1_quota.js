// Best-effort isolate-local circuit breaker; never stores credentials or uses D1.
const pausedUntil = new WeakMap();
export const D1_QUOTA_CODE = "d1_daily_quota_exceeded";

export function isD1DailyQuotaError(error) {
  const seen = new Set();
  for (let current = error; current && !seen.has(current); current = current.cause) {
    seen.add(current);
    const message = String(current.message || current);
    if (/D1/i.test(message) && /(?:daily.*(?:read|write).*limit|(?:read|write).*daily.*limit)/i.test(message)
        && /exceed|reached/i.test(message)) return true;
  }
  return false;
}

export function quotaRetrySeconds(db, now = Date.now()) {
  return Math.max(0, Math.ceil(((pausedUntil.get(db) || 0) - now) / 1000));
}

export function pauseForD1Quota(db, now = Date.now()) {
  const resetAt = (Math.floor(now / 86_400_000) + 1) * 86_400_000;
  // Probe periodically so an upgraded account can recover before midnight.
  pausedUntil.set(db, Math.min(now + 300_000, resetAt));
}

export function quotaResponse(db) {
  const retryAfter = Math.max(1, quotaRetrySeconds(db));
  return new Response(JSON.stringify({
    error: D1_QUOTA_CODE,
    message: "Исчерпан суточный лимит базы Cloudflare D1. Вход временно недоступен. Лимит сбрасывается в 00:00 UTC (03:00 по Москве).",
    retry_after: retryAfter,
  }), { status: 503, headers: {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
    "retry-after": String(retryAfter),
  } });
}
