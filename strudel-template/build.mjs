import * as esbuild from 'esbuild';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

await esbuild.build({
  entryPoints: [path.join(__dirname, 'src/browser-render.js')],
  bundle: true,
  format: 'iife',
  globalName: 'BenchStrudelBundle',
  outfile: path.join(__dirname, 'public/bundle.js'),
  platform: 'browser',
  target: ['chrome120'],
  sourcemap: false,
  logLevel: 'info',
  // Worklet URLs inside superdough need to stay as URLs — leave as-is
  loader: {
    '.wasm': 'file',
  },
});

console.log('Built public/bundle.js');
