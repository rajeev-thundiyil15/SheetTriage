import { expect, test, type Page } from '@playwright/test';
import path from 'node:path';

const fixture = (name: string) => path.join(__dirname, '..', 'fixtures', name);

async function uploadFile(page: Page, file: string) {
  await page.goto('/app');
  await page.locator('#csv-file').setInputFiles(fixture(file));
  await page.getByRole('button', { name: 'Continue to Schema' }).click();
}

async function uploadAndRun(page: Page, file: string) {
  await uploadFile(page, file);
  await expect(page.locator('#columns-section')).toBeVisible();
  await page.getByRole('button', { name: 'Run Agent' }).click();
}

test('a file whose rows have extra values is refused with the row number', async ({ page }) => {
  await uploadFile(page, 'extra-values.csv');

  await expect(page.locator('#upload-error')).toHaveText(
    'Row 3 has 3 values but the header has 2 columns, so its values can\'t be matched to columns. Fix that row and upload again.');
  await expect(page).toHaveURL(/\/app$/);
});

test('a semicolon CSV saved by Excel on Windows is read into its columns', async ({ page }) => {
  await uploadFile(page, 'excel-semicolons.csv');

  await expect(page).toHaveURL(/\/schema\?upload=/);
  await expect(page.locator('#file-name')).toHaveText('excel-semicolons.csv: 3 rows, 3 columns');
});

test('a run that fails partway explains why and offers no download', async ({ page }) => {
  await uploadAndRun(page, 'api-overloaded.csv');

  await expect(page.locator('#status-badge')).toHaveText('error');
  await expect(page.locator('#log-body')).toContainText(
    'Claude is overloaded right now, so the run stopped. Please try again in a few minutes.');
  await expect(page.locator('#log-body')).not.toContainText('Error code');
  await expect(page.getByRole('link', { name: 'Download Output CSV' })).toBeHidden();
  await expect(page.locator('#review-panel')).toBeHidden();
});

test('starting a second run while one is going says to wait', async ({ browser }) => {
  const first = await browser.newPage();
  const second = await browser.newPage();

  await test.step('start a slow run in one tab', async () => {
    await uploadAndRun(first, 'slow-run.csv');
    await expect(first.locator('#status-badge')).toHaveText('running');
  });

  await test.step('try to start another run in a second tab', async () => {
    const dialog = second.waitForEvent('dialog');
    await uploadAndRun(second, 'orders.csv');
    const message = await dialog;
    expect(message.message()).toContain('A job is already running — please wait for it to finish.');
    await message.accept();
    await expect(second.getByRole('button', { name: 'Run Agent' })).toBeEnabled();
  });

  await test.step('the first run still finishes', async () => {
    await expect(first.locator('#status-badge')).toHaveText('done', { timeout: 15_000 });
  });
});
