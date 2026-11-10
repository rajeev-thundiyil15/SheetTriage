import { test as base } from '@playwright/test';
import { ResultsPage } from './ResultsPage';
import { SchemaPage } from './SchemaPage';
import { UploadPage } from './UploadPage';

type Pages = { uploadPage: UploadPage; schemaPage: SchemaPage; resultsPage: ResultsPage };

/** Every test gets the three page objects, built on its own page. */
export const test = base.extend<Pages>({
  uploadPage: async ({ page }, use) => use(new UploadPage(page)),
  schemaPage: async ({ page }, use) => use(new SchemaPage(page)),
  resultsPage: async ({ page }, use) => use(new ResultsPage(page)),
});

export { expect } from '@playwright/test';
export { ResultsPage, SchemaPage, UploadPage };
