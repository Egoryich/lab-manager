import { test, expect } from '@playwright/test';

test('teacher creates a group, student joins, owner sees the roster', async ({ browser, page }) => {
  const password = process.env.LAB_E2E_PASSWORD;
  if (!password) throw new Error('Run the isolated browser suite via python tools/run-e2e.py');
  await page.goto('/');
  await page.getByLabel('Логин', { exact: true }).fill('teacher.e2e');
  await page.getByLabel('Пароль', { exact: true }).fill(password);
  await page.getByRole('button', { name: 'Войти →' }).click();
  await expect(page.getByRole('heading', { name: 'Учебные группы', exact: true })).toBeVisible();
  await page.getByLabel('Название группы').fill('Практикум Linux');
  await page.getByRole('button', { name: 'Создать', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Практикум Linux' })).toBeVisible();
  const code = await page.locator('.join-code').innerText();
  expect(code).toMatch(/^[A-Z2-9]{6}$/);
  await page.screenshot({ path: 'artifacts/teacher-group.png', fullPage: true });

  const studentContext = await browser.newContext();
  const student = await studentContext.newPage();
  await student.goto('http://localhost:5174/');
  await student.getByRole('button', { name: 'Регистрация студента' }).click();
  await student.getByLabel('Логин', { exact: true }).fill('student.e2e');
  await student.getByLabel('Имя и фамилия').fill('Иван Студентов');
  await student.getByLabel('Пароль', { exact: true }).fill(password);
  await student.getByRole('button', { name: 'Создать аккаунт' }).click();
  await expect(student.getByRole('status')).toContainText('Аккаунт создан');
  await student.getByLabel('Логин', { exact: true }).fill('student.e2e');
  await student.getByLabel('Пароль', { exact: true }).fill(password);
  await student.getByRole('button', { name: 'Войти →' }).click();
  await student.getByLabel('Код группы').fill(code);
  await student.getByRole('button', { name: 'Вступить', exact: true }).click();
  await expect(student.getByRole('heading', { name: 'Практикум Linux' })).toBeVisible();
  await expect(student.getByRole('heading', { name: 'Код приглашения' })).toHaveCount(0);
  await page.reload();
  await expect(page.getByText('Иван Студентов')).toBeVisible();
  await student.setViewportSize({ width: 390, height: 844 });
  await student.screenshot({ path: 'artifacts/student-mobile.png', fullPage: true });
  expect(
    await student.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
  ).toBe(true);
  await studentContext.close();
});

test('administrator grants teaching rights and issues recovery code', async ({ page }) => {
  await page.goto('/');
  await page.getByLabel('Логин', { exact: true }).fill('admin.e2e');
  await page.getByLabel('Пароль', { exact: true }).fill(process.env.LAB_E2E_PASSWORD!);
  await page.getByRole('button', { name: 'Войти →' }).click();
  await page.getByRole('link', { name: /Пользователи/ }).click();
  const row = page.locator('.admin-row').filter({ hasText: 'student.e2e' });
  await row.getByRole('button', { name: 'Назначить преподавателем' }).click();
  await expect(row.getByRole('button', { name: 'Разрешить группы' })).toBeVisible();
  page.once('dialog', (dialog) => dialog.accept());
  await row.getByRole('button', { name: 'Восстановить доступ' }).click();
  await expect(page.getByLabel('Одноразовый код')).toHaveValue(/.{32,}/);
  await page.getByRole('button', { name: 'Скрыть код' }).click();
  await expect(page.getByLabel('Одноразовый код')).toHaveCount(0);
});
