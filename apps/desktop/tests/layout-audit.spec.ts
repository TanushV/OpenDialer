import { expect, test, type Page } from "./fixtures";
import {
  answer,
  finishCall,
  hangUp,
  prepareIncomingCall,
  prepareSimulationCall,
} from "./simulation";
import { coveredBy, drift, noSidewaysScroll } from "./layout";

/**
 * R1.3 layout audit: in every Dialer phase the simulation can reach, at both window sizes, the
 * primary action is inside the window, nothing covers it, it holds still, and the page never
 * scrolls sideways. Run in WebKit, the engine the Mac app uses.
 */
const SIZES = [
  { width: 1024, height: 720 },
  { width: 1440, height: 940 },
];
/** Steady-state movement allowed for the primary action, in pixels. */
const DRIFT_LIMIT = 4;

type Finding = { phase: string; problem: string };

/** Known failures, fixed in a later phase. Each stays listed until that fix lands; any other finding fails. */
const KNOWN: { phase: string; problem: RegExp; fix: string }[] = [];
const known = (f: Finding) =>
  KNOWN.some((k) => k.phase === f.phase && k.problem.test(f.problem));

async function audit(page: Page, phase: string, name: string) {
  const findings: Finding[] = [];
  const action = page.getByRole("button", { name, exact: true }).last();
  await expect(action).toBeVisible();
  const box = (await action.boundingBox())!;
  const view = page.viewportSize()!;
  if (box.y < 0 || box.y + box.height > view.height)
    findings.push({ phase, problem: `${name} is outside the window` });
  const cover = await coveredBy(action);
  if (cover) findings.push({ phase, problem: `${cover} covers ${name}` });
  const moved = await drift(action);
  if (moved > DRIFT_LIMIT)
    findings.push({ phase, problem: `${name} moved ${Math.round(moved)}px` });
  try {
    await noSidewaysScroll(page);
  } catch {
    findings.push({ phase, problem: "page scrolls sideways" });
  }
  return findings;
}

test.describe("compact desktop layout", () => {
  test.use({ viewport: { width: 1024, height: 720 } });

  const longName = "EnterpriseAccount".repeat(8);

  test("idle Dialer and Leads keep long list names inside their controls", async ({
    page,
  }) => {
    await page.route(/\/v1\/lists(?:\?|$)/, async (route) => {
      const response = await route.fetch();
      const data = await response.json();
      if (data.lists.length) data.lists[0].name = longName;
      await route.fulfill({ response, json: data });
    });
    await page.goto("/");
    await page
      .getByRole("button", { name: "Try a simulated workspace" })
      .click();
    await expect(
      page.getByRole("button", { name: "Start calling Space" }),
    ).toBeVisible();
    await expect(page.locator(".action-bar")).toBeHidden();

    const dialFrom = page.getByRole("region", { name: "Dial from" });
    await dialFrom
      .getByRole("checkbox", { name: "Dial from all active lists" })
      .uncheck();
    const check = dialFrom.getByRole("checkbox", {
      name: `Dial list ${longName}`,
    });
    await expect(check).toBeVisible();
    await expect
      .poll(() =>
        dialFrom.evaluate((card) => card.scrollWidth - card.clientWidth),
      )
      .toBe(0);

    await page
      .getByRole("navigation")
      .getByRole("button", { name: "Leads", exact: true })
      .click();
    const filter = page.getByRole("combobox", { name: "Filter leads by list" });
    await expect(
      filter.locator("option").filter({ hasText: longName }),
    ).toHaveCount(1);
    const search = page.getByRole("textbox", { name: "Search leads" });
    expect((await filter.boundingBox())!.width).toBeLessThan(350);
    await expect(search).toBeVisible();
    await page
      .getByRole("checkbox", { name: "Select all visible leads" })
      .check();
    const bulk = page.getByRole("region", { name: "Selected lead actions" });
    const action = (await bulk
      .getByRole("combobox", { name: "Lead action" })
      .boundingBox())!;
    const apply = (await bulk
      .getByRole("button", { name: "Apply to selected leads" })
      .boundingBox())!;
    expect(
      Math.abs(action.y + action.height - apply.y - apply.height),
    ).toBeLessThan(2);
    await noSidewaysScroll(page);
  });

  test("an incoming call keeps the full caller name and controls within the card", async ({
    page,
  }) => {
    const deliver = await prepareIncomingCall(page, {
      id: "compact-layout",
      state: "ringing",
      prospect: { name: longName, company: longName },
    });
    await page.goto("/");
    await page
      .getByRole("button", { name: "Try a simulated workspace" })
      .click();
    await deliver();
    const incoming = page.getByRole("region", { name: "Incoming call" });
    await expect(incoming.getByText(longName, { exact: false })).toBeVisible();
    await expect
      .poll(() =>
        incoming.evaluate((card) => card.scrollWidth - card.clientWidth),
      )
      .toBe(0);
    const answerButton = incoming.getByRole("button", {
      name: "Answer",
      exact: true,
    });
    const declineButton = incoming.getByRole("button", {
      name: "Decline",
      exact: true,
    });
    await expect(answerButton).toBeInViewport();
    await expect(declineButton).toBeInViewport();
    expect((await answerButton.boundingBox())!.height).toBeGreaterThanOrEqual(
      36,
    );
    await noSidewaysScroll(page);
  });
});

