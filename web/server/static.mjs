// @ts-check
// Minimal static file server for the built SPA (web/dist). No directory listings, no dotfiles, no path traversal.
import { createReadStream } from 'node:fs';
import { stat } from 'node:fs/promises';
import path from 'node:path';
import { pipeline } from 'node:stream';

const MIME = /** @type {Record<string, string>} */ ({
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.txt': 'text/plain; charset=utf-8',
  '.map': 'application/json; charset=utf-8',
  '.woff2': 'font/woff2',
});

/**
 * Resolve a request path to a file under `root`, or null when it would escape the root, hide a dotfile or contain NUL.
 * @param {string} root absolute directory
 * @param {string} rawPathname URL pathname (still percent-encoded)
 * @returns {string | null}
 */
export function resolveStaticPath(root, rawPathname) {
  let decoded;
  try {
    decoded = decodeURIComponent(rawPathname);
  } catch {
    return null;
  }
  if (decoded.includes('\0') || decoded.includes('\\')) return null;
  const segments = decoded.split('/').filter((s) => s !== '');
  if (segments.some((s) => s === '..' || s.startsWith('.'))) return null;
  const target = path.join(root, ...segments);
  const rel = path.relative(root, target);
  if (rel.startsWith('..') || path.isAbsolute(rel)) return null;
  return target;
}

/**
 * @param {string} root
 * @returns {(req: import('node:http').IncomingMessage, res: import('node:http').ServerResponse, pathname: string) => Promise<void>}
 */
export function createStaticHandler(root) {
  /** @param {string} p */
  async function fileInfo(p) {
    try {
      const s = await stat(p);
      return s.isFile() ? s : null;
    } catch {
      return null;
    }
  }

  return async function serve(req, res, pathname) {
    const head = req.method === 'HEAD';
    let file = resolveStaticPath(root, pathname);
    let info = file ? await fileInfo(file) : null;
    if (file && !info && !path.extname(pathname)) {
      // SPA fallback for extension-less paths only; a missing /assets/x.js must stay a 404, not an HTML document
      file = path.join(root, 'index.html');
      info = await fileInfo(file);
    }
    if (!file || !info) {
      const indexExists = await fileInfo(path.join(root, 'index.html'));
      const status = indexExists ? 404 : 503;
      const text = indexExists ? 'Not found' : 'The web client has not been built yet. Run "npm run build" in web/.';
      res.writeHead(status, { 'content-type': 'text/plain; charset=utf-8', 'cache-control': 'no-store' });
      res.end(head ? undefined : text);
      return;
    }
    const ext = path.extname(file).toLowerCase();
    const immutable = path.basename(path.dirname(file)) === 'assets' && /-[A-Za-z0-9_-]{6,}\.\w+$/.test(file);
    res.writeHead(200, {
      'content-type': MIME[ext] ?? 'application/octet-stream',
      'content-length': info.size,
      'cache-control': immutable ? 'public, max-age=31536000, immutable' : 'no-cache',
      'last-modified': info.mtime.toUTCString(),
    });
    if (head) {
      res.end();
      return;
    }
    await new Promise((resolve) => {
      pipeline(createReadStream(file), res, () => resolve(undefined));
    });
  };
}
