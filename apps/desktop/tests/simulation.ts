import { expect, type Page } from "@playwright/test";

/** A configuration journey must not revoke evidence used by unrelated call tests. */
export async function isolatedSimulationWorkspace(page: Page, name: string) {
  await expect(page.getByText("SIMULATION", { exact: true })).toBeVisible();
  const url = "http://127.0.0.1:8766";
  const login = await page.request.post(`${url}/v1/auth/session`, {
    data: {
      email: "team@example.test",
      password: "Connected-test-password-123",
    },
  });
  expect(login.ok()).toBeTruthy();
  const administrator = await login.json();
  const created = await page.request.post(`${url}/v1/admin/workspaces`, {
    headers: { authorization: `Bearer ${administrator.access_token}` },
    data: {
      seller_name: name,
      campaign_name: name,
      email: `${name}@example.test`,
      password: "Disposable-workspace-password-123",
    },
  });
  expect(created.ok()).toBeTruthy();
  const session = await created.json();
  const seeded = await page.request.post(`${url}/v1/simulation/seed`, {
    headers: { authorization: `Bearer ${session.access_token}` },
  });
  expect(seeded.ok()).toBeTruthy();
  await page.getByTitle("Sign out").click();
  await page.getByRole("button", { name: "Connect to a server" }).click();
  await page.getByLabel("Server URL").fill(url);
  await page.getByLabel("Email").fill(`${name}@example.test`);
  await page
    .getByLabel("Password", { exact: true })
    .fill("Disposable-workspace-password-123");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
}

/** Give a calling journey its own fictional work without resetting shared state. */
export async function prepareSimulationCall(
  page: Page,
  name: string,
  phone: string,
) {
  await expect(
    page.getByText(/Practice workspace · Calls and provider responses/),
  ).toBeVisible();
  await page.getByRole("button", { name: "Leads", exact: true }).click();
  await page
    .getByLabel("CSV lead data")
    .fill(
      `name,phone,company,timezone,region\n${name},${phone},Example,America/Los_Angeles,415`,
    );
  await page.getByRole("button", { name: "Import leads", exact: true }).click();
  await expect(page.getByText(/1 imported/)).toBeVisible();
  await page.getByRole("button", { name: "Dialer", exact: true }).click();
  const ready = page.getByRole("button", { name: "Go ready", exact: true });
  await expect(
    ready.or(page.getByRole("button", { name: "Pause dialing", exact: true })),
  ).toBeVisible();
  if (await ready.isVisible()) await ready.click();
}
