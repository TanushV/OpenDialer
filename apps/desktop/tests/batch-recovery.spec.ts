import { expect, test } from "./fixtures";

test("deferred deadline is explicit and a lost response keeps the same request", async ({
  page,
}) => {
  const requests: { ready_at: string; idempotency_key: string }[] = [];
  await page.route("**/v1/admin/batches", (route) =>
    route.fulfill({ json: { batches: [], limited: false } }),
  );
  await page.route("**/v1/admin/batches/scripts", async (route) => {
    requests.push(route.request().postDataJSON());
    await route.fulfill({
      status: 503,
      json: { detail: "Original submission uncertain" },
    });
  });
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
  await page.getByRole("button", { name: "Intelligence", exact: true }).click();
  const deadline = page.getByLabel("Scripts needed by", { exact: true });
  await expect(deadline).toBeVisible();
  const submit = page.getByRole("button", {
    name: "Submit deferred scripts",
    exact: true,
  });
  await submit.click();
  await expect(
    page.getByText("Original submission uncertain", { exact: true }),
  ).toBeVisible();
  await expect(deadline).toBeDisabled();
  await submit.click();
  await expect.poll(() => requests.length).toBe(2);
  expect(requests[0]).toEqual(requests[1]);
  expect(Date.parse(requests[0].ready_at)).toBeGreaterThan(
    Date.now() + 24 * 3600000,
  );
});

test("deferred preparation recovers an original receipt after reload without resubmitting", async ({
  page,
}) => {
  let saved = false;
  let submissions = 0;
  const posts: unknown[] = [];
  await page.route("**/v1/admin/batches", (route) =>
    route.fulfill({
      json: {
        batches: [
          {
            id: "original-intent",
            version: 3,
            status: saved ? "in_progress" : "uncertain",
            provider_batch_id: saved ? "batch-original" : null,
            lead_count: 2,
            published: [],
            retry_lead_ids: [],
            can_reconcile: true,
            reason:
              "Original submission may have been charged. Verify its provider receipt.",
          },
        ],
        limited: false,
      },
    }),
  );
  await page.route("**/v1/admin/batches/scripts", (route) => {
    submissions++;
    return route.fulfill({
      status: 503,
      json: { detail: "Lost submission response" },
    });
  });
  await page.route(
    "**/v1/admin/batches/original-intent/reconcile",
    async (route) => {
      const body = route.request().postDataJSON();
      posts.push(body);
      expect(body).toEqual({
        expected_version: 3,
        provider_batch_id: "batch-original",
      });
      if (posts.length === 1)
        await route.fulfill({
          status: 503,
          json: { detail: "Provider status unavailable" },
        });
      else {
        saved = true;
        await route.fulfill({
          json: { status: "in_progress", published: [], retry_lead_ids: [] },
        });
      }
    },
  );
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
  await page.getByRole("button", { name: "Intelligence", exact: true }).click();
  const panel = page.getByRole("region", {
    name: "Deferred script preparation",
    exact: true,
  });
  await expect(panel).toContainText(
    "Original submission may have been charged",
  );
  expect(submissions).toBe(0);
  await panel.getByLabel("Original provider batch ID").fill("batch-original");
  const verify = panel.getByRole("button", {
    name: "Verify and publish original batch",
    exact: true,
  });
  await verify.click();
  await expect(panel).toContainText("Provider status unavailable");
  await expect(verify).toBeEnabled();
  await verify.click();
  await expect(panel).toContainText("batch-original");
  await page.reload();
  await page.getByRole("button", { name: "Intelligence", exact: true }).click();
  await expect(panel).toContainText("batch-original");
  expect(posts).toHaveLength(2);
  expect(submissions).toBe(0);
});

test("partial deferred preparation retries only unfinished objects", async ({
  page,
}) => {
  const calls: unknown[] = [];
  await page.route("**/v1/admin/batches", (route) =>
    route.fulfill({
      json: {
        batches: [
          {
            id: "partial-batch",
            version: 8,
            status: "completed",
            provider_batch_id: "batch-partial",
            lead_count: 3,
            published: ["already-ready"],
            retry_lead_ids: ["needs-one", "needs-two"],
            can_reconcile: true,
            reason: "",
          },
        ],
        limited: false,
      },
    }),
  );
  await page.route("**/v1/research/batches", async (route) => {
    calls.push(route.request().postDataJSON());
    await route.fulfill({ json: { queued: 1 } });
  });
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
  await page.getByRole("button", { name: "Intelligence", exact: true }).click();
  const panel = page.getByRole("region", {
    name: "Deferred script preparation",
    exact: true,
  });
  const retry = panel.getByRole("button", {
    name: "Prepare only unfinished leads",
    exact: true,
  });
  await expect(retry).toBeDisabled();
  await panel
    .getByLabel("Authorize new budgeted preparation for these unfinished leads")
    .check();
  await retry.click();
  await expect(panel).toContainText(
    "Unfinished leads queued for new preparation",
  );
  expect(calls).toEqual([{ lead_ids: ["needs-one", "needs-two"] }]);
});

test("a confirmed completed batch permits a new preparation identity", async ({
  page,
}) => {
  const keys: string[] = [];
  await page.route("**/v1/admin/batches", (route) =>
    route.fulfill({ json: { batches: [], limited: false } }),
  );
  await page.route("**/v1/admin/batches/scripts", (route) => {
    keys.push(route.request().postDataJSON().idempotency_key);
    return route.fulfill({
      json: {
        id: `complete-${keys.length}`,
        status: "completed",
        published: [],
        retry_lead_ids: [],
      },
    });
  });
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
  await page.getByRole("button", { name: "Intelligence", exact: true }).click();
  const submit = page.getByRole("button", {
    name: "Submit deferred scripts",
    exact: true,
  });
  await submit.click();
  await expect.poll(() => keys.length).toBe(1);
  await expect(submit).toBeEnabled();
  await submit.click();
  await expect.poll(() => keys.length).toBe(2);
  expect(keys[0]).not.toBe(keys[1]);
  await expect(
    page.getByLabel("Scripts needed by", { exact: true }),
  ).toBeEnabled();
});
