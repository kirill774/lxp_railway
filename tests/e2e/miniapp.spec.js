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

  await expect(page.getByRole('heading', { name: 'Добро пожаловать' })).toBeVisible();
  await expect(page.getByPlaceholder('Иванов Иван Иванович')).toBeVisible();
  await expect(page.getByPlaceholder('2БМ2.24')).toBeVisible();
});
