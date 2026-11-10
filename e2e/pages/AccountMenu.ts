import { expect, type Locator, type Page } from '@playwright/test';

/** The top bar's account area, and the development sign-in page behind it. */
export class AccountMenu {
  readonly signInLink: Locator;
  readonly menu: Locator;
  readonly myFiles: Locator;

  constructor(readonly page: Page) {
    this.signInLink = page.locator('#nav-sign-in');
    this.menu = page.locator('#account-menu');
    this.myFiles = page.locator('#nav-files');
  }

  /** Signs in from the sign-in page, then lands on `next`. */
  async signIn(email: string, next = '/files') {
    await this.page.goto(`/sign-in?next=${encodeURIComponent(next)}`);
    await this.page.getByLabel('Email').fill(email);
    await this.page.getByRole('button', { name: 'Continue' }).click();
    await expect(this.menu).toContainText(email);
  }

  async open() {
    await this.menu.locator('summary').click();
  }

  async signOut() {
    await this.open();
    await this.menu.getByRole('button', { name: 'Sign out' }).click();
  }

  async deleteAccount() {
    await this.open();
    this.page.once('dialog', dialog => dialog.accept());
    await this.menu.getByRole('button', { name: 'Delete my account' }).click();
  }
}
