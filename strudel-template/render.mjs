#!/usr/bin/env node
/**
 * Headless Strudel → WAV (then optional MP3 via ffmpeg in Python wrapper).
 *
 * Usage:
 *   node render.mjs --code song.js --out out.wav [--cycles 8] [--cps 0.5] [--sample-rate 44100]
 *   node render.mjs --code-stdin --out out.wav
 */
import fs from 'fs';
import path from 'path';
import http from 'http';
import os from 'os';
import { fileURLToPath } from 'url';
import { createRequire } from 'module';
import puppeteer from 'puppeteer-core';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PUBLIC_DIR = path.join(__dirname, 'public');
const require = createRequire(import.meta.url);

function parseArgs(argv) {
  const opts = {
    code: null,
    codeFile: null,
    codeStdin: false,
    out: null,
    cycles: null,
    durationSec: 16,
    cps: null,
    sampleRate: 44100,
    chrome: process.env.CHROME_BIN || process.env.REMOTION_BROWSER_EXECUTABLE || null,
    timeoutMs: Number(process.env.STRUDEL_RENDER_TIMEOUT_MS || 180000),
  };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--code') opts.codeFile = argv[++i];
    else if (a === '--code-stdin') opts.codeStdin = true;
    else if (a === '--out') opts.out = argv[++i];
    else if (a === '--cycles') opts.cycles = Number(argv[++i]);
    else if (a === '--duration') opts.durationSec = Number(argv[++i]);
    else if (a === '--cps') opts.cps = Number(argv[++i]);
    else if (a === '--sample-rate') opts.sampleRate = Number(argv[++i]);
    else if (a === '--chrome') opts.chrome = argv[++i];
    else if (a === '--timeout-ms') opts.timeoutMs = Number(argv[++i]);
    else if (a === '--help' || a === '-h') {
      console.log(`Usage: node render.mjs --code file.js --out out.wav [options]`);
      process.exit(0);
    }
  }
  return opts;
}

function defaultChromeCandidates() {
  return [
    process.env.CHROME_BIN,
    process.env.REMOTION_BROWSER_EXECUTABLE,
    path.join(
      __dirname,
      '..',
      'remotion-template/node_modules/.remotion/chrome-headless-shell/linux64',
      'chrome-headless-shell-linux64/chrome-headless-shell',
    ),
    path.join(os.homedir(), '.local/bin/chromium'),
    '/usr/bin/chromium',
    '/usr/bin/google-chrome',
    '/usr/bin/google-chrome-stable',
  ].filter(Boolean);
}

function resolveChrome(explicit) {
  const list = explicit ? [explicit, ...defaultChromeCandidates()] : defaultChromeCandidates();
  for (const p of list) {
    if (p && fs.existsSync(p) && !String(p).includes('/snap/')) return p;
  }
  return null;
}

function contentType(filePath) {
  if (filePath.endsWith('.html')) return 'text/html; charset=utf-8';
  if (filePath.endsWith('.js')) return 'application/javascript; charset=utf-8';
  if (filePath.endsWith('.css')) return 'text/css; charset=utf-8';
  if (filePath.endsWith('.wasm')) return 'application/wasm';
  if (filePath.endsWith('.json')) return 'application/json';
  if (filePath.endsWith('.map')) return 'application/json';
  return 'application/octet-stream';
}

function startStaticServer(rootDir) {
  return new Promise((resolve, reject) => {
    const server = http.createServer((req, res) => {
      try {
        const urlPath = decodeURIComponent((req.url || '/').split('?')[0]);
        let rel = urlPath === '/' ? '/index.html' : urlPath;
        // Prevent path escape
        rel = path.normalize(rel).replace(/^(\.\.[/\\])+/, '');
        const filePath = path.join(rootDir, rel);
        if (!filePath.startsWith(rootDir)) {
          res.writeHead(403);
          res.end('forbidden');
          return;
        }
        if (!fs.existsSync(filePath) || fs.statSync(filePath).isDirectory()) {
          res.writeHead(404);
          res.end('not found');
          return;
        }
        const data = fs.readFileSync(filePath);
        res.writeHead(200, {
          'Content-Type': contentType(filePath),
          'Cache-Control': 'no-store',
          // Allow AudioWorklet blob/module loads
          'Cross-Origin-Opener-Policy': 'same-origin',
          'Cross-Origin-Embedder-Policy': 'require-corp',
        });
        res.end(data);
      } catch (err) {
        res.writeHead(500);
        res.end(String(err?.message || err));
      }
    });
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      resolve({ server, port, url: `http://127.0.0.1:${port}/` });
    });
    server.on('error', reject);
  });
}

