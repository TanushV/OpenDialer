import { expect, test } from '@playwright/test';

async function enterSimulation(page: import('@playwright/test').Page) {
  await page.goto('/');
  if (await page.getByRole('button', { name: 'Try a simulated workspace' }).isVisible()) {
    await page.getByRole('button', { name: 'Try a simulated workspace' }).click();
  }
  await expect(page.getByText('SIMULATION', { exact: true })).toBeVisible();
}

test('local setup, readiness, calling, evidence, human notes and opt-out', async ({ page }) => {
  await enterSimulation(page);
  await expect(page.getByRole('heading', { name: 'Your next conversation' })).toBeVisible();
  await page.getByRole('button', { name: 'Go ready' }).click();
  await page.getByRole('button', { name: 'Get next lead' }).click();
  await expect(page.getByTestId('lead-name')).not.toBeEmpty();
  await page.getByRole('button', { name: 'Start call', exact: true }).click();
  await expect(page.getByText('RINGING', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Simulate human answer' }).click();
  await expect(page.getByText('HUMAN', { exact: true })).toBeVisible();
  await page.getByLabel('Simulation transcript').fill('We already use another provider.');
  await page.getByRole('button', { name: 'Add prospect turn' }).click();
  await expect(page.getByText('We already use another provider.', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Hang up', exact: true }).click();
  await page.getByLabel('Disposition').selectOption('callback_requested');
  await page.getByRole('button', { name: 'Save outcome' }).click();
  await page.getByRole('button', { name: 'Review', exact: true }).click();
  await page.getByRole('button', { name: /Review call/ }).first().click();
  await page.getByLabel('Your notes').fill('Keep this human correction after final processing.');
  await page.getByRole('button', { name: 'Save notes' }).click();
  await expect(page.getByText('Notes saved')).toBeVisible();
  await page.reload();
  await page.getByRole('button', { name: 'Review', exact: true }).click();
  await page.getByRole('button', { name: /Review call/ }).first().click();
  await expect(page.getByLabel('Your notes')).toHaveValue('Keep this human correction after final processing.');
});

test('admin inventory, lead import, policy, preparation and real readiness boundary', async ({ page }) => {
  await enterSimulation(page);
  await page.getByRole('button', { name: 'Leads', exact: true }).click();
  await page.getByLabel('CSV lead data').fill('name,phone,company,timezone\nTaylor Example,+12125550177,Example Co,America/New_York');
  await page.getByRole('button', { name: 'Import leads' }).click();
  await expect(page.getByText('Taylor Example', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Prepare lead packets' }).click();
  await expect(page.getByText(/Preparation queued/)).toBeVisible();
  await page.getByRole('button', { name: 'Numbers', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Numbers & callbacks' })).toBeVisible();
  await page.getByRole('button', { name: 'Plan regional capacity' }).click();
  await expect(page.getByText('Regional capacity proposal')).toBeVisible();
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  await page.getByRole('button', { name: 'Run preflight' }).click();
  await expect(page.getByText(/Simulated providers/)).toBeVisible();
});

test('connected authentication and shared state survive reconnect', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByText('Opening your workspace…')).toBeHidden();
  if (await page.getByTitle('Sign out').isVisible()) await page.getByTitle('Sign out').click();
  await page.getByRole('button', { name: 'Connect to a server' }).click();
  await page.getByLabel('Server URL').fill('http://127.0.0.1:8766');
  await page.getByLabel('Email').fill('team@example.test');
  await page.getByLabel('Password', { exact: true }).fill('Connected-test-password-123');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByText('Connected', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Leads', exact: true }).click();
  await expect(page.getByText('Jordan Ellis', { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText('Connected', { exact: true })).toBeVisible();
});
