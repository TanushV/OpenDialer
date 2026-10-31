import { expect, test, type Page } from "./fixtures";
import { chooseOutcome } from "./simulation";

async function enter(page: Page) {
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  const sample = page.getByRole("button", {
    name: "Try a simulated workspace",
  });
  if (await sample.isVisible()) await sample.click();
  await page.getByRole("button", { name: "Review", exact: true }).click();
}

test("personal goals survive reload and sit in the coaching list", async ({
  page,
}) => {
  await enter(page);
  const panel = page.getByRole("region", { name: "Coaching" });
  await panel
    .getByRole("textbox", { name: "Add a goal" })
    .fill("Clarify ownership before pitching");
  await panel.getByRole("button", { name: "Add goal", exact: true }).click();
  await expect(
    panel.getByText("Clarify ownership before pitching", { exact: true }),
  ).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: "Review", exact: true }).click();
  await expect(
    panel.getByText("Clarify ownership before pitching", { exact: true }),
  ).toBeVisible();
  await panel.getByRole("button", { name: "Done", exact: true }).click();
  await expect(
    panel.getByText("Clarify ownership before pitching", { exact: true }),
  ).toBeHidden();
});

test("practice is a list of what to work on and how, with no AI rehearsal", async ({
  page,
}) => {
  await page.route("**/v1/coaching", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    data.improvement_groups = [
      {
        id: "pricing-too-early",
        concept: "Pricing came up before discovery",
        approach: "Ask two discovery questions before naming a price.",
        count: 3,
        examples: [],
      },
    ];
    await route.fulfill({ response, json: data });
  });
  await enter(page);
  const panel = page.getByRole("region", { name: "Coaching" });
  await expect(
    panel.getByText("Pricing came up before discovery", { exact: true }),
  ).toBeVisible();
  await expect(
    panel.getByText("How: Ask two discovery questions before naming a price.", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    panel.getByText("Seen in 3 calls", { exact: true }),
  ).toBeVisible();
  await expect(panel.getByRole("button", { name: "Practice" })).toHaveCount(0);
  await expect(
    panel.getByRole("textbox", { name: "Practice scenario" }),
  ).toHaveCount(0);
});

test("a reviewed coaching tip can be published and withdrawn with its source version", async ({
  page,
}) => {
  let published = false;
  const example = {
    id: "reviewed-example",
    version: 1,
    text: "Clarify ownership first.",
    raw_evidence_status: "expired",
  };
  await page.route("**/v1/calls/*/analysis", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    body.call.transcript_version = 7;
    body.call_memory = {
      coaching_points: [
        { id: "tip-one", text: example.text, status: "confirmed" },
      ],
    };
    await route.fulfill({ response, json: body });
  });
  await page.route("**/v1/coaching", async (route) => {
    const response = await route.fetch();
    await route.fulfill({
      response,
      json: {
        ...(await response.json()),
        examples: published ? [example] : [],
      },
    });
  });
  await page.route("**/v1/admin/snippets", async (route) => {
    const payload = route.request().postDataJSON();
    expect(payload.coaching_point_id).toBe("tip-one");
    expect(payload.expected_transcript_version).toBe(7);
    expect(payload.review_reference).toBe("Reviewed procurement clarification");
    expect(payload.text).toBeUndefined();
    published = true;
    await route.fulfill({ json: example });
  });
  await page.route("**/v1/admin/snippets/reviewed-example", async (route) => {
    expect(route.request().postDataJSON()).toEqual({
      expected_version: 1,
      approved: false,
    });
    published = false;
    await route.fulfill({ json: { ...example, approved: false, version: 2 } });
  });
  await enter(page);
  // Create a real simulation call so this UI contract does not depend on earlier tests.
  await page.getByRole("button", { name: "Dialer", exact: true }).click();
  await page.getByRole("button", { name: "Go ready", exact: true }).click();
  await page
    .getByRole("button", { name: "Get next lead", exact: true })
    .click();
  await page.getByRole("button", { name: "Start call", exact: true }).click();
  await page.getByRole("button", { name: "Hang up", exact: true }).click();
  await chooseOutcome(page, "no_answer");
  await page.getByRole("button", { name: "Save outcome", exact: true }).click();
  await page
    .getByRole("button", { name: "Pause dialing", exact: true })
    .click();
  await page.getByRole("button", { name: "Review", exact: true }).click();
  await page
    .getByRole("button", { name: /^Review call / })
    .first()
    .click();
  await page
    .getByRole("button", { name: "Review for team", exact: true })
    .click();
  await page
    .getByRole("textbox", { name: "Example review reference", exact: true })
    .fill("Reviewed procurement clarification");
  await page
    .getByRole("button", { name: "Publish reviewed example", exact: true })
    .click();
  await expect(
    page.getByText("Published as a reviewed example", { exact: false }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "All conversations", exact: true })
    .click();
  const panel = page.getByRole("region", { name: "Coaching" });
  await expect(panel.getByText(example.text, { exact: true })).toBeVisible();
  await panel.getByRole("button", { name: "Withdraw", exact: true }).click();
  await expect(panel.getByText(example.text, { exact: true })).toBeHidden();
});
