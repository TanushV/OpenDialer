import { expect, test } from "./fixtures";
import { waitForPreparation } from "./simulation";

test("a late plan request cannot replace the newly selected list", async ({
  page,
}) => {
  await page.route("**/v1/admin/numbers/summary*", (route) => {
    const list = new URL(route.request().url()).searchParams.get("list_name");
    return route.fulfill({
      json: {
        lists: ["First list", "Second list"],
        states: [],
        inventory: {
          total: 0,
          ready: 0,
          pending_setup: 0,
          limited_use: 0,
          paused_outbound: 0,
          investigating: 0,
        },
        stable_assignments: 0,
        forecast: {
          list_name: list,
          regions: [],
          forecast_attempts: 0,
          staffing_shortfall: 0,
          number_shortfall: 0,
          projection: { status: "ready" },
        },
      },
    });
  });
  let release!: () => void;
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  let requested = false;
  await page.route("**/v1/admin/numbers/proposal", async (route) => {
    requested = true;
    await held;
    await route.fulfill({ json: {} });
  });
  try {
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
    await page.getByRole("button", { name: "Numbers", exact: true }).click();
    await page.getByLabel("Plan numbers for list").selectOption("First list");
    await expect(page.getByTestId("capacity-proposal")).toContainText(
      "First list.",
    );
    await page
      .getByRole("button", { name: "Plan state coverage", exact: true })
      .click();
    await expect.poll(() => requested).toBeTruthy();
    await page.getByLabel("Plan numbers for list").selectOption("Second list");
    await expect(page.getByTestId("capacity-proposal")).toContainText(
      "Second list.",
    );
    release();
    await expect(
      page.getByRole("button", { name: "Plan state coverage", exact: true }),
    ).toBeEnabled();
    await expect(page.getByTestId("capacity-proposal")).toContainText(
      "Second list.",
    );
  } finally {
    release();
  }
});

for (const connected of [false, true]) {
  test(`${connected ? "hosted" : "local"} list plan opens state quotes without a purchase`, async ({
    page,
  }) => {
    await page.goto("/");
    await expect(page.getByText("Opening your workspace…")).toBeHidden();
    if (connected) {
      if (await page.getByTitle("Sign out").isVisible())
        await page.getByTitle("Sign out").click();
      await page.getByRole("button", { name: "Connect to a server" }).click();
      await page.getByLabel("Server URL").fill("http://127.0.0.1:8766");
      await page.getByLabel("Email").fill("team@example.test");
      await page
        .getByLabel("Password", { exact: true })
        .fill("Connected-test-password-123");
      await page.getByRole("button", { name: "Sign in", exact: true }).click();
    } else if (
      await page
        .getByRole("button", { name: "Try a simulated workspace" })
        .isVisible()
    ) {
      await page
        .getByRole("button", { name: "Try a simulated workspace" })
        .click();
    }
    await page.getByRole("button", { name: "Leads", exact: true }).click();
    await page.getByLabel("List name", { exact: true }).fill("California list");
    await page
      .getByLabel("CSV lead data")
      .fill(
        "name,phone,state,timezone\nLA,+14155550194,CA,America/Los_Angeles",
      );
    await page
      .getByRole("button", { name: "Import leads", exact: true })
      .click();
    await expect(
      page.getByText("1 leads imported", { exact: true }),
    ).toBeVisible();
    await waitForPreparation(page);
    await page.getByRole("button", { name: "Numbers", exact: true }).click();
    await page
      .getByLabel("Plan numbers for list")
      .selectOption("California list");
    await page
      .getByRole("button", { name: "Plan state coverage", exact: true })
      .click();
    const plan = page.getByTestId("capacity-proposal");
    await expect(plan).toContainText("California");
    await expect(plan).toContainText("California list");
    await page.screenshot({
      path: test.info().outputPath("state-plan.png"),
      fullPage: true,
    });
    const purchases: unknown[] = [];
    await page.route("**/v1/admin/numbers/purchase", (route) => {
      purchases.push(route.request().postDataJSON());
      return route.abort();
    });
    await plan
      .getByRole("button", { name: "Find numbers in California" })
      .click();
    await expect(page.getByLabel("Search state")).toHaveValue("CA");
    await page
      .getByRole("button", { name: "Find available numbers", exact: true })
      .click();
    await expect(page.getByText(/Searched area codes:/)).toBeVisible();
    await page
      .getByRole("button", { name: /^Review \+1/ })
      .first()
      .click();
    await expect(
      page.getByRole("button", { name: "Approve and order number" }),
    ).toBeDisabled();
    expect(purchases).toEqual([]);
  });
}

