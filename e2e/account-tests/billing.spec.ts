import { expect, test } from '../pages/fixtures';

// The monthly plan, against the test server's stand-in for Stripe: its payment page "pays" and
// sends the app Stripe's message two seconds later, and its billing page cancels the plan.
const newEmail = () => `payer-${Date.now()}-${Math.floor(Math.random() * 1e6)}@example.com`;

test('a free account upgrades, waits for Stripe to confirm, then cancels', async ({ page, account }) => {
  await test.step('a free account is offered the monthly plan', async () => {
    await account.signIn(newEmail(), '/files');
    await account.open();
    await expect(account.planNote).toContainText('Free plan');
    await expect(account.upgrade).toHaveText('Upgrade ($3 a month)');
    await expect(account.manageBilling).toHaveCount(0);
  });

  await test.step('the plans page sends them to pay', async () => {
    await account.upgrade.click();
    await expect(page).toHaveURL(/\/pricing$/);
    await expect(page.locator('#monthly-price')).toHaveText('$3 a month');
    await expect(page.locator('#monthly-runs')).toHaveText('100');
    await page.getByRole('button', { name: 'Upgrade' }).click();
  });

  await test.step('back on My files, the plan switches once Stripe\'s message arrives', async () => {
    await expect(page).toHaveURL(/\/files\?upgraded=/);
    await expect(page.locator('#upgrade-notice')).toHaveText(
      "You're on the monthly plan: up to 100 runs a month, with results kept for 30 days.", { timeout: 15_000 });
    await expect(page.locator('#stat-plan')).toHaveText('Monthly');
    await expect(page.locator('#stat-runs-label')).toHaveText('Runs this month');
  });

  await test.step('the plans page offers Manage billing instead of paying again', async () => {
    await page.goto('/pricing');
    await expect(page.locator('#plan-monthly')).toHaveClass(/current/);
    await expect(page.locator('#monthly-note')).toContainText('Your plan renews on');
    await expect(page.getByRole('button', { name: 'Manage billing' })).toBeVisible();
  });

  await test.step('cancelling on Stripe\'s billing page puts the account back on the free plan', async () => {
    await account.open();
    await expect(account.planNote).toContainText('Monthly plan');
    await account.manageBilling.click();
    await expect(page).toHaveURL(/\/pricing$/);
    await expect(page.getByRole('button', { name: 'Upgrade' })).toBeEnabled();
    await expect(page.locator('#monthly-note')).toContainText('Your earlier plan has ended');
  });
});

test('a visitor is asked to sign in before upgrading', async ({ page }) => {
  await page.goto('/pricing');

  const signIn = page.getByRole('link', { name: 'Sign in to upgrade' });
  await expect(signIn).toHaveAttribute('href', '/sign-in?next=%2Fpricing');
  await expect(page.locator('#monthly-note')).toHaveText('The plan is saved to your account, so sign in first.');
});
