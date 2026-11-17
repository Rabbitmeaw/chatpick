// Maintainer build: TypeScript syntax only; query functions execute as copied JavaScript.
import {readFileSync, writeFileSync} from 'node:fs';
import {stripTypeScriptTypes} from 'node:module';
import {fileURLToPath} from 'node:url';

if (process.versions.node !== '24.5.0') throw new Error('Use Node 24.5.0 for reproducible generation.');
const directory = fileURLToPath(new URL('../vendor/starmemo/query/', import.meta.url));
const manifest = JSON.parse(readFileSync(directory + 'manifest.json', 'utf8'));
let output = '// Generated from copied TS with Node 24.5.0. See ORIGIN.md.\n';
output += 'globalThis.StarMemoQueryCore = (() => {\nconst modules = {};\n';
const order = ['local-query-api', 'ai-search-evidence', 'query-agent-message-references',
  'query-agent-conversation-memory', 'query-agent-service', 'local-query-tool-executor',
  'ai-provider-failure', 'ask-channel-service'];
for (const name of order) {
  if (!manifest.files.some(file => file.file === name + '.ts')) throw new Error('Untracked module');
  let code = stripTypeScriptTypes(readFileSync(directory + name + '.ts', 'utf8'), {mode:'transform'});
  const exports = [...code.matchAll(/^export\s+(?:(?:async)\s+)?(?:function|class|const|let|var)\s+(\w+)/gm)].map(m => m[1]);
  code = code.replace(/^import\s+\{([\s\S]*?)\}\s+from\s+['"]([^'"]+)['"];?\s*$/gm,
    (_match, names, path) => {
      const id = path.split('/').pop();
      const binding = names.replace(/\b(\w+)\s+as\s+(\w+)\b/g, '$1: $2');
      return `const {${binding}} = ${path === 'node:crypto' ? 'QueryHost.crypto' : `modules[${JSON.stringify(id)}]`};`;
    }).replace(/^export\s+/gm, '');
  if (/^import\s/m.test(code)) throw new Error('Unhandled runtime import in ' + name);
  output += `modules[${JSON.stringify(name)}] = (() => {\n${code}\nreturn {${exports.join(',')}};\n})();\n`;
}
output += 'return Object.assign({}, ...Object.values(modules));\n})();\n';
const path = directory + 'query-core.js';
if (process.argv.includes('--check')) {
  if (readFileSync(path, 'utf8') !== output) throw new Error('Regenerate query-core.js with the pinned Node version.');
} else writeFileSync(path, output);
