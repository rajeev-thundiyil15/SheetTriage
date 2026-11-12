import { expect, type Locator, type Page } from '@playwright/test';

/** Step 3: live progress, the summary, the review tables and the downloads. */
export class ResultsPage {
  readonly status: Locator;
  readonly headline: Locator;
  readonly log: Locator;
  readonly reviewPanel: Locator;
  readonly reviewNote: Locator;
  readonly cleanedDataTab: Locator;
  readonly errorReportTab: Locator;
  readonly filter: Locator;
  readonly search: Locator;
  readonly table: Locator;
  readonly flaggedCells: Locator;
  readonly correctedCells: Locator;
  readonly downloadCsv: Locator;
  readonly downloadReport: Locator;
  readonly title: Locator;
  readonly keepNote: Locator;
  readonly copyLink: Locator;
  readonly deleteButton: Locator;
  readonly allColumnsToggle: Locator;

  constructor(readonly page: Page) {
    this.title = page.locator('#results-title');
    this.keepNote = page.locator('#job-note');
    this.copyLink = page.getByRole('button', { name: 'Copy link' });
    this.deleteButton = page.locator('#delete-job-btn');
    this.allColumnsToggle = page.locator('#toggle-all-columns');
    this.status = page.locator('#status-badge');
    this.headline = page.locator('#summary-headline');
    this.log = page.locator('#log-body');
    this.reviewPanel = page.locator('#review-panel');
    this.reviewNote = page.locator('#review-note');
    this.cleanedDataTab = page.locator('#tab-data');
    this.errorReportTab = page.locator('#tab-report');
    this.filter = page.locator('#review-filter');
    this.search = page.locator('#review-search');
    this.table = page.locator('.review-table');
    this.flaggedCells = this.table.locator('td.cell-flagged');
    this.correctedCells = this.table.locator('td.cell-changed');
    this.downloadCsv = page.getByRole('link', { name: 'Download Output CSV' });
    this.downloadReport = page.getByRole('link', { name: 'Download Error Report' });
  }

  /** The review's on/off button for one column of the file. */
  columnToggle(column: string): Locator {
    return this.page.locator(`.column-toggle[data-column="${column}"]`);
  }

  async expectFinished() {
    await expect(this.page).toHaveURL(/\/results\?job=/);
    await expect(this.status).toHaveText('done');
  }
}
