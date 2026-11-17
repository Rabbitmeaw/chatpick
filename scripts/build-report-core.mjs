// Maintenance only: mechanically erase types; never rewrite the copied functions.
import {readFileSync, writeFileSync} from 'node:fs';
import {stripTypeScriptTypes} from 'node:module';
import {fileURLToPath} from 'node:url';

const directory = fileURLToPath(new URL('../vendor/starmemo/', import.meta.url));
const source = readFileSync(directory + 'report-core.ts', 'utf8');
const code = stripTypeScriptTypes(source, {mode:'strip'})
  .replace(/^import\b[\s\S]*?\sfrom\s+['"][^'"]+['"]\s*\n/gm, '')
  .replace(/^export\s+/gm, '');
const header = '// Generated from report-core.ts with Node 24.5.0 type stripping. See ORIGIN.md.\n';
const entry = `\nreturn {GROUP_REPORT_SYSTEM_PROMPT, GROUP_REPORT_JSON_REPAIR_SYSTEM_PROMPT,
  buildGroupReportInput, parseGroupDailyReport, extractJson, postProcessReport,
  dedupeItems, createSignature, REPORT_MODE_CONFIG};\n`;
const output = header + 'globalThis.StarMemoReportCore = (() => {\n' +
  'const {jsonrepair} = JSONRepair;\n' + code + entry + '})();\n';
if (process.argv.includes('--check')) {
  if (readFileSync(directory + 'report-core.js', 'utf8') !== output) {
    throw new Error('Generated report-core.js differs; regenerate with the pinned Node version.');
  }
} else {
  writeFileSync(directory + 'report-core.js', output);
}
