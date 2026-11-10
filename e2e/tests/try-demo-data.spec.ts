import { expect, test } from '@playwright/test';

test('a visitor with no file tries the demo data from start to finish', async ({ page }) => {
  await test.step('click Try demo data', async () => {
    await page.goto('/app');
    await page.getByRole('button', { name: 'Try demo data' }).click();
  });

  await test.step('the schema page has read the demo file', async () => {
    await expect(page).toHaveURL(/\/schema\?upload=/);
    await expect(page.locator('#file-name')).toHaveText('event_registrations.csv: 40 rows, 9 columns');
  });

  await test.step('the date question already suggests month first', async () => {
    const dateQuestion = page.locator('.question').filter({ hasText: 'How should dates like' });
    await expect(dateQuestion).toContainText('Most dates are month first');
    const monthFirst = dateQuestion.locator('.question-option').filter({ hasText: 'Month first' });
    await expect(monthFirst.getByRole('radio')).toBeChecked();
    await expect(monthFirst).toContainText('Suggested');
    await expect(page.locator('#questions-note')).toHaveText('Our suggestions are selected. Change any that are wrong.');
  });

  await test.step('rare countries are left out of the suggested list', async () => {
    const countryQuestion = page.locator('.question').filter({ hasText: 'Should country only allow' });
    await expect(countryQuestion).toContainText("Left out because they're rare: 'U.S.' (1), 'Untied Kingdom' (1)");
  });

  await test.step('the run finishes with nothing to answer first', async () => {
    await page.getByRole('button', { name: 'Run Agent' }).click();
    await expect(page).toHaveURL(/\/results\?job=/);
    await expect(page.locator('#status-badge')).toHaveText('done');
    await expect(page.locator('#summary-headline')).toContainText('of 40 rows are clean.');
  });
});
