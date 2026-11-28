import { expect, test } from "./fixtures";
import { goTo } from "./navigation";
import {
  answer,
  finishCall,
  leaveNoCall,
  prepareSimulationCall,
  startDialing,
} from "./simulation";

/** Call insights plan: what a call keeps, the R key, the script jump button, Settings and Review insights. */

async function enter(page: import("@playwright/test").Page) {
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  const sample = page.getByRole("button", {
    name: "Try a simulated workspace",
  });
  if (await sample.isVisible()) await sample.click();
}

test.afterEach(async ({ page }) => {
  await leaveNoCall(page);
});

test("the Dialer says what a stricter-state call keeps and R keeps the prospect's side", async ({
  page,
}) => {
  await enter(page);
  await prepareSimulationCall(page, "Recording prospect", "+14155550796");
  await startDialing(page);
  await answer(page);
  const status = page.getByRole("status", { name: "Recording" });
  await expect(status).toContainText("Recording your side");
  await expect(status).toContainText("Prospect audio not kept: CA");
  const consent = page.waitForRequest(
    (request) =>
      request.url().endsWith("/recording/consent") &&
      request.method() === "POST",
  );
  await page.keyboard.press("r");
  await consent;
  await expect(status).toContainText("Recording both sides");
  await expect(
    status.getByRole("button", { name: /They know it's recorded/ }),
  ).toHaveCount(0);
  await finishCall(page);
});

test("a far script part is offered as a button instead of a jump", async ({
  page,
}) => {
  await page.route("**/v1/calls/*", async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    const response = await route.fetch();
    const json = await response.json();
    const close = json.script_snapshot?.sections?.find(
      (section: { key: string }) => section.key === "close",
    );
    if (close && json.answered_at)
      json.script_suggestion = {
        key: `close:${close.options[0].id}`,
        sequence: 1,
      };
    await route.fulfill({ response, json });
  });
  await enter(page);
  await prepareSimulationCall(page, "Jump prospect", "+14155550797");
  await startDialing(page);
  await answer(page);
  const jump = page.getByTestId("script-suggestion");
  await expect(jump).toContainText("Jump to Close");
  await jump.click();
  await expect(jump).toHaveCount(0);
  await page.unroute("**/v1/calls/*");
  await finishCall(page);
});

test("Settings keeps the call recording rules and Review shows the test and exports", async ({
  page,
}) => {
  await page.route("**/v1/review/variants", (route) =>
    route.fulfill({
      json: {
        test: { id: "abc123def456", when: [] },
        by: "variant",
        enough_at: 30,
        groups: [],
        arms: [
          {
            variant: "direct-open",
            share: 50,
            hypothesis: "Naming the reason first gets past the opener.",
            results: null,
          },
          {
            variant: "permission-open",
            share: 50,
            hypothesis: "Asking for thirty seconds cuts early hang-ups.",
            results: null,
          },
        ],
      },
    }),
  );
  await page.route("**/v1/review/export", (route) =>
    route.fulfill({
      json: {
        path: "/tmp/Exports/opendialer-calls.zip",
        calls: 2,
        audio: 1,
        transcripts: 2,
        bytes: 100,
      },
    }),
  );
  await enter(page);
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  const card = page.getByRole("region", { name: "Call recording" });
  await expect(card).toContainText("Record my side");
  const live = card.getByLabel(
    "Transcribe the prospect live in stricter states",
  );
  await expect(live).toBeChecked();
  const saved = page.waitForRequest(
    (request) =>
      request.url().endsWith("/v1/admin/settings") &&
      request.method() === "PUT",
  );
  // The box follows the saved setting: it changes once the save comes back.
  await live.click();
  await saved;
  await expect(live).not.toBeChecked();
  await live.click();
  await expect(live).toBeChecked();
  await goTo(page, "Review");
  const insights = page.getByRole("region", { name: "Insights" });
  await expect(insights).toContainText("Script test");
  await expect(insights).toContainText(
    "Naming the reason first gets past the opener.",
  );
  await expect(
    insights.getByRole("row", { name: /direct-open/ }),
  ).toContainText("No calls yet");
  await insights.getByText("Export calls with audio and transcripts").click();
  await insights.getByRole("button", { name: "Export calls" }).click();
  await expect(
    page.getByText(/Exported 2 calls \(1 with audio\)/),
  ).toBeVisible();
});
