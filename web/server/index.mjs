// @ts-check
// Production entry:  node web/server/index.mjs   (after `npm run build` in web/)
// Required env: BFF_RAG_API_KEY. Common: RAG_API_URL, BFF_HOST, BFF_PORT. See web/README.md and DOCS/17_WEB_CLIENT.md.
import { createBff, defaultLogger } from './app.mjs';
import { ConfigError, loadConfig } from './config.mjs';

/** @type {import('./config.mjs').BffConfig} */
let config;
try {
  config = loadConfig(process.env);
} catch (e) {
  const message = e instanceof ConfigError ? e.message : 'invalid configuration';
  defaultLogger({ level: 'fatal', msg: 'startup failed', reason: message });
  process.exit(1);
}

const bff = createBff(config);
const { port, host } = await bff.listen();

const warnings = [];
if (config.upstreamOrigin.startsWith('http://') && !/^http:\/\/(127\.0\.0\.1|localhost|\[::1\])(:|$)/.test(config.upstreamOrigin)) {
  warnings.push('RAG_API_URL is plain http to a non-loopback host: the API key crosses the network unencrypted');
}
if (config.isProduction && !config.cookieSecure) warnings.push('NODE_ENV=production but BFF_COOKIE_SECURE is off: set it to 1 when served over HTTPS');
if (!['127.0.0.1', 'localhost', '::1'].includes(host) && config.trustedProxies === null) {
  warnings.push('listening on a non-loopback address without BFF_TRUSTED_PROXIES: per-IP limits see the proxy address if one is in front');
}

defaultLogger({
  level: 'info',
  msg: 'listening',
  host,
  port,
  upstream: config.upstreamOrigin, // origin only: RAG_API_URL cannot carry credentials (rejected at startup)
  cookie_secure: config.cookieSecure,
  rate_limit_per_min: config.ratePerMin,
  static_dir: config.staticDir,
  warnings: warnings.length > 0 ? warnings : undefined,
});

let stopping = false;
/** @param {string} signal */
function shutdown(signal) {
  if (stopping) return;
  stopping = true;
  defaultLogger({ level: 'info', msg: 'shutting down', signal });
  const force = setTimeout(() => process.exit(1), 10_000);
  force.unref();
  void bff.close().then(() => process.exit(0));
}
process.on('SIGINT', () => shutdown('SIGINT'));
process.on('SIGTERM', () => shutdown('SIGTERM'));
