/**
 * The top of Settings (section 7, R7.2, R7.3): one true status from the calling blockers, then one row per thing to
 * set, saying where to get it and what it powers. A row is green only after its real check.
 */
import { useCallback, useEffect, useState } from "react";
import { openSource } from "../api/client";
import { useWorkspace } from "../context";
import { Button, Panel, Status } from "../ui/kit";

interface Row {
  id: string;
  label: string;
  where: string;
  link: string | null;
  powers: string;
  optional: boolean;
  state: "ok" | "missing" | "unchecked" | "failed" | "optional";
  detail: string;
  checked_at?: string;
}
interface Rows {
  status: { code: string; message: string; fix: string } | null;
  rows: Row[];
}

/** Rows whose value is typed here; saving one runs its check at once. */
const FIELDS: Record<string, { key: string; secret: boolean }> = {
  telnyx: { key: "telnyx_api_key", secret: true },
  openrouter: { key: "openrouter_api_key", secret: true },
  test_phone: { key: "test_phone", secret: false },
};
const TONE = {
  ok: "good",
  failed: "bad",
  unchecked: "warn",
  missing: "warn",
  optional: "neutral",
} as const;
const WORD = {
  ok: "Checked",
  failed: "Failed",
  unchecked: "Not checked",
  missing: "Not set",
  optional: "Optional",
};

export function SetupRows({ version }: { version?: number }) {
  const { api, run } = useWorkspace();
  const [data, setData] = useState<Rows | null>(null);
  const load = useCallback(
    async () => setData(await api<Rows>("/v1/settings/rows")),
    [api],
  );
  useEffect(() => {
    void load().catch(() => {});
  }, [load, version]);
  if (!data) return null;
  return (
    <section className="setup-rows" aria-label="Readiness">
      <Panel
        className={data.status ? "setup-status blocked" : "setup-status ready"}
      >
        <strong>{data.status ? data.status.message : "Ready to call"}</strong>
      </Panel>
      {data.rows.map((row) => (
        <SetupRow
          key={row.id}
          row={row}
          save={async (value) => {
            const field = FIELDS[row.id];
            await api("/v1/admin/settings", "PUT", { [field.key]: value });
            setData(
              await api<Rows>(`/v1/settings/rows/${row.id}/check`, "POST"),
            );
          }}
          check={() =>
            run(async () =>
              setData(
                await api<Rows>(`/v1/settings/rows/${row.id}/check`, "POST"),
              ),
            )
          }
        />
      ))}
    </section>
  );
}

function SetupRow({
  row,
  save,
  check,
}: {
  row: Row;
  save: (value: string) => Promise<void>;
  check: () => Promise<void>;
}) {
  const { run } = useWorkspace();
  const [value, setValue] = useState("");
  const field = FIELDS[row.id];
  return (
    <div className="setup-row" data-testid={`setup-${row.id}`}>
      <div className="setup-row-text">
        <strong>{row.label}</strong>
        <span className="muted">
          {row.powers} ·{" "}
          {row.link ? (
            <button
              className="source-link"
              onClick={() => void openSource(row.link!)}
            >
              {row.where} ↗
            </button>
          ) : (
            row.where
          )}
        </span>
      </div>
      <Status tone={TONE[row.state]} label={`${row.label} status`}>
        {WORD[row.state]}: {row.detail}
      </Status>
      {field && (
        <form
          className="setup-row-form"
          onSubmit={(event) => {
            event.preventDefault();
            void run(async () => {
              await save(value.trim());
              setValue("");
            });
          }}
        >
          <input
            aria-label={`New ${row.label}`}
            type={field.secret ? "password" : "tel"}
            value={value}
            placeholder={
              row.state === "missing" ? "" : "Saved; type to replace"
            }
            onChange={(event) => setValue(event.target.value)}
          />
          <Button type="submit" disabled={!value.trim()}>
            Save and check
          </Button>
        </form>
      )}
      {!field && (row.state === "unchecked" || row.state === "failed") && (
        <Button variant="text" onClick={() => void check()}>
          Check again
        </Button>
      )}
    </div>
  );
}
