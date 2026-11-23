import { expect, test } from '../pages/fixtures';

// The scripted server accepts any well-formed key and refuses ones with "refused" in them.
const KEY = 'sk-ant-test-' + 'a1B2c3D4e5'.repeat(3);
const REFUSED_KEY = 'sk-ant-test-refused-' + '0'.repeat(20);
const KEY_STORE = 'sheettriage.api_key';

test('a run on your own key sends the key with the run and finishes', async ({ page, uploadPage, schemaPage, resultsPage }) => {
  await uploadPage.upload('orders.csv');
  await schemaPage.expectLoaded();
  await schemaPage.useOwnKey(KEY);

  const sent = page.waitForRequest(request => request.url().endsWith('/clean'));
  await schemaPage.runAgent();

  expect((await sent).postData()).toContain(KEY);
  await resultsPage.expectFinished();
  await expect(resultsPage.headline).toHaveText('9 of 10 rows are clean.');
  expect(await page.evaluate(name => localStorage.getItem(name), KEY_STORE)).toBeNull();
});

test('the key is remembered in this browser only when the box is ticked', async ({ page, uploadPage, schemaPage, resultsPage }) => {
  await uploadPage.upload('orders.csv');
  await schemaPage.expectLoaded();
  await schemaPage.useOwnKey(KEY, { remember: true });
  await schemaPage.runAgent();
  await resultsPage.expectFinished();

  await uploadPage.upload('orders.csv');
  await schemaPage.expectLoaded();

  await expect(schemaPage.ownKey).toHaveAttribute('open', '');
  await expect(schemaPage.apiKey).toHaveValue(KEY);
  await schemaPage.rememberKey.uncheck();
  expect(await page.evaluate(name => localStorage.getItem(name), KEY_STORE)).toBeNull();
});

test('a key Anthropic refuses is explained next to the key field, and nothing runs', async ({ page, uploadPage, schemaPage }) => {
  const dialogs: string[] = [];
  page.on('dialog', dialog => { dialogs.push(dialog.message()); dialog.dismiss(); });
  await uploadPage.upload('orders.csv');
  await schemaPage.expectLoaded();
  await schemaPage.useOwnKey(REFUSED_KEY, { remember: true });

  await schemaPage.runAgent();

  await expect(schemaPage.keyNote).toHaveText("Anthropic didn't accept that API key. Check you copied all of it, or create a new one.");
  await expect(page).toHaveURL(/\/schema\?upload=/);
  await expect(schemaPage.runButton).toBeEnabled();
  await expect(schemaPage.apiKey).toBeFocused();
  expect(await page.evaluate(name => localStorage.getItem(name), KEY_STORE)).toBeNull();
  expect(dialogs).toEqual([]);
});

test('something that is not a key is refused before anything is sent to Anthropic', async ({ uploadPage, schemaPage }) => {
  await uploadPage.upload('orders.csv');
  await schemaPage.expectLoaded();
  await schemaPage.useOwnKey('my-password');

  await schemaPage.runAgent();

  await expect(schemaPage.keyNote).toHaveText("That doesn't look like an Anthropic API key. Keys start with sk-ant-.");
});

test('running out of free runs opens the key field', async ({ page, uploadPage, schemaPage }) => {
  const message = "You've used your 3 free runs for today. You can run another file in about 5 hours, "
    + 'or add your own Anthropic API key for as many runs as you like.';
  await page.route('**/clean', route => route.fulfill({ status: 429, contentType: 'application/json', body: JSON.stringify({ detail: message }) }));
  await uploadPage.upload('orders.csv');
  await schemaPage.expectLoaded();
  await expect(schemaPage.ownKey).not.toHaveAttribute('open', '');

  await schemaPage.runAgent();

  await expect(schemaPage.limitNote).toHaveText(message);
  await expect(schemaPage.ownKey).toHaveAttribute('open', '');
  await expect(schemaPage.apiKey).toBeVisible();
});
