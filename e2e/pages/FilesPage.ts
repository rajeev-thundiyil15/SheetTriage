import { expect, type Locator, type Page } from '@playwright/test';

/** My files: the account's saved runs, with their results and Delete buttons. */
export class FilesPage {
  readonly subtitle: Locator;
  readonly jobs: Locator;
  readonly empty: Locator;
  readonly signInNotice: Locator;
  readonly runsThisMonth: Locator;

  constructor(readonly page: Page) {
    this.subtitle = page.locator('#page-sub');
    this.jobs = page.locator('.job');
    this.empty = page.locator('#jobs-empty');
    this.signInNotice = page.locator('#sign-in-notice');
    this.runsThisMonth = page.locator('#stat-runs');
  }

  async goto() {
    await this.page.goto('/files');
    await expect(this.page.locator('.loading')).toHaveCount(0);
  }

  /** The card for a run of this file. */
  job(filename: string): Locator {
    return this.jobs.filter({ has: this.page.locator('.job-name', { hasText: filename }) });
  }

  async deleteJob(filename: string) {
    this.page.once('dialog', dialog => dialog.accept());
    await this.job(filename).getByRole('button', { name: 'Delete' }).click();
  }
}
