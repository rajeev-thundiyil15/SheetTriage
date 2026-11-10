import { expect, test } from '../pages/fixtures';

test('a user uploads a CSV, runs the agent, and reviews the flagged row', async ({ uploadPage, schemaPage, resultsPage }) => {
  await test.step('upload the file', async () => {
    await uploadPage.upload('orders.csv');
  });

  await test.step('check the schema page read the file', async () => {
    await schemaPage.expectLoaded();
    await expect(schemaPage.fileName).toHaveText('orders.csv: 10 rows, 4 columns');
  });

  await test.step('run the agent', async () => {
    await schemaPage.runAgent();
    await resultsPage.expectFinished();
  });

  await test.step('review the results', async () => {
    await expect(resultsPage.headline).toHaveText('9 of 10 rows are clean.');
    await expect(resultsPage.errorReportTab).toHaveText('Error report (1)');
    await expect(resultsPage.flaggedCells).toHaveCount(1);
    await expect(resultsPage.flaggedCells).toHaveText('not-an-email');
    await expect(resultsPage.downloadCsv).toBeVisible();
  });
});

test('refreshing the results page keeps the review', async ({ page, uploadPage, schemaPage, resultsPage }) => {
  await test.step('upload the file and run the agent', async () => {
    await uploadPage.upload('orders.csv');
    await schemaPage.expectLoaded();
    await schemaPage.runAgent();
    await resultsPage.expectFinished();
  });

  await test.step('refresh the page', async () => {
    await page.reload();
  });

  await test.step('check the results are still there', async () => {
    await expect(resultsPage.headline).toHaveText('9 of 10 rows are clean.');
    await expect(resultsPage.flaggedCells).toHaveText('not-an-email');
    await expect(resultsPage.downloadCsv).toBeVisible();
    await expect(resultsPage.log).not.toContainText('Connection to server lost');
  });
});
