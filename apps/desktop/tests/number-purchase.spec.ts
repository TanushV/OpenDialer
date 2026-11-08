import { expect, test } from "./fixtures";

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
