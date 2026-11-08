import { expect, test, type Page } from "./fixtures";
import {
  answer,
  finishCall,
  hangUp,
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
