import { expect } from '@playwright/test';
import { Given, Then, When } from './fixtures';

Given('I am on the upload page', async ({ uploadPage }) => {
  await uploadPage.goto();
});

When('I try the demo data', async ({ uploadPage }) => {
  await uploadPage.demoButton.click();
});

When('I upload {string}', async ({ uploadPage }, file: string) => {
  await uploadPage.chooseFile(file);
  await uploadPage.continueButton.click();
});

Then('the schema page reads {string}', async ({ schemaPage }, text: string) => {
  await schemaPage.expectLoaded();
  await expect(schemaPage.fileName).toHaveText(text);
});

Then('the date question suggests {string}', async ({ schemaPage }, label: string) => {
  const answer = schemaPage.answer(schemaPage.question('How should dates like'), label);
  await expect(answer.getByRole('radio')).toBeChecked();
  await expect(answer).toContainText('Suggested');
});

When('I run the agent', async ({ schemaPage }) => {
  await schemaPage.expectLoaded();
  await schemaPage.runAgent();
});

Then('the run finishes', async ({ resultsPage }) => {
  await resultsPage.expectFinished();
});

Then('the summary reads {string}', async ({ resultsPage }, text: string) => {
  await expect(resultsPage.headline).toHaveText(text);
});

Then('the summary ends with {string}', async ({ resultsPage }, text: string) => {
  await expect(resultsPage.headline).toContainText(text);
});

Then('{int} cell is flagged, showing {string}', async ({ resultsPage }, count: number, value: string) => {
  await expect(resultsPage.flaggedCells).toHaveCount(count);
  await expect(resultsPage.flaggedCells.first()).toHaveText(value);
});

Then('I stay on the upload page with the message {string}', async ({ page, uploadPage }, message: string) => {
  await expect(uploadPage.error).toHaveText(message);
  await expect(page).toHaveURL(/\/app$/);
});
