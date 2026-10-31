import { expect, test } from "./fixtures";
import { openScriptSection } from "./navigation";

test("reviewed business knowledge and current version survive publication and reload", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  const sample = page.getByRole("button", {
    name: "Try a simulated workspace",
  });
  if (await sample.isVisible()) await sample.click();
  await openScriptSection(page, "What you sell");
  await page
    .getByRole("textbox", {
      name: "Product and pricing knowledge",
      exact: true,
    })
    .fill(
      "Workflow training with three guided onboarding sessions. Pricing is confirmed by the rep.",
    );
  await page
    .getByRole("textbox", { name: "Approved factual claims", exact: true })
    .fill(
      "We provide workflow training.\nThree guided onboarding sessions are included.",
    );
  await page
    .getByRole("textbox", { name: "Limits and prohibited claims", exact: true })
    .fill("Do not promise a revenue increase.");
  await page
    .getByRole("textbox", { name: "Opener", exact: true })
    .fill(
      "Hello from Example Seller. Is this a good time for a quick introduction?",
    );
  await page
    .getByRole("textbox", { name: "Publication reason", exact: true })
    .fill("Reviewed product context for calling");
  await page
    .getByRole("button", { name: "Publish reviewed version", exact: false })
    .click();
  await expect(
    page.getByText(
      "New playbook version published. Active call scripts stay stable.",
      { exact: true },
    ),
  ).toBeVisible();
  await page.reload();
  await openScriptSection(page, "What you sell");
  await expect(
    page.getByRole("textbox", {
      name: "Product and pricing knowledge",
      exact: true,
    }),
  ).toHaveValue(
    "Workflow training with three guided onboarding sessions. Pricing is confirmed by the rep.",
  );
  await expect(
    page.getByRole("textbox", { name: "Approved factual claims", exact: true }),
  ).toHaveValue(
    "We provide workflow training.\nThree guided onboarding sessions are included.",
  );
  await expect(
    page.getByRole("textbox", {
      name: "Limits and prohibited claims",
      exact: true,
    }),
  ).toHaveValue("Do not promise a revenue increase.");
  await expect(
    page.getByRole("textbox", { name: "Opener", exact: true }),
  ).toHaveValue(
    "Hello from Example Seller. Is this a good time for a quick introduction?",
  );
});

for (const failure of ["conflict", "lost response", "failed refresh"]) {
  test(`knowledge ${failure} recovers the current publication without overwriting it`, async ({
    page,
  }) => {
    await page.goto("/");
    await expect(page.getByText("Opening your workspace…")).toBeHidden();
    const sample = page.getByRole("button", {
      name: "Try a simulated workspace",
    });
    if (await sample.isVisible()) await sample.click();
    await openScriptSection(page, "What you sell");
    const panel = page.getByRole("region", { name: "Business knowledge" });
    let saved = false;
    let version: Record<string, unknown>;
    let failRefresh = failure === "failed refresh";
    await page.route("**/v1/workspace", async (route) => {
      if (saved && failRefresh) {
        failRefresh = false;
        await route.fulfill({
          status: 503,
          json: { detail: "Temporary refresh outage" },
        });
        return;
      }
      const response = await route.fetch();
      const data = await response.json();
      if (saved) data.active_playbook = version;
      await route.fulfill({ response, json: data });
    });
    await page.route("**/v1/admin/playbooks", async (route) => {
      expect(saved).toBe(false); // Reload must not submit the uncertain publication again.
      const data = route.request().postDataJSON();
      version = {
        ...data,
        id: "newly-published-knowledge",
        version: data.expected_previous_version + 1,
        product_description:
          failure === "conflict"
            ? "Another admin's reviewed current knowledge."
            : "This saved product knowledge.",
      };
      saved = true;
      if (failure === "conflict") {
        await route.fulfill({
          status: 409,
          json: {
            detail:
              "Business knowledge changed; reload and review the current version",
          },
        });
      } else if (failure === "lost response") {
        await route.abort("failed");
      } else {
        await route.fulfill({ json: version });
      }
    });
    try {
      await panel
        .getByRole("textbox", {
          name: "Product and pricing knowledge",
          exact: true,
        })
        .fill("This saved product knowledge.");
      await panel
        .getByRole("textbox", { name: "Publication reason", exact: true })
        .fill("Updated approved context");
      await panel
        .getByRole("button", { name: "Publish reviewed version" })
        .click();
      await expect(
        panel.getByRole("button", { name: "Reload current knowledge" }),
      ).toBeEnabled();
      await panel
        .getByRole("button", { name: "Reload current knowledge" })
        .click();
      await expect(
        panel.getByRole("textbox", {
          name: "Product and pricing knowledge",
          exact: true,
        }),
      ).toHaveValue(
        failure === "conflict"
          ? "Another admin's reviewed current knowledge."
          : "This saved product knowledge.",
      );
      await panel
        .getByRole("textbox", { name: "Publication reason", exact: true })
        .fill("Next considered edit");
      await expect(
        panel.getByRole("button", { name: "Publish reviewed version" }),
      ).toBeEnabled();
    } finally {
      await page.unrouteAll({ behavior: "wait" });
    }
  });
}

