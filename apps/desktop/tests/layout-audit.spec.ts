import { expect, test, type Page } from "./fixtures";
import { chooseOutcome, prepareSimulationCall } from "./simulation";
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

/**
 * Known failures, fixed in a later phase. Each stays listed until that fix lands (plan
 * section 13); any other finding fails the audit.
 */
const KNOWN = [
  {
    phase: "ended",
    problem: /^Save outcome moved|covers Save outcome$/,
    fix: "R5.3: the wrap-up grows as its analysis loads; reserved space fixes it",
  },
];
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
    await expect(page.getByRole("button", { name: "Go ready" })).toBeVisible();
    findings.push(...(await audit(page, "paused", "Go ready")));
    await prepareSimulationCall(page, "Layout audit", "+14155550782");
    findings.push(...(await audit(page, "ready", "Next call Space")));
    await page
      .getByRole("button", { name: "Next call Space", exact: true })
      .click();
    findings.push(...(await audit(page, "in conversation", "Hang up")));
    await page.getByRole("button", { name: "Hang up", exact: true }).click();
    findings.push(...(await audit(page, "ended", "Save outcome")));
    await chooseOutcome(page, "no_answer");
    await page
      .getByRole("button", { name: "Save outcome", exact: true })
      .click();
    await expect(
      page.getByRole("button", { name: "Save outcome", exact: true }),
    ).toBeHidden();
    findings.push(...(await audit(page, "saved", "Next call Space")));
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
