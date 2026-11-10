/**
 * The top of Settings (section 7, R7.2, R7.3): one true status from the calling blockers, then one row per thing to
 * set, saying where to get it and what it powers. A row is green only after its real check.
 */
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
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

/** Rows whose values are typed here; saving runs the row's check at once. */
const FIELDS: Record<string, { keys: [string, string][]; secret: boolean }> = {
  telnyx: { keys: [["telnyx_api_key", "Telnyx API key"]], secret: true },
  openrouter: {
    keys: [["openrouter_api_key", "OpenRouter key"]],
    secret: true,
  },
  test_phone: { keys: [["test_phone", "Test phone"]], secret: false },
  hubspot: {
    keys: [["hubspot_service_key", "HubSpot Service Key"]],
    secret: true,
  },
  treg: { keys: [["treg_token", "Treg token"]], secret: true },
  realvalidito: {
    keys: [
      ["realvalidito_api_key", "RealValidito key"],
      ["realvalidito_api_secret", "RealValidito secret"],
    ],
    secret: true,
  },
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

export function SetupRows({
  version,
  testSession,
  onTestCall,
  reload,
  extras = {},
}: {
  version?: number;
  /** A row's own control from the page that owns it: the ChatGPT login, the call connection, calling hours. */
  extras?: Record<string, ReactNode>;
  /** A test session is on: calling reaches only the test phone. */
  testSession?: boolean;
  onTestCall: () => void;
  /** Re-read the saved settings after a change made here. */
  reload: () => Promise<void>;
}) {
  const { api, run, notice, refresh } = useWorkspace();
  const [data, setRows] = useState<Rows | null>(null);
  // An answer shows only if no later request has answered: a slow re-read never undoes a newer check.
  const started = useRef(0);
  const shown = useRef(0);
  const fetchRows = useCallback(
    async (path: string, method?: "POST") => {
      const ticket = ++started.current;
      const rows = await api<Rows>(path, method);
      if (ticket < shown.current) return;
      shown.current = ticket;
      setRows(rows);
    },
    [api],
  );
  const load = useCallback(() => fetchRows("/v1/settings/rows"), [fetchRows]);
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
      {testSession && (
        <Panel className="setup-status blocked">
          <strong>
            Test session on: Start calling dials only your test phone.
          </strong>{" "}
          <Button
            onClick={() =>
              void run(async () => {
                await api("/v1/settings/test-call", "DELETE");
                await Promise.all([refresh(), reload()]);
                notice(
                  "Test session ended. Calling uses your lead lists again.",
                );
              })
            }
          >
            End test session
          </Button>
        </Panel>
      )}
      {data.rows.map((row) => (
        <SetupRow
          key={row.id}
          row={row}
          extra={extras[row.id]}
          save={async (values) => {
            await api("/v1/admin/settings", "PUT", values);
            await fetchRows(`/v1/settings/rows/${row.id}/check`, "POST");
          }}
          check={() =>
            run(() => fetchRows(`/v1/settings/rows/${row.id}/check`, "POST"))
          }
          testCall={
            row.id === "test_phone" && row.state === "ok"
              ? () =>
                  run(async () => {
                    await api("/v1/settings/test-call", "POST");
                    await Promise.all([refresh(), reload()]);
                    notice(
                      "Test session on. Press Start calling to call your test phone.",
                    );
                    onTestCall();
                  })
              : undefined
          }
        />
      ))}
    </section>
  );
}

