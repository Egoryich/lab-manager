import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const failures = [];
const check = (condition, message) => { if (!condition) failures.push(message); };
const read = relative => fs.readFileSync(path.join(root, relative), 'utf8');
const exists = relative => fs.existsSync(path.join(root, relative));
const json = relative => JSON.parse(read(relative));

try {
  const meta = json('docs/spec-source.json');
  const specBytes = fs.readFileSync(path.join(root, meta.repository_path));
  const hash = crypto.createHash('sha256').update(specBytes).digest('hex');
  check(hash === meta.sha256, 'Specification SHA-256 differs from imported source metadata.');
  const sourceSections = [...specBytes.toString('utf8').matchAll(/^# (\d+)\. (.+)\r?$/gm)].map(m => ({n: Number(m[1]), title: m[2].trim()}));
  check(sourceSections.length === 76 && meta.sections === 76, 'Specification must contain 76 numbered sections.');
  const matrix = json('docs/requirements.json');
  check(matrix.requirements.length === 76, 'Requirement matrix must contain 76 entries.');
  const ids = matrix.requirements.map(r => r.id);
  check(new Set(ids).size === ids.length, 'Duplicate requirement IDs.');
  const acceptance = read('docs/acceptance.md');
  const definedAcceptance = new Set([...acceptance.matchAll(/ACC-\d{2}/g)].map(m => m[0]));
  check(definedAcceptance.size === 60, 'Acceptance plan must define ACC-01 through ACC-60.');
  for (let n = 1; n <= 60; n++) check(definedAcceptance.has(`ACC-${String(n).padStart(2, '0')}`), `Missing ACC-${n}.`);
  const buildGates = new Set([...read('docs/development.md').matchAll(/BUILD-\d{2}/g)].map(m => m[0]));
  const amendments = json('docs/requirements-updates.json');
  const decisionIds = new Set([...read('docs/decisions.md').matchAll(/^\| (DEC-\d{2}) \|/gm)].map(m => m[1]));
  check(decisionIds.size === 17 && amendments.decisions.length === 17, 'Expected 17 user decisions and amendment mappings.');
  check(new Set(amendments.decisions.map(d => d.id)).size === 17, 'Duplicate user decision mappings.');
  for (const decision of amendments.decisions) {
    check(decisionIds.has(decision.id), `Unknown decision ${decision.id}.`);
    check(exists(decision.design_document), `Missing document for ${decision.id}.`);
    for (const id of decision.acceptance_ids) check(definedAcceptance.has(id), `Unknown acceptance ${id} in ${decision.id}.`);
  }
  for (const section of sourceSections) {
    const matches = matrix.requirements.filter(r => r.section === section.n);
    check(matches.length === 1, `Expected one mapping for section ${section.n}.`);
    if (matches.length !== 1) continue;
    const entry = matches[0];
    check(entry.id === `SPEC-${String(section.n).padStart(2, '0')}`, `Invalid ID for section ${section.n}.`);
    check(entry.title === section.title, `Source title mismatch for ${entry.id}.`);
    check(exists(entry.design_document), `Missing design document for ${entry.id}.`);
    check(['NOT_STARTED', 'IN_PROGRESS'].includes(entry.implementation_status), `Unsupported implementation claim for ${entry.id}.`);
    check(['NOT_RUN', 'PARTIAL'].includes(entry.verification_status), `Unsupported verification claim for ${entry.id}.`);
    if (entry.implementation_status === 'IN_PROGRESS') check(exists(entry.evidence_document ?? ''), `Missing implementation evidence for ${entry.id}.`);
    check(Array.isArray(entry.acceptance_ids) && (entry.acceptance_ids.length > 0 || entry.build_gate_ids?.length > 0), `No verification mapping for ${entry.id}.`);
    for (const id of entry.acceptance_ids ?? []) check(definedAcceptance.has(id), `Unknown acceptance ID ${id} in ${entry.id}.`);
    for (const id of entry.build_gate_ids ?? []) check(buildGates.has(id), `Unknown build gate ${id} in ${entry.id}.`);
    for (const id of entry.amended_by ?? []) check(decisionIds.has(id), `Unknown amendment ${id} in ${entry.id}.`);
  }

  const apiPlan = read('docs/api.md').replace(/\s+/g, ' ');
  const specText = specBytes.toString('utf8');
  const requiredRoutes = [...specText.matchAll(/^(GET|POST|PATCH|PUT|DELETE)\s+(\/api\/\S+)\s*$/gm)]
    .map(m => `${m[1]} ${m[2]}`).filter(route => route !== 'DELETE /api/storage/cleanup/all');
  for (const route of requiredRoutes) check(apiPlan.includes(route), `Missing required API route: ${route}.`);
  const permissionDoc = read('docs/permissions.md');
  const requiredPermissions = new Set([...specText.matchAll(/^can_[a-z_]+\s*$/gm)].map(m => m[0].trim()));
  for (const permission of requiredPermissions) check(permissionDoc.includes(`\`${permission}\``), `Missing permission: ${permission}.`);
  check(requiredPermissions.size === 20, 'Expected the 20 permissions from the source specification.');

  const componentDirs = ['apps/api', 'apps/web', 'apps/node-agent', 'packages/shared-types', 'packages/client-sdk', 'infra/docker', 'infra/wireguard', 'infra/guacamole', 'infra/systemd', 'infra/ansible', 'migrations', 'tests/unit', 'tests/integration', 'tests/e2e', 'tests/load'];
  for (const dir of componentDirs) check(exists(`${dir}/README.md`), `Missing component description: ${dir}.`);
  json('docs/infrastructure.example.json');

  const files = [];
  const walk = dir => {
    for (const item of fs.readdirSync(dir, {withFileTypes: true})) {
      if (['.git', '.cache', 'node_modules', '.venv', 'artifacts', 'test-results', 'dist'].includes(item.name)) continue;
      const full = path.join(dir, item.name);
      if (item.isDirectory()) walk(full);
      else if (item.name.endsWith('.md')) files.push(full);
    }
  };
  walk(root);
  let localLinks = 0;
  for (const file of files) {
    // Ignore fenced examples and remote URLs; anchors are not validated here.
    const markdown = fs.readFileSync(file, 'utf8').replace(/```[\s\S]*?```/g, '');
    for (const m of markdown.matchAll(/\[[^\]]*\]\(([^)]+)\)/g)) {
      let target = m[1].trim().replace(/^<|>$/g, '');
      if (/^(?:[a-z][a-z0-9+.-]*:|#)/i.test(target)) continue;
      target = decodeURIComponent(target.split('#')[0]);
      if (!target) continue;
      localLinks++;
      check(fs.existsSync(path.resolve(path.dirname(file), target)), `Broken local link in ${path.relative(root, file)}: ${target}`);
    }
  }
  if (failures.length) throw new Error(failures.join('\n'));
  console.log(`PASS: source SHA-256 verified; 76 original sections + 17 decisions mapped; 60 acceptance IDs and ${buildGates.size} build gates; ${requiredRoutes.length} original API routes; 20 permission names; ${componentDirs.length} component directories; ${files.length} Markdown files; ${localLinks} local links resolve.`);
  console.log('Scope: specification traceability and documents only; see docs/implementation-status.md for executable test evidence.');
} catch (error) {
  console.error(`FAIL: ${error.message}`);
  process.exitCode = 1;
}
