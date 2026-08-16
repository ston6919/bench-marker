/** Patch @kabelsalat/web package.json so ESM named exports resolve under Node/esbuild. */
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const root = path.dirname(fileURLToPath(import.meta.url));
const pkgPath = path.join(root, 'node_modules/@kabelsalat/web/package.json');
if (!fs.existsSync(pkgPath)) {
  console.warn('postinstall: @kabelsalat/web not installed, skip');
  process.exit(0);
}
const data = JSON.parse(fs.readFileSync(pkgPath, 'utf8'));
data.exports = {
  '.': {
    import: './dist/index.mjs',
    require: './dist/index.js',
    default: './dist/index.mjs',
  },
};
if (!data.module) data.module = './dist/index.mjs';
fs.writeFileSync(pkgPath, JSON.stringify(data, null, 2) + '\n');
console.log('postinstall: patched @kabelsalat/web exports');
