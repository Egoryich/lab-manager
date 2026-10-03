import { test, expect } from '@playwright/test';

test('administrator sees empty inventory and clearly marked stale observations', async ({
  page,
}) => {
  await page.goto('/');
  await page.getByLabel('Логин', { exact: true }).fill('admin.e2e');
  await page.getByLabel('Пароль', { exact: true }).fill(process.env.LAB_E2E_PASSWORD!);
  await page.getByRole('button', { name: 'Войти →' }).click();
  await page.getByRole('link', { name: /Серверы/ }).click();
  await expect(page.getByText('Серверы ещё не подключены.')).toBeVisible();
  // UI-only fixture; transport and persistence are covered by real TLS/DB tests.
  await page.route('**/api/admin/nodes', (route) =>
    route.fulfill({
      json: [
        {
          id: 'bb8a7cf1-9582-448b-89cc-99e3ea3691b0',
          name: 'Учебный сервер',
          status: 'STALE',
          last_contact_at: '2026-09-29T12:00:00Z',
          sampled_at: '2026-09-29T12:00:00Z',
          error_code: 'NODE_CONNECTION_FAILED',
          host: {
            logical_cpus: 24,
            cores_reported: 12,
            sockets: 1,
            uptime_seconds: 600,
            memory_total_bytes: 33547112448,
            memory_used_bytes: 2228731904,
            memory_free_bytes: 31177109504,
          },
          guest_count: 2,
          storage_count: 3,
          network_bridges: [
            { name: 'vmbr0', active: true, ports: ['nic0'] },
            { name: 'vmbr1', active: true, ports: [] },
          ],
          storages: [
            {
              name: 'student-lvm',
              backend: 'lvmthin',
              active: true,
              total_bytes: 489970204672,
              used_bytes: 1004821,
              available_bytes: 477481706 * 1024,
              thin_metadata_percent: 0.38,
              observed_volume_count: 0,
            },
          ],
          admission_ready: false,
        },
      ],
    }),
  );
  await page.reload();
  await expect(page.getByText('Данные устарели', { exact: true })).toBeVisible();
  await expect(page.getByText('student-lvm')).toBeVisible();
  await expect(page.getByText('0.38%')).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Сетевые мосты' })).toBeVisible();
  await expect(page.getByRole('cell', { name: 'vmbr1' })).toBeVisible();
  await expect(page.getByText(/Это не означает, что сервер выключен/)).toBeVisible();
  await page.screenshot({ path: 'artifacts/nodes-admin.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole('heading', { name: 'Серверы лаборатории' })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: 'artifacts/nodes-mobile.png', fullPage: true });
});
