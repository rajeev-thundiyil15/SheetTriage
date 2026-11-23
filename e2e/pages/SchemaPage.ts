import { expect, type Locator, type Page } from '@playwright/test';

/** Step 2: the drafted schema, its questions, and the Run Agent button. */
export class SchemaPage {
  readonly fileName: Locator;
  readonly columns: Locator;
  readonly questionsNote: Locator;
  readonly runButton: Locator;
  readonly ownKey: Locator;
  readonly apiKey: Locator;
  readonly rememberKey: Locator;
  readonly keyNote: Locator;
  readonly limitNote: Locator;

  constructor(readonly page: Page) {
    this.fileName = page.locator('#file-name');
    this.columns = page.locator('#columns-section');
    this.questionsNote = page.locator('#questions-note');
    this.runButton = page.getByRole('button', { name: 'Run Agent' });
    this.ownKey = page.locator('#own-key');
    this.apiKey = page.getByLabel('Your Anthropic API key');
    this.rememberKey = page.getByLabel('Remember it in this browser');
    this.keyNote = page.locator('#key-note');
    this.limitNote = page.locator('#limit-note');
  }

  /** Opens "Use your own Anthropic API key" and types the key in. */
  async useOwnKey(key: string, { remember = false } = {}) {
    await this.page.getByText('Use your own Anthropic API key').click();
    await this.apiKey.fill(key);
    if (remember) await this.rememberKey.check();
  }

  async expectLoaded() {
    await expect(this.page).toHaveURL(/\/schema\?upload=/);
    await expect(this.columns).toBeVisible();
  }

  /** The question card whose text includes these words. */
  question(words: string): Locator {
    return this.page.locator('.question').filter({ hasText: words });
  }

  /** One answer inside a question card. */
  answer(question: Locator, label: string): Locator {
    return question.locator('.question-option').filter({ hasText: label });
  }

  async runAgent() {
    await this.runButton.click();
  }
}