async function readCode(opts) {
  if (opts.codeStdin) {
    return fs.readFileSync(0, 'utf8');
  }
  if (opts.codeFile) {
    return fs.readFileSync(opts.codeFile, 'utf8');
  }
  throw new Error('Provide --code <file> or --code-stdin');
}

async function main() {
  const opts = parseArgs(process.argv);
  if (!opts.out) {
    console.error(JSON.stringify({ ok: false, error: '--out is required' }));
    process.exit(2);
  }

  const bundlePath = path.join(PUBLIC_DIR, 'bundle.js');
  if (!fs.existsSync(bundlePath)) {
    console.error(
      JSON.stringify({
        ok: false,
        error: 'public/bundle.js missing — run: npm run build (in strudel-template)',
      }),
    );
    process.exit(2);
  }

  const chrome = resolveChrome(opts.chrome);
  if (!chrome) {
    console.error(JSON.stringify({ ok: false, error: 'Chrome/Chromium not found for Strudel render' }));
    process.exit(2);
  }

  let code;
  try {
    code = await readCode(opts);
  } catch (err) {
    console.error(JSON.stringify({ ok: false, error: String(err?.message || err) }));
    process.exit(2);
  }

  const { server, url } = await startStaticServer(PUBLIC_DIR);
  let browser;
  try {
    browser = await puppeteer.launch({
      executablePath: chrome,
      headless: 'new',
      args: [
        '--no-sandbox',
        '--disable-setuid-sandbox',
        '--disable-dev-shm-usage',
        '--autoplay-policy=no-user-gesture-required',
        '--disable-background-timer-throttling',
        '--disable-renderer-backgrounding',
        '--disable-gpu',
      ],
    });

    const page = await browser.newPage();
    page.setDefaultTimeout(opts.timeoutMs);

    const consoleLines = [];
    page.on('console', (msg) => {
      consoleLines.push(`[${msg.type()}] ${msg.text()}`);
    });
    page.on('pageerror', (err) => {
      consoleLines.push(`[pageerror] ${err.message}`);
    });

    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });

    // Wait for init
    await page.waitForFunction(
      () => window.__benchStrudelReady === true || window.__benchStrudelError,
      { timeout: Math.min(opts.timeoutMs, 90000) },
    );

    const readyState = await page.evaluate(() => ({
      ready: !!window.__benchStrudelReady,
      error: window.__benchStrudelError || null,
    }));
    if (!readyState.ready) {
      const payload = {
        ok: false,
        error: `Strudel engine init failed: ${readyState.error || 'unknown'}`,
        log: consoleLines.slice(-80).join('\n'),
      };
      console.log(JSON.stringify(payload));
      process.exit(1);
    }

    const result = await page.evaluate(
      async (userCode, renderOpts) => {
        try {
          return await window.BenchStrudel.renderToBase64Wav(userCode, renderOpts);
        } catch (err) {
          return { ok: false, error: String(err?.message || err) };
        }
      },
      code,
      {
        cycles: opts.cycles,
        durationSec: opts.durationSec,
        sampleRate: opts.sampleRate,
        cps: opts.cps,
      },
    );

    if (!result?.ok || !result.wavBase64) {
      console.log(
        JSON.stringify({
          ok: false,
          error: result?.error || 'Render returned no audio',
          meta: result?.meta || null,
          log: consoleLines.slice(-120).join('\n'),
        }),
      );
      process.exit(1);
    }

    const outPath = path.resolve(opts.out);
    fs.mkdirSync(path.dirname(outPath), { recursive: true });
    fs.writeFileSync(outPath, Buffer.from(result.wavBase64, 'base64'));

    console.log(
      JSON.stringify({
        ok: true,
        out: outPath,
        bytes: fs.statSync(outPath).size,
        meta: result.meta,
        log: consoleLines.slice(-40).join('\n'),
      }),
    );
  } catch (err) {
    console.log(
      JSON.stringify({
        ok: false,
        error: String(err?.message || err),
      }),
    );
    process.exit(1);
  } finally {
    try {
      if (browser) await browser.close();
    } catch {
      /* ignore */
    }
    try {
      server.close();
    } catch {
      /* ignore */
    }
  }
}

main();
