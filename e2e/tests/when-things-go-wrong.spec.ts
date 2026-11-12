import { expect, test, ResultsPage, SchemaPage, UploadPage } from '../pages/fixtures';

test('a file whose rows have extra values is refused with the row number', async ({ page, uploadPage }) => {
  await uploadPage.upload('extra-values.csv');

  await expect(uploadPage.error).toHaveText(
    'Row 3 has 3 values but the header has 2 columns, so its values can\'t be matched to columns. Fix that row and upload again.');
  await expect(page).toHaveURL(/\/app$/);
});

test('a semicolon CSV saved by Excel on Windows is read into its columns', async ({ uploadPage, schemaPage }) => {
  await uploadPage.upload('excel-semicolons.csv');

  await schemaPage.expectLoaded();
  await expect(schemaPage.fileName).toHaveText('excel-semicolons.csv: 3 rows, 3 columns');
});

test('a run that fails partway explains why and offers no download', async ({ uploadPage, schemaPage, resultsPage }) => {
  await uploadPage.upload('api-overloaded.csv');
  await schemaPage.expectLoaded();
  await schemaPage.runAgent();

  await expect(resultsPage.status).toHaveText('error');
  await expect(resultsPage.log).toContainText(
    'Claude is overloaded right now, so the run stopped. Please try again in a few minutes.');
  await expect(resultsPage.log).not.toContainText('Error code');
  await expect(resultsPage.downloadCsv).toBeHidden();
  await expect(resultsPage.reviewPanel).toBeHidden();
});

test('two people can run files at the same time', async ({ browser }) => {
  const firstTab = await browser.newPage();
  const secondTab = await browser.newPage();
  const first = { upload: new UploadPage(firstTab), schema: new SchemaPage(firstTab), results: new ResultsPage(firstTab) };
  const second = { upload: new UploadPage(secondTab), schema: new SchemaPage(secondTab), results: new ResultsPage(secondTab) };

  await test.step('start a slow run in one tab', async () => {
    await first.upload.upload('slow-run.csv');
    await first.schema.expectLoaded();
    await first.schema.runAgent();
    await expect(first.results.status).toHaveText('running');
  });

  await test.step('start another run in a second tab while the first is going', async () => {
    await second.upload.upload('orders.csv');
    await second.schema.expectLoaded();
    await second.schema.runAgent();
    await second.results.expectFinished();
    await expect(second.results.headline).toContainText('of 10 rows are clean.');
  });

  await test.step('the first run was still going, and finishes with its own results', async () => {
    await expect(first.results.status).toHaveText('running');
    await expect(first.results.status).toHaveText('done', { timeout: 20_000 });
    await expect(first.results.title).toHaveText('slow-run.csv');
  });
});

test('running out of free runs explains when the next one frees up', async ({ page, uploadPage, schemaPage }) => {
  const message = "You've used your 3 free runs for today. You can run another file in about 5 hours.";
  await page.route('**/clean', route => route.fulfill({ status: 429, contentType: 'application/json', body: JSON.stringify({ detail: message }) }));
  const dialogs: string[] = [];
  page.on('dialog', dialog => { dialogs.push(dialog.message()); dialog.dismiss(); });

  await uploadPage.upload('orders.csv');
  await schemaPage.expectLoaded();
  await schemaPage.runAgent();

  await expect(page.locator('#limit-note')).toHaveText(message);
  await expect(page).toHaveURL(/\/schema\?upload=/);
  await expect(schemaPage.runButton).toBeEnabled();
  expect(dialogs).toEqual([]);
});
