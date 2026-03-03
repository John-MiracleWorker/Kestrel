import { test, expect } from '@playwright/test';

test.describe('Kestrel Desktop E2E', () => {
    test('has title and main chat interface', async ({ page }) => {
        // Go to the main page
        await page.goto('/');

        // Expect a title "to contain" Kestrel.
        await expect(page).toHaveTitle(/Kestrel/);

        // Expect the app root to be visible
        const rootBlock = page.locator('#root');
        await expect(rootBlock).toBeVisible();
    });
});
