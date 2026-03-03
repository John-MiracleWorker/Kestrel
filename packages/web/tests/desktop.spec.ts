import { test, expect } from '@playwright/test';

test.describe('Kestrel Desktop E2E', () => {
    test('has title and main chat interface', async ({ page }) => {
        await page.goto('/');
        await expect(page).toHaveTitle(/Kestrel/);
        const rootBlock = page.locator('#root');
        await expect(rootBlock).toBeVisible();
    });

    test('chat input is visible and accepts text', async ({ page }) => {
        await page.goto('/');
        const chatInput = page.locator('textarea, input[type="text"]').first();
        await expect(chatInput).toBeVisible();
        await chatInput.fill('Hello Kestrel');
        await expect(chatInput).toHaveValue('Hello Kestrel');
    });

    test('app loads without fatal console errors', async ({ page }) => {
        const consoleErrors: string[] = [];
        page.on('console', (msg) => {
            if (msg.type() === 'error') consoleErrors.push(msg.text());
        });
        await page.goto('/');
        await page.waitForLoadState('networkidle');
        // Filter out known benign noise (favicon 404s, Vite HMR messages)
        const fatalErrors = consoleErrors.filter(
            (e) => !e.includes('favicon') && !e.includes('hot-update'),
        );
        expect(fatalErrors).toHaveLength(0);
    });

    test('navigation does not show error boundary', async ({ page }) => {
        await page.goto('/');
        const errorBoundary = page.locator('[data-testid="error-boundary"], .error-boundary');
        await expect(errorBoundary).toHaveCount(0);
    });
});
