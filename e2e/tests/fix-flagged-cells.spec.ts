import { expect, test } from '../pages/fixtures';
import { readFile } from 'node:fs/promises';

// The scripted agent flags every rule break and fixes nothing, and the scripted
// recommendation picks the first option the code found, so every run gives the same cards.
test('a person approves suggested fixes and removes a duplicate, and the download follows', async ({ page, uploadPage, schemaPage, resultsPage }) => {
  await uploadPage.upload('shop-orders.csv');
  await schemaPage.expectLoaded();
  await schemaPage.runAgent();
  await resultsPage.expectFinished();
  await expect(resultsPage.fixesTab).toBeVisible();

  const card = (head: string) => page.locator('.fix-card').filter({ has: page.locator('.fix-head', { hasText: head }) });

  await test.step('the Fix flagged cells tab lists cells and the duplicate', async () => {
    await resultsPage.fixesTab.click();
    await expect(card('Row 5 · status')).toContainText('shiped');
    await expect(card('Row 5 · status')).toContainText('Suggested:');
    await expect(card('Row 12 · customer')).toContainText('No safe fix to suggest');
    await expect(card('Rows 10, 33')).toContainText('These rows look like the same record.');
  });

  await test.step('use a suggested fix, keep a value, and remove the copy', async () => {
    await card('Row 5 · status').getByRole('button', { name: 'Use "Shipped"' }).click();
    await expect(card('Row 5 · status')).toContainText('Changed to "Shipped".');
    await card('Row 21 · payment_method').getByRole('button', { name: "It's correct as is" }).click();
    await expect(card('Row 21 · payment_method')).toContainText('Kept as "Credit Card"');
    await card('Rows 10, 33').getByRole('button', { name: 'Remove row 33' }).click();
    await expect(card('Rows 10, 33')).toContainText('Removed row 33. Row 10 is kept.');
    await expect(resultsPage.page.locator('#fixes-summary')).toContainText('3 of');
  });

  await test.step('undo puts a choice back', async () => {
    await card('Row 21 · payment_method').getByRole('button', { name: 'Undo' }).click();
    await expect(card('Row 21 · payment_method')).toContainText('Use "Card"');
    await card('Row 21 · payment_method').getByRole('button', { name: "It's correct as is" }).click();
  });

  await test.step('the cleaned data shows the choices', async () => {
    await resultsPage.cleanedDataTab.click();
    await expect(resultsPage.reviewNote).toContainText('You fixed 1 cell, kept 1 value as correct, removed 1 duplicate row');
    await expect(resultsPage.table.locator('td.cell-approved', { hasText: 'Shipped' })).toHaveCount(1);
    await expect(resultsPage.table.locator('tr.row-removed')).toHaveCount(1);
  });

  await test.step('the download has the fix and leaves out the copy', async () => {
    const download = page.waitForEvent('download');
    await resultsPage.downloadCsv.click();
    const lines = (await readFile(await (await download).path(), 'utf-8')).trim().split(/\r?\n/);
    expect(lines).toHaveLength(40);
    expect(lines.find(l => l.startsWith('5004,'))).toContain(',Shipped,');
    expect(lines.some(l => l.startsWith('5032,'))).toBe(false);
    const creditCard = lines.find(l => l.startsWith('5020,'))!;
    expect(creditCard).toContain('Credit Card');
    expect(creditCard).not.toContain('not in allowed values');
  });

  await test.step('use all suggested fixes at once', async () => {
    await resultsPage.fixesTab.click();
    const useAll = page.getByRole('button', { name: /^Use all \d+ suggested fix/ });
    await useAll.click();
    await expect(useAll).toHaveCount(0);
    await expect(card('Row 22 · order_date')).toContainText('Changed to "2026-01-31".');
  });
});
