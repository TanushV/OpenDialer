import { expect, test } from "@playwright/test";

test("restore explains current-record preservation and permits the same backup retry", async ({
  page,
}) => {
  const backupId = "690e8a64-46fd-4e09-a98d-02f26ec7675c";
  let attempts = 0;
  await page.route("**/v1/admin/restore", async (route) => {
    expect(route.request().postDataJSON()).toEqual({ backup_id: backupId });
    attempts++;
    if (attempts === 1) await route.abort("failed");
    else
      await route.fulfill({
        json: {
          restored: true,
          mode: "recover_missing_keep_current",
          recovered_records: 3,
          suppression_journal_replayed: 0,
          outbound_paused: true,
        },
      });
  });
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  if (
    await page
      .getByRole("button", { name: "Try a simulated workspace" })
      .isVisible()
  ) {
    await page
      .getByRole("button", { name: "Try a simulated workspace" })
      .click();
  }
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByRole("button", { name: "Operations", exact: true }).click();
  const backup = page
    .locator("section")
    .filter({ has: page.getByRole("heading", { name: "Encrypted backups" }) });
  await expect(backup).toContainText(
    "keeps current records and live obligations",
  );
  await backup.getByLabel("Backup ID to restore").fill(backupId);
  const restore = backup.getByRole("button", {
    name: "Restore selected backup",
  });
  await restore.click();
  await expect(restore).toBeEnabled();
  await restore.click();
  await expect(
    page.getByText(
      "Recovered 3 missing records. Current records were kept. Outbound calling is paused.",
      { exact: true },
    ),
  ).toBeVisible();
  expect(attempts).toBe(2);
});
