import { expect, test } from '../pages/fixtures';
import { readFile } from 'node:fs/promises';

test('script code in a CSV is shown as text and never runs', async ({ page, uploadPage, schemaPage, resultsPage }) => {
  const header = `<img src=x onerror="window.__injected='header'">`;
  const cell = `<img src=x onerror="window.__injected='cell'">`;
  const dialogs: string[] = [];
  page.on('dialog', dialog => { dialogs.push(dialog.message()); dialog.dismiss(); });
  const injected = () => page.evaluate(() => (window as unknown as { __injected?: string }).__injected);

  await test.step('the schema page shows the column name as text', async () => {
    await uploadPage.upload('script-injection.csv');
    await schemaPage.expectLoaded();
    await expect(schemaPage.columns).toContainText(header);
    expect(await injected()).toBeUndefined();
  });

  await test.step('the results page shows the header and values as text', async () => {
    await schemaPage.runAgent();
    await resultsPage.expectFinished();
    await expect(resultsPage.table.locator('th').filter({ hasText: header })).toHaveCount(1);
    await expect(resultsPage.flaggedCells.filter({ hasText: cell })).toHaveCount(1);
    await expect(resultsPage.flaggedCells.filter({ hasText: '<script>' })).toHaveCount(1);
    await expect(page.locator('#column-checklist')).toContainText(header);
  });

  await test.step('none of it ran', async () => {
    expect(await injected()).toBeUndefined();
    expect(dialogs).toEqual([]);
    await expect(resultsPage.table.locator('img')).toHaveCount(0);
  });
});

test('both downloads hold the run\'s results', async ({ page, uploadPage, schemaPage, resultsPage }) => {
  await uploadPage.upload('orders.csv');
  await schemaPage.expectLoaded();
  await schemaPage.runAgent();
  await resultsPage.expectFinished();

  await test.step('the cleaned CSV keeps every row and notes the flagged one', async () => {
    const download = page.waitForEvent('download');
    await resultsPage.downloadCsv.click();
    const file = await download;
    expect(file.suggestedFilename()).toBe('output.csv');
    const lines = (await readFile(await file.path(), 'utf-8')).trim().split(/\r?\n/);
    expect(lines[0]).toBe('id,customer,email,quantity,_errors');
    expect(lines).toHaveLength(11);
    expect(lines.find(line => line.startsWith('3,'))).toContain('not-an-email');
  });

  await test.step('the error report lists the flagged row', async () => {
    const download = page.waitForEvent('download');
    await resultsPage.downloadReport.click();
    const file = await download;
    expect(file.suggestedFilename()).toBe('error_report.csv');
    // A summary by column, then one detail line per problem.
    const lines = (await readFile(await file.path(), 'utf-8')).trim().split(/\r?\n/);
    expect(lines[0]).toBe('SUMMARY');
    expect(lines).toContain('email,1');
    expect(lines).toContain('total_errors,1');
    const details = lines.slice(lines.indexOf('DETAILS') + 1);
    expect(details[0]).toBe('row_id,column,original_value,error,action_needed');
    expect(details).toHaveLength(2);
    expect(details[1]).toMatch(/^3,email,not-an-email,/);
  });
});

test('columns can be turned off and on, in the cleaned data and the error report', async ({ uploadPage, schemaPage, resultsPage }) => {
  await uploadPage.upload('orders.csv');
  await schemaPage.expectLoaded();
  await schemaPage.runAgent();
  await resultsPage.expectFinished();
  await expect(resultsPage.reviewPanel).toBeVisible();
  const email = resultsPage.columnToggle('email');
  const headers = resultsPage.table.locator('th');

  await test.step('every column starts on, with its issue count', async () => {
    await expect(email).toHaveAttribute('aria-pressed', 'true');
    await expect(email).toContainText('1');
    await expect(headers.filter({ hasText: /^email$/ })).toHaveCount(1);
  });

  await test.step('turning email off hides it and its flag from the cleaned data', async () => {
    await email.click();
    await expect(email).toHaveAttribute('aria-pressed', 'false');
    await expect(headers.filter({ hasText: /^email$/ })).toHaveCount(0);
    await expect(resultsPage.flaggedCells).toHaveCount(0);
    await resultsPage.filter.selectOption('flagged');
    await expect(resultsPage.page.locator('#review-count')).toHaveText('0 rows');
  });

  await test.step('the error report follows the same columns', async () => {
    await resultsPage.errorReportTab.click();
    await expect(resultsPage.filter).toBeHidden();
    await expect(resultsPage.page.locator('#review-count')).toHaveText('0 issues');
    await email.click();
    await expect(resultsPage.page.locator('#review-count')).toHaveText('Showing 1–1 of 1 issues');
    await expect(resultsPage.table).toContainText('not-an-email');
  });

  await test.step('None and All switch every column at once', async () => {
    await resultsPage.allColumnsToggle.click();
    await expect(resultsPage.allColumnsToggle).toHaveText('All');
    await expect(resultsPage.page.locator('.review-empty')).toContainText('No columns are turned on');
    await resultsPage.allColumnsToggle.click();
    await expect(resultsPage.page.locator('#review-count')).toHaveText('Showing 1–1 of 1 issues');
  });
});

test.describe('at phone width', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test('no page scrolls sideways from upload to results', async ({ page, uploadPage, schemaPage, resultsPage }) => {
    const overflow = () => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);

    await uploadPage.goto();
    expect(await overflow()).toBe(0);

    await uploadPage.demoButton.click();
    await schemaPage.expectLoaded();
    expect(await overflow()).toBe(0);

    await schemaPage.runAgent();
    await resultsPage.expectFinished();
    await expect(resultsPage.reviewPanel).toBeVisible();
    expect(await overflow()).toBe(0);
  });
});