function SetupRow({
  row,
  extra,
  save,
  check,
  testCall,
}: {
  row: Row;
  extra?: ReactNode;
  save: (values: Record<string, string>) => Promise<void>;
  check: () => Promise<void>;
  testCall?: () => Promise<void>;
}) {
  const { run } = useWorkspace();
  const [values, setValues] = useState<Record<string, string>>({});
  // Optional services not set yet show one Add button, so Settings stays one short page.
  const [adding, setAdding] = useState(false);
  const field = FIELDS[row.id];
  const filled =
    !!field && field.keys.every(([key]) => (values[key] ?? "").trim());
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
        {row.detail === WORD[row.state]
          ? row.detail
          : `${WORD[row.state]}: ${row.detail}`}
      </Status>
      {field && row.state === "optional" && !adding && (
        <Button onClick={() => setAdding(true)}>Add</Button>
      )}
      {field && (row.state !== "optional" || adding) && (
        <form
          className="setup-row-form"
          onSubmit={(event) => {
            event.preventDefault();
            void run(async () => {
              await save(
                Object.fromEntries(
                  field.keys.map(([key]) => [key, values[key].trim()]),
                ),
              );
              setValues({});
            });
          }}
        >
          {field.keys.map(([key, name]) => (
            <input
              key={key}
              aria-label={
                field.keys.length === 1 ? `New ${row.label}` : `New ${name}`
              }
              type={field.secret ? "password" : "tel"}
              value={values[key] ?? ""}
              placeholder={
                row.state === "missing" || row.state === "optional"
                  ? name
                  : "Saved; type to replace"
              }
              onChange={(event) =>
                setValues({ ...values, [key]: event.target.value })
              }
            />
          ))}
          <Button type="submit" disabled={!filled}>
            Save and check
          </Button>
        </form>
      )}
      {extra && <div className="setup-row-extra">{extra}</div>}
      {row.id === "calls_for" && <CallsFor onSaved={check} />}
      {row.id === "chatgpt" && <ResearchRoute />}
      {testCall && (
        <Button variant="primary" onClick={() => void testCall()}>
          Test call
        </Button>
      )}
      {/* A saved value is checked as it is, without typing it again. */}
      {(row.state === "unchecked" || row.state === "failed") && (
        <Button variant="text" onClick={() => void check()}>
          {row.state === "failed" ? "Check again" : "Check"}
        </Button>
      )}
    </div>
  );
}

/** Which HubSpot user this computer calls for: chosen once when the seat is set up, then read-only (D25). */
function CallsFor({ onSaved }: { onSaved: () => Promise<void> }) {
  const { api, run } = useWorkspace();
  const [owners, setOwners] = useState<{ id: string; name: string }[]>([]);
  const [chosen, setChosen] = useState<string | null>(null);
  const [pick, setPick] = useState("");
  if (chosen) return null;
  return (
    <div className="setup-row-form">
      {owners.length === 0 ? (
        <Button
          onClick={() =>
            void run(async () => {
              const found = await api<{
                owners: { id: string; name: string }[];
                calls_for: string | null;
              }>("/v1/hubspot/owners");
              setOwners(found.owners);
              setChosen(found.calls_for);
              setPick(found.owners[0]?.id ?? "");
            })
          }
        >
          Choose HubSpot user
        </Button>
      ) : (
        <>
          <select
            aria-label="Calls for"
            value={pick}
            onChange={(e) => setPick(e.target.value)}
          >
            {owners.map((o) => (
              <option key={o.id} value={o.id}>
                {o.name}
              </option>
            ))}
          </select>
          <Button
            disabled={!pick}
            onClick={() =>
              void run(async () => {
                await api("/v1/hubspot/calls-for", "PUT", { owner_id: pick });
                setChosen(pick);
                await onSaved();
              })
            }
          >
            Save
          </Button>
        </>
      )}
    </div>
  );
}

/** R9C.10: a rep's seat researches with their own ChatGPT login, or with the OpenRouter fallback as its main route. */
function ResearchRoute() {
  const { api, run, notice } = useWorkspace();
  const [route, setRoute] = useState<string | null>(null);
  useEffect(() => {
    void api<{ research_route?: string }>("/v1/admin/settings")
      .then((saved) => setRoute(saved.research_route ?? "chatgpt"))
      .catch(() => setRoute("chatgpt"));
  }, [api]);
  if (route === null) return null;
  return (
    <label className="setup-row-form">
      Research uses
      <select
        aria-label="Research uses"
        value={route}
        onChange={(e) => {
          const next = e.target.value;
          void run(async () => {
            await api("/v1/admin/settings", "PUT", { research_route: next });
            setRoute(next);
            notice(
              next === "fallback"
                ? "Research uses OpenRouter (DeepSeek) on this computer."
                : "Research uses this computer's ChatGPT login, with OpenRouter as the fallback.",
            );
          });
        }}
      >
        <option value="chatgpt">ChatGPT login (OpenRouter as fallback)</option>
        <option value="fallback">OpenRouter only</option>
      </select>
    </label>
  );
}
