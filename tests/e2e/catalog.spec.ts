import { test, expect } from '@playwright/test';

test('administrator configures profiles and teacher prepares an environment', async ({
  page,
  browser,
}) => {
  const password = process.env.LAB_E2E_PASSWORD!;
  await page.goto('/');
  await page.getByLabel('Логин', { exact: true }).fill('admin.e2e');
  await page.getByLabel('Пароль', { exact: true }).fill(password);
  await page.getByRole('button', { name: 'Войти →' }).click();
  await page.getByRole('link', { name: /Профили и права/ }).click();
  await page.getByLabel('Название шаблона', { exact: true }).fill('Linux E2E');
  await page.getByLabel('Версия образа').fill('e2e-1');
  await page.getByRole('button', { name: 'Добавить шаблон' }).click();
  await expect(page.locator('.catalog-list').filter({ hasText: 'Linux E2E' })).toBeVisible();
  await page.getByLabel('Название профиля', { exact: true }).fill('Практика 512');
  await page
    .getByRole('combobox', { name: 'Версия шаблона', exact: true })
    .selectOption({ label: 'Linux E2E · e2e-1 · LXC' });
  await page.getByLabel('Минимум RAM, MiB').fill('256');
  await page.getByLabel('Максимум RAM, MiB').fill('1024');
  await page.getByLabel('Минимум диска, GiB').fill('5');
  await page.getByRole('button', { name: 'Сохранить профиль' }).click();
  await expect(page.locator('.catalog-list').filter({ hasText: 'Практика 512' })).toBeVisible();
  await page.getByLabel('Название политики', { exact: true }).fill('Учебная политика');
  await page.getByLabel('Создавать LXC', { exact: true }).check();
  await page.getByLabel('Linux', { exact: true }).check();
  await page.getByLabel('LXC на окружение', { exact: true }).fill('30');
  await page.getByLabel('RAM, MiB', { exact: true }).last().fill('24000');
  await page.getByLabel('CPU, кредиты', { exact: true }).fill('12');
  await page.getByLabel('Диски и гибернация, GiB', { exact: true }).fill('350');
  await page.getByLabel('Одновременные занятия').fill('2');
  await page
    .getByRole('combobox', { name: 'Разрешённый демонстрационный профиль', exact: true })
    .selectOption({ label: 'Практика 512 · LXC' });
  await page.getByRole('button', { name: 'Сохранить политику' }).click();
  await page
    .getByRole('combobox', { name: 'Преподаватель', exact: true })
    .selectOption({ label: 'Teacher E2E · teacher.e2e' });
  await page
    .getByRole('combobox', { name: 'Политика', exact: true })
    .selectOption({ label: 'Учебная политика' });
  await expect(page.getByRole('button', { name: 'Назначить', exact: true })).toBeEnabled();
  const assigned = page.waitForResponse(
    (r) => r.url().endsWith('/policy') && r.request().method() === 'PUT',
  );
  await page.getByRole('button', { name: 'Назначить', exact: true }).click();
  expect((await assigned).status()).toBe(200);
  await page.screenshot({ path: 'artifacts/catalog-admin.png' });
  const context = await browser.newContext();
  const teacher = await context.newPage();
  await teacher.goto('http://localhost:5174/');
  await teacher.getByLabel('Логин', { exact: true }).fill('teacher.e2e');
  await teacher.getByLabel('Пароль', { exact: true }).fill(password);
  await teacher.getByRole('button', { name: 'Войти →' }).click();
  await teacher.getByLabel('Название группы').fill('Подготовка окружения');
  await teacher.getByRole('button', { name: 'Создать', exact: true }).click();
  await teacher.getByLabel('Название окружения').fill('Практика Linux');
  await teacher
    .getByRole('combobox', { name: 'Машина студента', exact: true })
    .selectOption({ label: 'Практика 512 · LXC' });
  await teacher
    .getByRole('combobox', { name: 'Демонстрационная машина', exact: true })
    .selectOption({ label: 'Практика 512 · LXC' });
  await teacher.locator('input[name="student_memory_mib"]').fill('256');
  await teacher.locator('input[name="student_disk_gib"]').fill('5');
  await teacher.getByRole('button', { name: 'Рассчитать ресурсы' }).click();
  await expect(teacher.getByText('Одно окружение укладывается в заданные лимиты.')).toBeVisible();
  await teacher.screenshot({ path: 'artifacts/environment-estimate.png', fullPage: true });
  await teacher.getByRole('button', { name: 'Создать окружение', exact: true }).click();
  await expect(teacher.getByText('Подготовлено', { exact: true })).toBeVisible();
  await teacher.getByRole('button', { name: 'Проверить конфигурацию' }).click();
  await expect(teacher.getByText('Проверка завершена', { exact: true })).toBeVisible();
  await teacher.reload();
  await expect(teacher.getByText('Проверка завершена', { exact: true })).toBeVisible();
  await teacher.screenshot({ path: 'artifacts/environment-operation.png', fullPage: true });
  await teacher.setViewportSize({ width: 390, height: 844 });
  expect(await teacher.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(
    true,
  );
  await context.close();
});
