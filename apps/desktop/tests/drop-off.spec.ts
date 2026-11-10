import { expect, test } from "./fixtures";
import { goTo } from "./navigation";

test("Review shows where answered calls end in the script", async ({
  page,
}) => {
  // D31, R11.6: stage drop-off is the one learning view kept after StatML was archived.
  await page.route("**/v1/review/drop-off", (route) =>
    route.fulfill({
      json: {
        answered: 5,
        placed: 4,
        window_days: 30,
        stages: [
          { stage: "Opening", calls: 2, outcomes: { not_interested: 2 } },
          { stage: "Pitch", calls: 1, outcomes: { callback_requested: 1 } },
          { stage: "Close", calls: 1, outcomes: { meeting_booked: 1 } },
        ],
      },
    }),
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
  await expect(page.getByRole("status", { name: "Call status" })).toBeVisible();
  await goTo(page, "Review");
  const card = page.getByRole("region", { name: "Where calls end" });
  await expect(card).toContainText("Answered calls in the last 30 days: 5");
  await expect(card).toContainText("4 show the script part they ended in");
  const opening = card.getByRole("row").filter({ hasText: "Opening" });
  await expect(opening).toContainText("2");
  await expect(opening).toContainText("2 Not interested");
  await expect(
    page.getByRole("region", { name: "What it has learned" }),
  ).toHaveCount(0);
});
