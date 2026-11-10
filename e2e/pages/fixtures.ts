import { test as base } from '@playwright/test';
import { AccountMenu } from './AccountMenu';
import { FilesPage } from './FilesPage';
import { ResultsPage } from './ResultsPage';
import { SchemaPage } from './SchemaPage';
import { UploadPage } from './UploadPage';

type Pages = {
  uploadPage: UploadPage;
  schemaPage: SchemaPage;
  resultsPage: ResultsPage;
  filesPage: FilesPage;
  account: AccountMenu;
};

/** Every test gets the page objects, built on its own page. */
export const test = base.extend<Pages>({
  uploadPage: async ({ page }, use) => use(new UploadPage(page)),
  schemaPage: async ({ page }, use) => use(new SchemaPage(page)),
  resultsPage: async ({ page }, use) => use(new ResultsPage(page)),
  filesPage: async ({ page }, use) => use(new FilesPage(page)),
  account: async ({ page }, use) => use(new AccountMenu(page)),
});

export { expect } from '@playwright/test';
export { AccountMenu, FilesPage, ResultsPage, SchemaPage, UploadPage };
