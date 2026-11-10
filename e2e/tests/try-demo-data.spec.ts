import { expect, test } from '../pages/fixtures';

test('a visitor with no file tries the demo data from start to finish', async ({ uploadPage, schemaPage, resultsPage }) => {
  await test.step('click Try demo data', async () => {
    await uploadPage.tryDemoData();
  });

  await test.step('the schema page has read the demo file', async () => {
    await schemaPage.expectLoaded();
    await expect(schemaPage.fileName).toHaveText('event_registrations.csv: 40 rows, 9 columns');
  });

  await test.step('the date question already suggests month first', async () => {
    const dateQuestion = schemaPage.question('How should dates like');
    const monthFirst = schemaPage.answer(dateQuestion, 'Month first');
    await expect(dateQuestion).toContainText('Most dates are month first');
    await expect(monthFirst.getByRole('radio')).toBeChecked();
    await expect(monthFirst).toContainText('Suggested');
    await expect(schemaPage.questionsNote).toHaveText('Our suggestions are selected. Change any that are wrong.');
  });

  await test.step('rare countries are left out of the suggested list', async () => {
    await expect(schemaPage.question('Should country only allow'))
      .toContainText("Left out because they're rare: 'U.S.' (1), 'Untied Kingdom' (1)");
  });

  await test.step('the run finishes with nothing to answer first', async () => {
    await schemaPage.runAgent();
    await resultsPage.expectFinished();
    await expect(resultsPage.headline).toContainText('of 40 rows are clean.');
  });
});