for (const size of SIZES) {
  test(`Dialer phases hold their layout at ${size.width}x${size.height}`, async ({
    page,
  }) => {
    await page.setViewportSize(size);
    await page.goto("/");
    await expect(page.getByText("Opening your workspace…")).toBeHidden();
    const sample = page.getByRole("button", {
      name: "Try a simulated workspace",
    });
    if (await sample.isVisible()) await sample.click();
    const findings: Finding[] = [];
    await prepareSimulationCall(
      page,
      "Bartholomew Alexander Fitzgerald-Montgomery of the Extended Enterprise Accounts Team",
      "+14155550782",
    );
    findings.push(...(await audit(page, "paused", "Start calling Space")));
    await page
      .getByRole("button", { name: "Start calling Space", exact: true })
      .click();
    findings.push(...(await audit(page, "dialing", "Skip Space")));
    await answer(page);
    findings.push(...(await audit(page, "talking", "Hang up Esc")));
    await hangUp(page).click();
    findings.push(...(await audit(page, "ended", "3 Not interested")));
    await page
      .getByRole("button", { name: "3 Not interested", exact: true })
      .click();
    findings.push(...(await audit(page, "saved", "Dial now Space")));
    await finishCall(page);
    expect(findings.filter((f) => !known(f))).toEqual([]);
    // The known shift appears only when analysis lands after the button, so its absence is
    // reported, not failed; Phase 5 removes the entry with its fix.
    for (const k of KNOWN)
      if (
        !findings.some((f) => f.phase === k.phase && k.problem.test(f.problem))
      )
        test.info().annotations.push({
          type: "known failure not seen",
          description: k.fix,
        });
  });
}

/** R10.10: every page, not only the Dialer: its main control is uncovered and still, and nothing scrolls sideways. */
const PAGES: [string, string][] = [
  ["Leads", "Search lead history"],
  ["Review", "Review"],
  ["Numbers", "Check my Telnyx numbers"],
  ["Script", "Save script"],
  ["Settings", "Save and check"],
];

for (const size of SIZES) {
  test(`every page holds its layout at ${size.width}x${size.height}`, async ({
    page,
  }) => {
    await page.setViewportSize(size);
    await page.goto("/");
    await expect(page.getByText("Opening your workspace…")).toBeHidden();
    const sample = page.getByRole("button", {
      name: "Try a simulated workspace",
    });
    if (await sample.isVisible()) await sample.click();
    const findings: Finding[] = [];
    for (const [title, control] of PAGES) {
      await page
        .getByRole("button", { name: title, exact: true })
        .first()
        .click();
      const target =
        control === title
          ? page.getByRole("heading", { name: title, exact: true })
          : page.getByRole("button", { name: control, exact: true }).first();
      await expect(target).toBeVisible();
      await target.scrollIntoViewIfNeeded();
      const cover = await coveredBy(target);
      if (cover)
        findings.push({ phase: title, problem: `${cover} covers ${control}` });
      const moved = await drift(target);
      if (moved > DRIFT_LIMIT)
        findings.push({
          phase: title,
          problem: `${control} moved ${Math.round(moved)}px`,
        });
      try {
        await noSidewaysScroll(page);
      } catch {
        findings.push({ phase: title, problem: "page scrolls sideways" });
      }
    }
    expect(findings).toEqual([]);
  });
}
