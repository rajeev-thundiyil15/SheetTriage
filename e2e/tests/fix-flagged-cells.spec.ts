import { expect, test } from '../pages/fixtures';
import { readFile } from 'node:fs/promises';

// The scripted agent flags every rule break and fixes nothing, and the scripted
// recommendation picks the first option the code found, so every run gives the same cards.
test('a person works through grouped fixes, a rule change, a typed value and duplicates', async ({ page, uploadPage, schemaPage, resultsPage }) => {
  await uploadPage.upload('shop-orders.csv');
  await schemaPage.expectLoaded();
  await schemaPage.runAgent();
  await resultsPage.expectFinished();
  await resultsPage.fixesTab.click();

  const group = (column: string, value: string) => page.locator(`.fix-card[data-group="${column}:${value}"]`);
  const list = (name: string) => page.locator('.fix-list-btn', { hasText: name });
  const dialogs: string[] = [];
  page.on('dialog', dialog => { dialogs.push(dialog.message()); dialog.dismiss(); });

  await test.step('suggested fixes come first', async () => {
    await expect(list('Suggested fixes')).toHaveAttribute('aria-pressed', 'true');
    await expect(group('status', 'shiped')).toContainText('Suggested:');
    await group('status', 'shiped').getByRole('button', { name: 'Use "Shipped"' }).click();
    await expect(group('status', 'shiped')).toContainText('Changed to "Shipped".');
    await group('payment_method', 'Credit Card').getByRole('button', { name: "It's correct as is" }).click();
    await expect(group('payment_method', 'Credit Card')).toContainText('Kept as "Credit Card".');
  });

  await test.step('a typed value that breaks the rules is refused with the reason', async () => {
    const date = group('order_date', '31/01/2026');
    await date.getByRole('textbox').fill('next tuesday');
    await date.getByRole('button', { name: 'Save' }).click();
    await expect.poll(() => dialogs.length).toBe(1);
    expect(dialogs[0]).toContain("isn't a date");
  });

  await test.step('needs your input: change a rule, and type a value', async () => {
    await list('Needs your input').click();
    await group('status', 'Returned').getByRole('button', { name: 'Allow "Returned" in status' }).click();
    await expect(group('status', 'Returned')).toContainText('Kept: your rule change allows it.');
    await expect(page.locator('.fix-rule')).toContainText('status allows "Returned" (1 cell)');
    const blank = group('customer', '(blank)');
    await blank.getByRole('textbox').fill('Ana Diaz');
    await blank.getByRole('button', { name: 'Save' }).click();
    await expect(blank).toContainText('Changed to "Ana Diaz".');
  });

  await test.step('undo a rule change', async () => {
    await page.locator('.fix-rule').getByRole('button', { name: 'Undo' }).click();
    await expect(page.locator('.fix-rule')).toHaveCount(0);
    await expect(group('status', 'Returned').getByRole('button', { name: 'Allow "Returned" in status' })).toBeVisible();
  });

  await test.step('duplicates: remove every copy at once', async () => {
    await list('Duplicates').click();
    await page.getByRole('button', { name: /^Remove all 1 cop/ }).click();
    await expect(page.locator('.fix-card').first()).toContainText('Removed row 33. Row 10 is kept.');
  });

  await test.step('the cleaned data and the downloads follow', async () => {
    await resultsPage.cleanedDataTab.click();
    await expect(resultsPage.table.locator('td.cell-approved', { hasText: 'Shipped' })).toHaveCount(1);
    await expect(resultsPage.table.locator('td.cell-approved', { hasText: 'Ana Diaz' })).toHaveCount(1);
    await expect(resultsPage.table.locator('tr.row-removed')).toHaveCount(1);

    const download = page.waitForEvent('download');
    await resultsPage.downloadCsv.click();
    const lines = (await readFile(await (await download).path(), 'utf-8')).trim().split(/\r?\n/);
    expect(lines).toHaveLength(40);
    expect(lines.find(l => l.startsWith('5004,'))).toContain(',Shipped,');
    expect(lines.find(l => l.startsWith('5011,'))).toContain('Ana Diaz');
    expect(lines.some(l => l.startsWith('5032,'))).toBe(false);

    await resultsPage.fixesTab.click();
    const left = page.waitForEvent('download');
    await page.locator('#download-remaining').click();
    const remaining = (await readFile(await (await left).path(), 'utf-8')).trim().split(/\r?\n/);
    expect(remaining[0].startsWith('row,order_id,')).toBe(true);
    expect(remaining.some(l => l.startsWith('5,'))).toBe(false);
    expect(remaining.some(l => l.startsWith('3,'))).toBe(true);
  });
});
