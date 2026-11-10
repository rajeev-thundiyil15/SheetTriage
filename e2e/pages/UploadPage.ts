import { type Locator, type Page } from '@playwright/test';
import path from 'node:path';

/** Step 1: choose a CSV, or the demo data, and continue to the schema page. */
export class UploadPage {
  readonly fileInput: Locator;
  readonly continueButton: Locator;
  readonly demoButton: Locator;
  readonly error: Locator;
  readonly sizeNote: Locator;
  readonly keepNote: Locator;
  readonly signInPrompt: Locator;

  constructor(readonly page: Page) {
    this.keepNote = page.locator('#keep-note');
    this.signInPrompt = page.locator('#sign-in-gate');
    this.fileInput = page.locator('#csv-file');
    this.continueButton = page.getByRole('button', { name: 'Continue to Schema' });
    this.demoButton = page.getByRole('button', { name: 'Try demo data' });
    this.error = page.locator('#upload-error');
    this.sizeNote = page.locator('#size-warning');
  }

  async goto() {
    await this.page.goto('/app');
  }

  /** Picks a file from e2e/fixtures by name. */
  async chooseFile(name: string) {
    await this.fileInput.setInputFiles(path.join(__dirname, '..', 'fixtures', name));
  }

  async upload(name: string) {
    await this.goto();
    await this.chooseFile(name);
    await this.continueButton.click();
  }

  async tryDemoData() {
    await this.goto();
    await this.demoButton.click();
  }
}
