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

test('starting a second run while one is going says to wait', async ({ browser }) => {
  const firstTab = await browser.newPage();
  const secondTab = await browser.newPage();
  const first = { upload: new UploadPage(firstTab), schema: new SchemaPage(firstTab), results: new ResultsPage(firstTab) };
  const second = { upload: new UploadPage(secondTab), schema: new SchemaPage(secondTab) };

  await test.step('start a slow run in one tab', async () => {
    await first.upload.upload('slow-run.csv');
    await first.schema.expectLoaded();
    await first.schema.runAgent();
    await expect(first.results.status).toHaveText('running');
  });

  await test.step('try to start another run in a second tab', async () => {
    await second.upload.upload('orders.csv');
    await second.schema.expectLoaded();
    // alert() blocks the page until it's answered, so answer it as it opens; waiting
    // for the click to finish first would wait forever.
    const alertText = new Promise<string>(resolve => {
      secondTab.once('dialog', async dialog => {
        resolve(dialog.message());
        await dialog.accept();
      });
    });
    await second.schema.runAgent();
    expect(await alertText).toContain('A job is already running — please wait for it to finish.');
    await expect(second.schema.runButton).toBeEnabled();
  });

  await test.step('the first run still finishes', async () => {
    await expect(first.results.status).toHaveText('done', { timeout: 20_000 });
  });
});
