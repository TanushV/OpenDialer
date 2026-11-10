import { expect, test } from "./fixtures";
import { readFileSync } from "node:fs";

test("welcome displays the packaged desktop version", async ({ page }) => {
  const { version } = JSON.parse(
    readFileSync(new URL("../package.json", import.meta.url), "utf8"),
  );
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  await expect(page.locator(".version")).toHaveText("DESKTOP · " + version);
});
