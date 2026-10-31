import { expect, test } from "./fixtures";
import { openMoreSettings, openScriptSection } from "./navigation";

test("an unfinished report shows pending evidence without invented totals", async ({
  page,
}) => {
  await page.route("**/v1/optimization", (route) =>
    route.fulfill({
      json: {
        settings: {},
        numbers: [],
        projection: {
          status: "pending",
          as_of: null,
          refresh_status: "queued",
          job_id: "report-job",
        },
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
  await openScriptSection(page, "Call statistics");
  await expect(
    page.getByText("Preparing the first report in the background.", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(page.getByText(/0 unique conversions/)).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Check report status", exact: true }),
  ).toBeVisible();
});

const settings = {
  conversion_goal: "meeting_held",
  window_days: 14,
  exploration: 0.1,
  auto_train: false,
  horizon_hours: 24,
  rep_minutes: 480,
  handling_seconds: 60,
  daily_capacity_per_number: 80,
  training_min_rows: 100,
  staffed_windows: [],
  additional_ready_after_hours: 0,
};
const savedReport = {
  settings,
  forecast: {
    regions: [],
    capacity_shortfall: 0,
    staffing_shortfall: 0,
    number_shortfall: 0,
  },
  numbers: [],
  metrics: {
    unique_conversions: 3,
    original_eligible_leads: 10,
    unique_right_party: 5,
    conversion_per_original_eligible_lead: 0.3,
  },
  dataset: {
    rows: 0,
    matured_rows: 0,
    pending_rows: 0,
    positive_conversions: 0,
    missing_snapshot_calls: 0,
    snapshots: [],
  },
  decisions: [],
  future_slots: [],
  drift: { action: "awaiting_evidence" },
  trial: {
    epoch: "current",
    arms: {},
    pending_leads: 10,
    required_per_arm: 100,
    sufficient_evidence: false,
  },
  comparisons: [],
  cohort_history: ["current", "historical"],
  coaching: { reviewed_cards: 0 },
};

async function openAdvanced(page: import("@playwright/test").Page) {
  await openMoreSettings(page, "Learning experiments and trackers");
}

async function openIntelligence(page: import("@playwright/test").Page) {
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
  await openScriptSection(page, "Call statistics");
}

test("stale totals disclose their definition and failed refresh without losing settings", async ({
  page,
}) => {
  await page.route(/\/v1\/optimization(?:\?.*)?$/, (route) =>
    route.fulfill({
      json: {
        ...savedReport,
        settings: { ...settings, rep_minutes: 1 },
        projection: {
          status: "stale",
          as_of: "2026-10-14T17:00:00Z",
          calculated_settings: settings,
          refresh_status: "dead",
          refresh_error: "RuntimeError",
          job_id: "report-job",
        },
      },
    }),
  );
  await openIntelligence(page);
  await expect(page.getByText(/3 unique conversions/)).toBeVisible();
  await expect(
    page.getByText(
      "Saved report; refresh failed. Review the job in Recovery.",
      { exact: true },
    ),
  ).toBeVisible();
  await expect(
    page.getByText(/Calculated for meeting held, 14 days/),
  ).toBeVisible();
  // Planning assumptions are edited in Settings → Advanced from the same report.
  await openAdvanced(page);
  await expect(page.getByLabel("Available rep minutes")).toHaveValue("1");
});

test("checking a pending historical cohort keeps the selected cohort", async ({
  page,
}) => {
  // The report stays pending until the rep checks it: the app refreshes a pending report on its
  // own, and CI runs 39682453717 and 8a862fa caught it turning ready before or after the check.
  let released = false;
  await page.route(/\/v1\/optimization(?:\?.*)?$/, (route) => {
    const historical =
      new URL(route.request().url()).searchParams.get("epoch") === "historical";
    return route.fulfill({
      json:
        historical && !released
          ? {
              settings,
              projection: {
                status: "pending",
                as_of: null,
                refresh_status: "queued",
              },
            }
          : {
              ...savedReport,
              trial: {
                ...savedReport.trial,
                epoch: historical ? "historical" : "current",
              },
              metrics: {
                ...savedReport.metrics,
                unique_conversions: historical ? 2 : 3,
              },
              projection: {
                status: "ready",
                as_of: "2026-10-14T17:00:00Z",
                calculated_settings: settings,
              },
            },
    });
  });
  await openIntelligence(page);
  // Cohort comparison is experiment detail in Settings → Advanced.
  await openAdvanced(page);
  await page.getByLabel("Cohort history").selectOption("historical");
  await expect(
    page.getByText("Preparing the first report in the background.", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(page.getByText(/3 unique conversions/)).toHaveCount(0);
  released = true;
  await page
    .getByRole("button", { name: "Check report status", exact: true })
    .click();
  await expect(page.getByText(/2 unique conversions/)).toBeVisible();
  await expect(page.getByLabel("Cohort history")).toHaveValue("historical");
});
