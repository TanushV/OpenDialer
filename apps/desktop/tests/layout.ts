import { expect, type Locator, type Page } from "@playwright/test";

/**
 * Layout checks for the audit (R1.3) and the hotfix (R0.12), made where the app leaves the page:
 * no test scrolling, so what the rep would see is what is checked.
 */

/** Something other than `target` is on top at any of 15 points across it: top, middle, bottom. */
export async function coveredBy(target: Locator) {
  const box = await target.boundingBox();
  expect(box, "target has no box").not.toBeNull();
  const points = [3, box!.height / 2, box!.height - 3].flatMap((dy) =>
    [0.1, 0.3, 0.5, 0.7, 0.9].map((fx) => ({
      x: box!.x + box!.width * fx,
      y: box!.y + dy,
    })),
  );
  return target.evaluate((element, points) => {
    for (const point of points) {
      const hit = document.elementFromPoint(point.x, point.y);
      if (hit && (element === hit || element.contains(hit))) continue;
      const owner = hit?.closest("[class]");
      return owner
        ? `${owner.tagName.toLowerCase()}.${owner.className}`
        : "outside the window";
    }
    return "";
  }, points);
}

export async function uncovered(target: Locator) {
  expect(await coveredBy(target), `covers ${await target.textContent()}`).toBe(
    "",
  );
}

/** The page never scrolls sideways. */
export async function noSidewaysScroll(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - window.innerWidth,
  );
  expect(overflow, "sideways scroll in pixels").toBeLessThanOrEqual(0);
}

/** Largest movement of `target` over `ms`, sampled every 150 ms, in pixels. */
export async function drift(target: Locator, ms = 1500) {
  const first = await target.boundingBox();
  let most = 0;
  for (let waited = 0; waited < ms; waited += 150) {
    await target.page().waitForTimeout(150);
    const now = await target.boundingBox();
    if (first && now)
      most = Math.max(
        most,
        Math.abs(now.x - first.x),
        Math.abs(now.y - first.y),
      );
  }
  return most;
}

/** Wait until `target` holds still for 600 ms (up to 15 s): checks then see the settled screen. */
export async function settled(target: Locator) {
  await expect(async () => {
    expect(await drift(target, 600)).toBeLessThan(1);
  }).toPass({ timeout: 15_000 });
}
