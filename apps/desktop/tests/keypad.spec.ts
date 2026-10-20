import { expect, test } from "@playwright/test";
import { chooseOutcome } from "./simulation";

test("keypad preserves a lost-response identity through reload and distinguishes a new press", async ({
  page,
}) => {
  const posts: Record<string, string>[] = [];
  await page.route("**/v1/calls/*/dtmf", async (route) => {
    posts.push(route.request().postDataJSON());
    if (posts.length === 1) await route.abort("failed");
    else
      await route.fulfill({
        json: {
          command_id: `tone-${posts.length}`,
          status: "completed",
          sent: true,
        },
      });
  });
  await page.route("**/v1/calls/*/dtmf/*", async (route) =>
    route.fulfill({ status: 404, json: { detail: "Request not received" } }),
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
  await page.getByRole("button", { name: "Go ready" }).click();
  await page
    .getByRole("button", { name: "Get next lead", exact: true })
    .click();
  await page.getByRole("button", { name: "Start call", exact: true }).click();
  await page.getByRole("button", { name: /Keypad$/ }).click();
  await page
    .locator(".keypad-popover")
    .getByRole("button", { name: "4", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Retry same keypad request" }),
  ).toBeVisible();
  expect(posts[0].idempotency_key).toBeTruthy();
  await page.reload();
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  await page.getByRole("button", { name: /Keypad$/ }).click();
  await page.getByRole("button", { name: "Retry same keypad request" }).click();
  await expect(
    page.getByText("Keypad request acknowledged", { exact: true }),
  ).toBeVisible();
  expect(posts[1]).toEqual(posts[0]);
  await page
    .locator(".keypad-popover")
    .getByRole("button", { name: "4", exact: true })
    .click();
  await expect.poll(() => posts.length).toBe(3);
  expect(posts[2].idempotency_key).not.toEqual(posts[0].idempotency_key);
  await page.getByRole("button", { name: "Hang up", exact: true }).click();
  await chooseOutcome(page, "no_answer");
  await page.getByRole("button", { name: "Save outcome", exact: true }).click();
  await page
    .getByRole("button", { name: "Pause dialing", exact: true })
    .click();
});

test("uncertain keypad lets the rep explicitly resume after checking the live menu", async ({
  page,
}) => {
  let submissions = 0;
  await page.route("**/v1/calls/*/dtmf", async (route) => {
    submissions++;
    await route.fulfill({
      json: {
        command_id: `tone-${submissions}`,
        status: "review_required",
        sent: false,
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
  await page.getByRole("button", { name: "Go ready" }).click();
  await page
    .getByRole("button", { name: "Get next lead", exact: true })
    .click();
  await page.getByRole("button", { name: "Start call", exact: true }).click();
  await page.getByRole("button", { name: /Keypad$/ }).click();
  const digit = page
    .getByRole("region", { name: "Call keypad" })
    .getByRole("button", { name: "2", exact: true });
  await digit.click();
  await expect(digit).toBeDisabled();
  await page
    .getByRole("button", { name: "I checked the menu; allow a new key" })
    .click();
  await expect(digit).toBeEnabled();
  expect(submissions).toBe(1);
  await page.getByRole("button", { name: "Hang up", exact: true }).click();
  await chooseOutcome(page, "no_answer");
  await page.getByRole("button", { name: "Save outcome", exact: true }).click();
  await page
    .getByRole("button", { name: "Pause dialing", exact: true })
    .click();
});
