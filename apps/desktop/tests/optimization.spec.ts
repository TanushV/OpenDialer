import { expect, test } from "@playwright/test";

async function simulation(page: import("@playwright/test").Page) {
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  if (
    await page
      .getByRole("button", { name: "Try a simulated workspace" })
      .isVisible()
  )
    await page
      .getByRole("button", { name: "Try a simulated workspace" })
      .click();
  await expect(page.getByText("SIMULATION", { exact: true })).toBeVisible();
  if (await page.getByRole("button", { name: "Pause dialing" }).isVisible())
    await page.getByRole("button", { name: "Pause dialing" }).click();
}

test("optimization goals, forecast, learning state and provider settings persist", async ({
  page,
}) => {
  await simulation(page);
  await page.getByRole("button", { name: "Intelligence", exact: true }).click();
  await page.getByLabel("Conversion goal").selectOption("sale_won");
  await page.getByLabel("Outcome window (days)").fill("30");
  await page
    .getByRole("button", { name: "Save optimization settings" })
    .click();
  await page
    .getByRole("button", { name: "Refresh learning and capacity" })
    .click();
  await expect(
    page.getByText("Future calling opportunities", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Operational training dataset", { exact: true }),
  ).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: "Intelligence", exact: true }).click();
  await expect(page.getByLabel("Conversion goal")).toHaveValue("sale_won");
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByLabel("Live coach provider").selectOption("openrouter");
  await expect(page.getByLabel("OpenRouter API key")).toBeVisible();
  await expect(page.getByText(/Cerebras only/)).toBeVisible();
});

test("live events deliver contextual guidance and conversion recording without call polling", async ({
  page,
}) => {
  await simulation(page);
  const sockets: string[] = [];
  page.on("websocket", (ws) => sockets.push(ws.url()));
  await page.reload();
  await expect(page.getByRole("button", { name: "Go ready" })).toBeVisible();
  await page.getByRole("button", { name: "Go ready" }).click();
  await page.getByRole("button", { name: "Get next lead" }).click();
  await page.getByRole("button", { name: "Start call", exact: true }).click();
  await page.getByRole("button", { name: "Simulate human answer" }).click();
  await page
    .getByLabel("Simulation transcript")
    .fill("We cannot afford another subscription.");
  await page.getByRole("button", { name: "Add prospect turn" }).click();
  await expect(
    page.getByText("What outcome would make the investment worthwhile?", {
      exact: false,
    }),
  ).toBeVisible();
  expect(sockets.some((url) => url.endsWith("/v1/live"))).toBeTruthy();
  await page.getByRole("button", { name: "Hang up", exact: true }).click();
  await page.getByLabel("Disposition").selectOption("qualified_conversation");
  await page.getByRole("button", { name: "Save outcome" }).click();
  await page.getByRole("button", { name: "Review", exact: true }).click();
  await page
    .getByRole("button", { name: /Review call/ })
    .first()
    .click();
  await page.getByLabel("Conversion event").selectOption("meeting_held");
  await page
    .getByLabel("Conversion source reference")
    .fill("Reviewed permissioned simulation meeting");
  await page.getByRole("button", { name: "Record conversion" }).click();
  await expect(
    page.getByText("Conversion recorded", { exact: true }),
  ).toBeVisible();
});
