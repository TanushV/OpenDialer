import { expect, test } from "./fixtures";
import { importLead } from "./simulation";

for (const connected of [false, true]) {
  test(`sparse support and reviewed model evidence ${connected ? "postgres" : "local"}`, async ({
    page,
  }) => {
    await page.route("**/v1/workspace", async (route) => {
      const result = await route.fetch();
      const value = await result.json();
      if (value.models)
        value.models = [
          {
            id: "review-fixture",
            checksum: "a".repeat(64),
            active: false,
            feature_schema_version: 2,
            reviewable: false,
            goal: "meeting_held",
            window_days: 14,
            metrics: { brier: 0.2, pooled_baseline_brier: 0.21 },
            objective_stage: "human",
            bootstrap: { groups: 45, interval: [-0.02, 0.01] },
            pooled_stages: [
              "right_party_given_human",
              "conversion_given_right_party",
            ],
            conversation_ablation: { eligible_for_serving: false },
            rolling_evaluations: [1, 2, 3].map((i) => ({
              training_cutoff: `2026-0${i}-01T00:00:00Z`,
              calibration_cutoff: `2026-0${i + 1}-01T00:00:00Z`,
              evaluation_cutoff: `2026-0${i + 2}-01T00:00:00Z`,
              stages: {
                human: {
                  status: "fitted",
                  support: {
                    train: { positive: 40, negative: 60 },
                    calibration: { positive: 12, negative: 14 },
                    validation: { positive: 20, negative: 22 },
                  },
                },
              },
            })),
          },
        ];
      await route.fulfill({ response: result, json: value });
    });
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
    await expect(page.getByText("SIMULATION", { exact: true })).toBeVisible();
    await importLead(
      page,
      `Strategy fixture ${connected ? "postgres" : "local"}`,
      connected ? "+14155550710" : "+14155550709",
    );
    if (await page.getByRole("button", { name: "Go ready" }).isVisible())
      await page.getByRole("button", { name: "Go ready" }).click();
    await page
      .getByRole("button", { name: "Get next lead", exact: true })
      .click();
    await page.getByText("Estimates & uncertainty", { exact: true }).click();
    await expect(
      page.getByText("Estimates frozen at this selection.", { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByText(
        "Observed-rate intervals describe classified history, not certainty about this prospect.",
      ),
    ).toBeVisible();
    await expect(
      page.getByRole("table", { name: "Conditional estimate support" }),
    ).toBeVisible();
    await page
      .getByRole("button", { name: "Pause dialing", exact: true })
      .click();
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await page.getByText("Advanced", { exact: true }).click();
    // Starting weights are read-only assumptions; changing them needs re-evaluation.
    await page
      .getByText("Sparse estimate assumptions", { exact: true })
      .click();
    await expect(page.getByTestId("estimate-assumptions")).toContainText(
      "Human pickup prior 2 successes / 8 failures",
    );
    await expect(
      page.getByLabel("Human pickup prior successes", { exact: true }),
    ).toHaveCount(0);
    await page
      .getByRole("combobox", { name: "Pilot candidate", exact: true })
      .selectOption("review-fixture");
    await expect(
      page.getByText("Predictive evidence: not reviewable", { exact: true }),
    ).toBeVisible();
    await page
      .getByLabel("Experiment review reference", { exact: true })
      .fill("Insufficient evidence");
    await expect(
      page.getByRole("button", {
        name: "Start bounded model pilot",
        exact: true,
      }),
    ).toBeDisabled();
    await expect(
      page.getByText(
        "This candidate's predictive evidence is not reviewable, so it cannot start a pilot.",
        { exact: true },
      ),
    ).toBeVisible();
    await page.getByText("Model evaluation", { exact: true }).click();
    // Raw rolling-support and calibration tables stay in the model record.
    await expect(
      page.getByRole("table", { name: "Rolling evaluation support" }),
    ).toHaveCount(0);
    await expect(
      page.getByText(
        "Conversation features: excluded after held-out comparison",
        { exact: true },
      ),
    ).toBeVisible();
  });
}

test("reviewed finite approaches keep freeform help and require a source for adaptation", async ({
  page,
}) => {
  const writes: Record<string, unknown>[] = [];
  await page.route("**/v1/optimization/strategies", (route) =>
    route.fulfill({
      json: {
        strategies: [
          {
            id: "control",
            source_version: 1,
            text: "Ordinary contextual help",
          },
          {
            id: "reviewed-approach",
            source_version: 3,
            text: "Clarify decision ownership",
          },
        ],
      },
    }),
  );
  await page.route("**/v1/admin/optimization/comparisons", async (route) => {
    writes.push(route.request().postDataJSON());
    await route.fulfill({ json: { id: "ui-protocol" } });
  });
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  const sample = page.getByRole("button", {
    name: "Try a simulated workspace",
  });
  if (await sample.isVisible()) await sample.click();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByText("Advanced", { exact: true }).click();
  await page.getByLabel("Experiment type").selectOption("approach");
  await page
    .getByRole("combobox", { name: "Reviewed response approach", exact: true })
    .selectOption("reviewed-approach");
  await page
    .getByLabel("Experiment review reference", { exact: true })
    .fill("Reviewed approach protocol");
  await expect(
    page.getByText(
      "Wording stays freeform. Control receives ordinary contextual help.",
      { exact: true },
    ),
  ).toBeVisible();
  await page
    .getByRole("button", {
      name: "Start reviewed strategy comparison",
      exact: true,
    })
    .click();
  await expect.poll(() => writes.length).toBe(1);
  expect(writes[0].strategy_ids).toEqual(["control", "reviewed-approach"]);
  expect(writes[0].strategy_policy).toBe("two_arm");
  expect(writes[0].candidate_probability).toBe(0.5);
  expect(writes[0].request_id).toMatch(/^[0-9a-f-]{36}$/);
  await page
    .getByRole("combobox", { name: "Strategy allocation", exact: true })
    .selectOption("adaptive");
  await expect(
    page.getByRole("button", {
      name: "Start reviewed strategy comparison",
      exact: true,
    }),
  ).toBeDisabled();
  await expect(
    page.getByText(
      "Adaptive allocation requires a supported completed comparison for this approach.",
      { exact: true },
    ),
  ).toBeVisible();
});
