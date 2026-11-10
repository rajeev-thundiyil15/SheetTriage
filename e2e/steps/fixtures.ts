import { test as base, createBdd } from 'playwright-bdd';
import { ResultsPage, SchemaPage, UploadPage } from '../pages/fixtures';

type Pages = { uploadPage: UploadPage; schemaPage: SchemaPage; resultsPage: ResultsPage };

/** The same page objects the spec tests use, for the Cucumber steps. */
export const test = base.extend<Pages>({
  uploadPage: async ({ page }, use) => use(new UploadPage(page)),
  schemaPage: async ({ page }, use) => use(new SchemaPage(page)),
  resultsPage: async ({ page }, use) => use(new ResultsPage(page)),
});

export const { Given, When, Then } = createBdd(test);
