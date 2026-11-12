import { expect, test } from '../pages/fixtures';
import { AccountMenu, FilesPage, ResultsPage } from '../pages/fixtures';

// These run against the test server with the development sign-in turned on. Each test
// signs in with its own email, so their saved runs never mix.
const newEmail = () => `user-${Date.now()}-${Math.floor(Math.random() * 1e6)}@example.com`;

test('a visitor can clean a file without signing in, kept for an hour', async ({ uploadPage, schemaPage, resultsPage, filesPage, account }) => {
  await test.step('the upload page works as normal and offers sign-in to save results', async () => {
    await uploadPage.goto();
    await expect(uploadPage.fileInput).toBeAttached();
    await expect(uploadPage.keepNote).toContainText(
      'Results are kept for 1 hour in this browser. Sign in to save them to My files for 7 days instead.');
    await expect(uploadPage.page.locator('#runs-left')).toHaveText(/^\d+ of 1000 free runs left today\.$/);
    await expect(account.signInLink).toBeVisible();
  });

  await test.step('their own file runs from start to finish', async () => {
    await uploadPage.chooseFile('orders.csv');
    await uploadPage.continueButton.click();
    await schemaPage.expectLoaded();
    await schemaPage.runAgent();
    await resultsPage.expectFinished();
    await expect(resultsPage.title).toHaveText('orders.csv');
    await expect(resultsPage.keepNote).toContainText('only this browser can open it');
    await expect(resultsPage.keepNote).toContainText('Sign in to save it to My files');
    await expect(resultsPage.copyLink).toBeHidden();
  });

  await test.step('My files shows the run and offers to sign in', async () => {
    await filesPage.goto();
    await expect(filesPage.signInNotice).toBeVisible();
    await expect(filesPage.job('orders.csv')).toContainText('1 row flagged');
  });
});

test('a signed-in user\'s run is saved to My files', async ({ page, uploadPage, schemaPage, resultsPage, filesPage, account }) => {
  await account.signIn(newEmail(), '/app');
  await expect(uploadPage.keepNote).toHaveText('Results are saved to My files for 7 days.1000 of 1000 free runs left today.');

  await test.step('run a file', async () => {
    await uploadPage.chooseFile('orders.csv');
    await uploadPage.continueButton.click();
    await schemaPage.expectLoaded();
    await schemaPage.runAgent();
    await resultsPage.expectFinished();
    await expect(resultsPage.title).toHaveText('orders.csv');
    await expect(resultsPage.keepNote).toContainText('Saved to My files until');
    await expect(resultsPage.copyLink).toBeVisible();
  });

  await test.step('My files lists it with its result and the month\'s usage', async () => {
    await filesPage.goto();
    await expect(filesPage.jobs).toHaveCount(1);
    await expect(filesPage.job('orders.csv')).toContainText('1 row flagged');
    await expect(filesPage.job('orders.csv')).toContainText('Kept until');
    await expect(filesPage.runsUsed).toHaveText('1 of 1000');
  });

  await test.step('opening it brings back the review', async () => {
    await filesPage.job('orders.csv').getByRole('link', { name: 'Open' }).click();
    await resultsPage.expectFinished();
    await expect(resultsPage.flaggedCells).toHaveCount(1);
  });

  await test.step('signing out hides it', async () => {
    await account.signOut();
    await expect(page).toHaveURL(/\/app$/);
    await filesPage.goto();
    await expect(filesPage.jobs).toHaveCount(0);
  });
});

test('another account cannot open your results link', async ({ browser, page, uploadPage, schemaPage, resultsPage, account }) => {
  await account.signIn(newEmail(), '/app');
  await uploadPage.demoButton.click();
  await schemaPage.expectLoaded();
  await schemaPage.runAgent();
  await resultsPage.expectFinished();
  const link = page.url();

  const otherContext = await browser.newContext();
  const otherPage = await otherContext.newPage();
  await new AccountMenu(otherPage).signIn(newEmail());

  await otherPage.goto(link);
  const other = new ResultsPage(otherPage);
  await expect(other.log).toContainText('This run is no longer available');
  await expect(other.reviewPanel).toBeHidden();
  await expect(other.downloadCsv).toBeHidden();
  await expect(new FilesPage(otherPage).jobs).toHaveCount(0);
  await otherContext.close();
});

test('runs from before signing in move to the account', async ({ uploadPage, schemaPage, resultsPage, filesPage, account }) => {
  await uploadPage.tryDemoData();
  await schemaPage.expectLoaded();
  await schemaPage.runAgent();
  await resultsPage.expectFinished();

  await account.signIn(newEmail());

  await expect(filesPage.job('event_registrations.csv')).toHaveCount(1);
  await expect(filesPage.signInNotice).toBeHidden();
});

test('deleting a run from its results page removes it', async ({ page, uploadPage, schemaPage, resultsPage, filesPage, account }) => {
  await account.signIn(newEmail(), '/app');
  await uploadPage.demoButton.click();
  await schemaPage.expectLoaded();
  await schemaPage.runAgent();
  await resultsPage.expectFinished();
  const link = page.url();

  page.once('dialog', dialog => dialog.accept());
  await resultsPage.deleteButton.click();

  await expect(page).toHaveURL(/\/files$/);
  await expect(filesPage.empty).toBeVisible();
  await page.goto(link);
  await expect(resultsPage.log).toContainText('This run is no longer available');
});

test('deleting an account signs you out and removes its files', async ({ page, uploadPage, schemaPage, resultsPage, filesPage, account }) => {
  const email = newEmail();
  await account.signIn(email, '/app');
  await uploadPage.demoButton.click();
  await schemaPage.expectLoaded();
  await schemaPage.runAgent();
  await resultsPage.expectFinished();

  await account.deleteAccount();

  await expect(page).toHaveURL(/\/app$/);
  await expect(account.signInLink).toBeVisible();
  await account.signIn(email);
  await expect(filesPage.empty).toBeVisible();
});

test.describe('at phone width', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test('My files and the account menu fit without scrolling sideways', async ({ page, uploadPage, schemaPage, resultsPage, filesPage, account }) => {
    const overflow = () => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    await account.signIn(newEmail(), '/app');
    await uploadPage.demoButton.click();
    await schemaPage.expectLoaded();
    await schemaPage.runAgent();
    await resultsPage.expectFinished();
    expect(await overflow()).toBe(0);

    await filesPage.goto();
    await expect(filesPage.jobs).toHaveCount(1);
    await account.open();
    expect(await overflow()).toBe(0);
  });
});
