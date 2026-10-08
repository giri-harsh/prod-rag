// @ts-check
// Environment -> validated config. Error messages name the variable, never print its value (BFF_RAG_API_KEY is a secret).
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseTrustedProxies } from './lib.mjs';

export class ConfigError extends Error {}

const HERE = path.dirname(fileURLToPath(import.meta.url));

/**
 * @param {Record<string, string | undefined>} env
 * @param {string} name
 * @param {number} dflt
 * @param {{ min: number, max: number }} range
 */
function int(env, name, dflt, range) {
  const raw = env[name];
  if (raw === undefined || raw.trim() === '') return dflt;
  const n = Number(raw);
  if (!Number.isInteger(n) || n < range.min || n > range.max) {
    throw new ConfigError(`${name} must be an integer between ${range.min} and ${range.max}`);
  }
  return n;
}

/** @param {Record<string, string | undefined>} env @param {string} name */
function flag(env, name) {
  const raw = env[name]?.trim().toLowerCase();
  return raw === '1' || raw === 'true' || raw === 'yes';
}

/**
 * @param {Record<string, string | undefined>} env usually process.env
 */
export function loadConfig(env) {
  const apiKey = (env['BFF_RAG_API_KEY'] ?? '').trim();
  if (apiKey === '') throw new ConfigError('BFF_RAG_API_KEY is required (the API key the BFF sends as X-API-Key; it never reaches the browser)');
  if (!/^[\x21-\x7e]+$/.test(apiKey)) throw new ConfigError('BFF_RAG_API_KEY contains characters that are not valid in an HTTP header (whitespace or non-ASCII)');

  const rawUrl = (env['RAG_API_URL'] ?? 'http://127.0.0.1:8080').trim();
  let upstream;
  try {
    upstream = new URL(rawUrl);
  } catch {
    throw new ConfigError('RAG_API_URL is not a valid URL');
  }
  if (upstream.protocol !== 'http:' && upstream.protocol !== 'https:') throw new ConfigError('RAG_API_URL must use http or https');
  if (upstream.username !== '' || upstream.password !== '') throw new ConfigError('RAG_API_URL must not contain credentials');
  if (upstream.search !== '' || upstream.hash !== '') throw new ConfigError('RAG_API_URL must not contain a query string or fragment');

  const portRange = { min: 0, max: 65535 };
  const port = int(env, 'BFF_PORT', int(env, 'PORT', 8787, portRange), portRange); // PORT: platform convention (Cloud Run, Heroku)
  const allowedOrigins = new Set(
    (env['BFF_ALLOWED_ORIGINS'] ?? '').split(',').map((s) => s.trim().replace(/\/+$/, '')).filter(Boolean),
  );
  for (const o of allowedOrigins) {
    let ok = false;
    try {
      const u = new URL(o);
      ok = (u.protocol === 'http:' || u.protocol === 'https:') && u.origin === o;
    } catch {
      /* invalid */
    }
    if (!ok) throw new ConfigError(`BFF_ALLOWED_ORIGINS entry "${o}" must be an origin such as https://chat.example.com`);
  }

  let trustedProxies;
  try {
    trustedProxies = parseTrustedProxies(env['BFF_TRUSTED_PROXIES'] ?? '');
  } catch (e) {
    throw new ConfigError(e instanceof Error ? e.message : 'BFF_TRUSTED_PROXIES is invalid');
  }

  return Object.freeze({
    apiKey,
    upstreamBase: upstream.href.replace(/\/+$/, ''),
    upstreamOrigin: upstream.origin,
    host: (env['BFF_HOST'] ?? '127.0.0.1').trim() || '127.0.0.1',
    port,
    staticDir: path.resolve(env['BFF_STATIC_DIR']?.trim() || path.join(HERE, '..', 'dist')),
    maxBodyBytes: int(env, 'BFF_MAX_BODY_BYTES', 32 * 1024, { min: 1024, max: 1024 * 1024 }),
    maxQueryChars: int(env, 'BFF_MAX_QUERY_CHARS', 4000, { min: 1, max: 100_000 }),
    ratePerMin: int(env, 'BFF_RATE_LIMIT_PER_MIN', 30, { min: 1, max: 100_000 }),
    healthRatePerMin: int(env, 'BFF_HEALTH_RATE_LIMIT_PER_MIN', 120, { min: 1, max: 100_000 }),
    upstreamTimeoutMs: int(env, 'BFF_UPSTREAM_TIMEOUT_MS', 65_000, { min: 1000, max: 300_000 }),
    cookieSecure: flag(env, 'BFF_COOKIE_SECURE'),
    allowedOrigins,
    trustedProxies,
    isProduction: env['NODE_ENV'] === 'production',
  });
}

/** @typedef {ReturnType<typeof loadConfig>} BffConfig */
