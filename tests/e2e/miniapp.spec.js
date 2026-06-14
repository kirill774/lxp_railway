const { test, expect } = require('@playwright/test');

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    window.Telegram = {
      WebApp: {
        initData: '',
        initDataUnsafe: { user: { id: 1016718472, first_name: 'Kirill', username: 'kirill774' } },
        expand() {},
        ready() {},
        setHeaderColor() {},
        setBackgroundColor() {},
      },
    };
  });
});

test('renders registration screen without Telegram client', async ({ page }) => {
  await page.goto('/');

  // Wait for loading screen to disappear and register screen to appear
  await expect(page.locator('#screen-register')).toBeVisible({ timeout: 15000 });

  await expect(page.getByRole('heading', { name: 'Добро пожаловать' })).toBeVisible();
  await expect(page.getByPlaceholder('Иванов Иван Иванович')).toBeVisible();
  await expect(page.getByPlaceholder('2БМ2.24')).toBeVisible();

  // Phase 2: new profile fields present
  await expect(page.locator('#reg-level')).toBeVisible();
  await expect(page.locator('#reg-tone')).toBeVisible();
  await expect(page.locator('#reg-format')).toBeVisible();
});

test('registration form has correct default select values', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('#screen-register')).toBeVisible({ timeout: 15000 });

  await expect(page.locator('#reg-level')).toHaveValue('bachelor');
  await expect(page.locator('#reg-tone')).toHaveValue('formal');
  await expect(page.locator('#reg-format')).toHaveValue('docx');
});