for (const loseResponse of [false, true]) {
  test(`number purchase preserves approval and uncertain orders with ${loseResponse ? "a lost HTTP response" : "a carrier receipt"}`, async ({
    page,
  }) => {
    const quote = {
      id: "quote-approval-test",
      phone: "+16505550188",
      initial_commitment_usd: 3,
      monthly_cost_usd: 1,
      expires_at: "2026-10-25T23:59:00Z",
    };
    const bodies: unknown[] = [];
    let ordered = false;
    await page.route("**/v1/admin/numbers/search", (route) =>
      route.fulfill({ json: { numbers: [quote] } }),
    );
    await page.route("**/v1/admin/numbers/orders", (route) =>
      route.fulfill({
        json: ordered
          ? [
              {
                ...quote,
                status: "uncertain",
                error: "Receipt unavailable. Check order status.",
              },
            ]
          : [],
      }),
    );
    await page.route("**/v1/admin/numbers/purchase", (route) => {
      bodies.push(route.request().postDataJSON());
      ordered = true;
      return loseResponse
        ? route.abort("failed")
        : route.fulfill({ json: { ...quote, status: "uncertain" } });
    });
    let releaseLate!: () => void;
    const lateRead = new Promise<void>((resolve) => {
      releaseLate = resolve;
    });
    let reads = 0;
    let lateDelivered = false;
    await page.route("**/v1/admin/settings", async (route) => {
      const index = ++reads;
      const response = await route.fetch();
      if (index === 1) {
        await lateRead;
        await route.fulfill({ response });
        lateDelivered = true;
      } else await route.fulfill({ response });
    });
    await page.goto("/");
    await expect(page.getByText("Opening your workspace…")).toBeHidden();
    const sample = page.getByRole("button", {
      name: "Try a simulated workspace",
    });
    if (await sample.isVisible()) await sample.click();
    await page.getByRole("button", { name: "Numbers", exact: true }).click();
    await page.getByText("Add numbers with approval", { exact: true }).click();
    await page.getByLabel("Maximum owned numbers", { exact: true }).fill("12");
    releaseLate();
    await expect.poll(() => lateDelivered).toBeTruthy();
    await expect(
      page.getByLabel("Maximum owned numbers", { exact: true }),
    ).toHaveValue("12");
    await page.unroute("**/v1/admin/settings");
    await page.route("**/v1/admin/settings", (route) =>
      route.request().method() === "PUT"
        ? route.fulfill({
            status: 409,
            json: { detail: "Settings changed; review and retry" },
          })
        : route.continue(),
    );
    await page.getByRole("button", { name: "Save number limits" }).click();
    await expect(
      page.getByText("Settings changed; review and retry"),
    ).toBeVisible();
    await expect(
      page.getByLabel("Maximum owned numbers", { exact: true }),
    ).toHaveValue("12");
    await page.unroute("**/v1/admin/settings");
    await page.getByLabel("Area code", { exact: true }).fill("650");
    await page.getByRole("button", { name: "Find available numbers" }).click();
    await page.getByRole("button", { name: "Review +16505550188" }).click();
    const approval = page.getByRole("region", { name: "Purchase approval" });
    await expect(approval).toContainText("$3.00 initial commitment");
    const buy = page.getByRole("button", { name: "Approve and order number" });
    await expect(buy).toBeDisabled();
    expect(bodies).toEqual([]);
    await page
      .getByLabel("I approve this number and its recurring rental")
      .check();
    await buy.click();
    await expect(
      page.getByRole("region", { name: "Number orders" }),
    ).toContainText("Uncertain");
    expect(bodies).toEqual([{ quote_id: quote.id, approve_recurring: true }]);
    await expect(
      page.getByRole("button", { name: "Check order status for +16505550188" }),
    ).toBeVisible();
  });
}
