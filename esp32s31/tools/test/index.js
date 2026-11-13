// Aggregator so that `node --test tools/test` works on Node 22 (a bare directory argument is resolved to this
// file). It imports every *.test.mjs next to it. When the runner is started with no arguments it already
// discovers every file under test/ by itself, so in that mode this file does nothing (avoids running twice).
import { readdirSync } from 'node:fs';
import { basename, dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
if (basename(process.argv[1] || '') !== 'index.js') {
  for (const f of readdirSync(here).filter(n => n.endsWith('.test.mjs')).sort())
    await import(pathToFileURL(join(here, f)).href);
}
