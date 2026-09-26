import { randomBytes } from 'node:crypto';
import { writeFileSync } from 'node:fs';

const password = randomBytes(24).toString('hex');
const contents = `# Local development only. Generated secrets; do not commit.
LAB_ENVIRONMENT=development
LAB_PUBLIC_ORIGIN=http://localhost:5173
LAB_DATABASE_URL=postgresql+psycopg://lab:${password}@127.0.0.1:55432/lab
LAB_REDIS_URL=redis://127.0.0.1:56379/0
LAB_ENCRYPTION_KEY=${randomBytes(32).toString('base64url')}=
LAB_DIGEST_KEY=${randomBytes(48).toString('hex')}
POSTGRES_PASSWORD=${password}
`;
try {
  writeFileSync('.env', contents, { flag: 'wx', mode: 0o600 });
  console.log('Created .env with local secrets. Existing files are never overwritten.');
} catch (error) {
  if (error.code === 'EEXIST') console.log('.env already exists; kept unchanged.');
  else throw error;
}
