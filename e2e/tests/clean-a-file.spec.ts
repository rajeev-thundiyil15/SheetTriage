import { expect, test } from '@playwright/test';
import path from 'node:path';

const ORDERS_CSV = path.join(__dirname, '..', 'fixtures', 'orders.csv');

test('a user uploads a CSV, runs the agent, and reviews the flagged row', async ({ page }) => {
  await test.step('upload the file', async () => {
    await page.goto('/app');
    await page.locator('#csv-file').setInputFiles(ORDERS_CSV);
    await page.getByRole('button', { name: 'Continue to Schema' }).click();
  });

  await test.step('check the schema page read the file', async () => {
    await expect(page).toHaveURL(/\/schema\?upload=/);
    await expect(page.locator('#file-name')).toHaveText('orders.csv: 10 rows, 4 columns');
    await expect(page.locator('#columns-section')).toBeVisible();
  });

  await test.step('run the agent', async () => {
    await page.getByRole('button', { name: 'Run Agent' }).click();
    await expect(page).toHaveURL(/\/results\?job=/);
    await expect(page.locator('#status-badge')).toHaveText('done');
  });

  await test.step('review the results', async () => {
    await expect(page.locator('#summary-headline')).toHaveText('9 of 10 rows are clean.');
    await expect(page.getByRole('button', { name: 'Error report (1)' })).toBeVisible();

    const flaggedCell = page.locator('.review-table td.cell-flagged');
    await expect(flaggedCell).toHaveCount(1);
    await expect(flaggedCell).toHaveText('not-an-email');

    await expect(page.getByRole('link', { name: 'Download Output CSV' })).toBeVisible();
  });
});
