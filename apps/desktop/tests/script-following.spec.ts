import { AUTHORITY, expect, setUpAuthority, test } from "./fixtures";
import { clickUntil, goTo } from "./navigation";
import {
  answer,
  hangUp,
  pauseCalling,
  saveOutcome,
  startCalling,
} from "./simulation";
import { readFile } from "node:fs/promises";

test("the Dialer follows spoken lines after a manual scroll and exports the actual route", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1024, height: 720 });
  await setUpAuthority(page);
  await page.goto("/");
  const local = await page.request.post(`${AUTHORITY}/v1/local/session`, {
    headers: { "X-Local-Token": "e2e-local-token" },
    data: {},
  });
  const headers = {
    authorization: `Bearer ${(await local.json()).access_token}`,
  };
  const base = await (
    await page.request.get(`${AUTHORITY}/v1/optimization/script`, { headers })
  ).json();
  const discovery =
    "What part of your customer follow-up process takes the most time each week?";
  const custom =
    "Where do orders get stuck when the warehouse sends them to your dispatch team?";
  const close =
    "Would it help to review the entire dispatch workflow next Tuesday?";
  const markdown = [
    "# Opening\nHi {first_name}, this is {rep} with {seller}.",
    `# Discovery\n${discovery}`,
    `# Dispatch\n${custom}`,
    `# Pitch\n${"We check where an order gets delayed and what is needed to move it along. ".repeat(25)}`,
    `# Close\n${close}`,
  ].join("\n");
  const response = await page.request.post(
    `${AUTHORITY}/v1/admin/script/import`,
    {
      headers,
      data: { markdown, expected_version: base.base.version, preview: false },
    },
  );
  expect(response.ok()).toBeTruthy();
  const titles = ["Opening", "Discovery", "Dispatch", "Pitch", "Close"];
  await goTo(page, "Script");
  await expect(
    page
      .locator(".script-outline h3")
      .filter({ hasText: /^(Opening|Discovery|Dispatch|Pitch|Close)$/ }),
  ).toHaveText(titles);
  await goTo(page, "Dialer");
  const placed = page.waitForResponse(
    (r) => r.url().endsWith("/v1/calls/start") && r.ok(),
  );
  await startCalling(page).click();
  const call = await (await placed).json();
  await answer(page);
  const script = page.getByTestId("prepared-script");
  await expect(script.locator(".script-reader > section > h3")).toHaveText(
    titles,
  );
  const current = script.locator('[aria-current="step"]');
  let segment = 0;
  async function say(text: string, side = "rep") {
    const updated = page.waitForResponse(
      (r) => r.url().endsWith(`/v1/calls/${call.id}`) && r.ok(),
    );
    const result = await page.request.post(
      `${AUTHORITY}/v1/calls/${call.id}/transcript`,
      {
        headers,
        data: {
          segment_id: `spoken-${++segment}`,
          revision: 1,
          side,
          text,
          is_final: true,
        },
      },
    );
    expect(result.ok()).toBeTruthy();
    await updated;
  }
  await say(discovery);
  await expect(current).toContainText(discovery);
  await script.evaluate((element) => {
    element.scrollTop = element.scrollHeight;
  });
  const manualPosition = await script.evaluate((element) => element.scrollTop);
  expect(manualPosition).toBeGreaterThan(50);
  await say(close, "remote");
  await say("Anyway I need to pick up a package at my apartment.");
  await expect(current).toContainText(discovery);
  expect(await script.evaluate((element) => element.scrollTop)).toBe(
    manualPosition,
  );
  await say(discovery);
  await expect
    .poll(() => script.evaluate((element) => element.scrollTop))
    .toBeLessThan(manualPosition - 30);
  await say(custom);
  await expect(current).toContainText(custom);
  await say(discovery);
  await expect(current).toContainText(discovery);
  await pauseCalling(page).click();
  await hangUp(page).click();
  await saveOutcome(page, "not_interested");
  await goTo(page, "Review");
  const exportButton = page.getByRole("button", {
    name: "Export call statistics",
  });
  const downloadPromise = page.waitForEvent("download", { timeout: 25000 });
  let requested = false;
  page.on("request", (request) => {
    if (request.url().includes("/v1/review/call-statistics/export"))
      requested = true;
  });
  await clickUntil(exportButton, async () => {
    await expect.poll(() => requested, { timeout: 2500 }).toBe(true);
  });
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe("opendialer-call-statistics.zip");
  const bytes = await readFile((await download.path())!);
  expect(bytes.subarray(0, 2).toString()).toBe("PK");
  expect(bytes.includes(Buffer.from("script-events.csv"))).toBeTruthy();
});