test("published knowledge withdraws obsolete live advice without changing the active script", async ({
  page,
}) => {
  let published = false;
  let headers: Record<string, string> = {};
  let current: Record<string, unknown> = {};
  let call: Record<string, unknown> = {};
  const advice = "This advice uses the old product context.";
  await page.route("**/v1/workspace", async (route) => {
    headers = await route.request().allHeaders();
    const response = await route.fetch();
    const data = await response.json();
    current = data.active_playbook;
    call = {
      id: "knowledge-live-call",
      version: 1,
      // A real call always carries its transcript version; updates compare it.
      transcript_version: 0,
      lead_id: data.leads[0].id,
      rep_id: data.user.id,
      state: "HUMAN",
      transcript: [],
      created_at: new Date().toISOString(),
    };
    data.calls = [call];
    await route.fulfill({ response, json: data });
  });
  await page.route("**/v1/calls/knowledge-live-call", (route) =>
    route.fulfill({
      json: {
        ...call,
        coach: published
          ? undefined
          : {
              id: "old-knowledge-advice",
              say_or_ask: advice,
              remaining_ms: 300000,
              raw_expires_at: new Date(Date.now() + 300000).toISOString(),
              expires_at: new Date(Date.now() + 300000).toISOString(),
            },
      },
    }),
  );
  try {
    await page.goto("/");
    await expect(page.getByText("Opening your workspace…")).toBeHidden();
    const sample = page.getByRole("button", {
      name: "Try a simulated workspace",
    });
    if (await sample.isVisible()) await sample.click();
    await expect(page.getByText(advice, { exact: true })).toBeVisible();
    // Capture the call's loaded script, not the placeholder shown until its
    // packet arrives: a late first packet is not a mid-call replacement.
    await expect(
      page.getByTestId("prepared-script").locator(".card-eyebrow .pill"),
    ).not.toHaveText("Approved playbook");
    const script = await page.getByTestId("prepared-script").innerText();
    published = true;
    const result = await page.request.post(
      "http://127.0.0.1:8765/v1/admin/playbooks",
      {
        headers: { authorization: headers.authorization },
        data: {
          name: "Updated seller knowledge",
          opener: current.opener,
          screening_intro: current.screening_intro,
          // Keep the approved examples: later specs share this authority and
          // its cached-knowledge tips.
          claims: current.claims ?? [],
          cards: current.cards ?? [],
          product_description: "Updated business capabilities.",
          prohibited_claims: current.prohibited_claims ?? [],
          approved: true,
          reason: "Reviewed new product facts",
          expected_previous_id: current.id,
          expected_previous_version: current.version,
        },
      },
    );
    expect(result.status()).toBe(200);
    await expect(page.getByText(advice, { exact: true })).toBeHidden();
    expect(await page.getByTestId("prepared-script").innerText()).toBe(script);
  } finally {
    await page.unrouteAll({ behavior: "wait" });
  }
});
