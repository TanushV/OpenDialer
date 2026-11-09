/**
 * Team calling (D25, D26, section 9C): set up a rep's seat from the owner's computer and keep the shared number pool.
 * Each rep gets a private setup file to import on their own Mac or PC; it never leaves the owner's computer otherwise.
 */
import { useCallback, useEffect, useState } from "react";
import { useWorkspace } from "../context";
import { Button, Field, Panel } from "../ui/kit";

type Usage = Record<
  string,
  {
    attempts_today: number;
    daily_cap: number;
    area_code: string;
    active: boolean;
  }
>;

export function Team() {
  const { api, run, notice } = useWorkspace();
  const [label, setLabel] = useState("");
  const [key, setKey] = useState("");
  const [pool, setPool] = useState<{ enabled: boolean; numbers: Usage } | null>(
    null,
  );
  const [phone, setPhone] = useState("");
  const [cap, setCap] = useState("50");
  const load = useCallback(
    async () =>
      setPool(await api<{ enabled: boolean; numbers: Usage }>("/v1/pool")),
    [api],
  );
  useEffect(() => {
    void load().catch(() => setPool(null));
  }, [load]);
  return (
    <details className="card padded" aria-label="Team calling">
      <summary>
        <strong>Team calling</strong>: set up reps and the shared number pool
      </summary>
      <p className="muted small">
        Each rep calls from their own computer with their own Telnyx key. Their
        HubSpot user decides whom they call, and every rep dials from the shared
        number pool.
      </p>
      <Panel>
        <strong>Set up a rep</strong>
        <form
          className="setup-row-form"
          onSubmit={(event) => {
            event.preventDefault();
            void run(async () => {
              const file = await api<Record<string, unknown>>(
                "/v1/seats",
                "POST",
                { label: label.trim(), telnyx_api_key: key.trim() },
              );
              const blob = new Blob([JSON.stringify(file, null, 2)], {
                type: "application/json",
              });
              const link = document.createElement("a");
              link.href = URL.createObjectURL(blob);
              link.download = `opendialer-seat-${label.trim().replace(/[^A-Za-z0-9]+/g, "-")}.json`;
              link.click();
              URL.revokeObjectURL(link.href);
              setKey("");
              notice(
                `${label.trim()}'s setup file is saved. Send it privately; they import it in Settings.`,
              );
            });
          }}
        >
          <input
            aria-label="Rep name"
            placeholder="Rep name"
            value={label}
            maxLength={60}
            onChange={(e) => setLabel(e.target.value)}
          />
          <input
            aria-label="Rep's Telnyx API key"
            placeholder="Rep's Telnyx API key"
            type="password"
            value={key}
            onChange={(e) => setKey(e.target.value)}
          />
          <Button
            type="submit"
            disabled={!label.trim() || key.trim().length < 10}
          >
            Create setup file
          </Button>
        </form>
      </Panel>
      <Panel>
        <strong>Number pool</strong>
        {pool?.enabled ? (
          <table aria-label="Pool numbers">
            <thead>
              <tr>
                <th>Number</th>
                <th>Calls today</th>
                <th>Daily cap</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(pool.numbers).map(([number, use]) => (
                <tr key={number}>
                  <td>
                    {number}
                    {!use.active && " (paused)"}
                  </td>
                  <td>{use.attempts_today}</td>
                  <td>{use.daily_cap}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="muted small">
            Not in use yet. Add the numbers you bought for the pool.
          </p>
        )}
        <form
          className="setup-row-form"
          onSubmit={(event) => {
            event.preventDefault();
            void run(async () => {
              await api("/v1/pool/numbers", "PUT", {
                phone: phone.trim(),
                daily_cap: Number(cap),
              });
              setPhone("");
              await load();
              notice("Pool number saved.");
            });
          }}
        >
          <Field label="Pool number (+1…)">
            <input value={phone} onChange={(e) => setPhone(e.target.value)} />
          </Field>
          <Field label="Daily cap">
            <input
              inputMode="numeric"
              value={cap}
              onChange={(e) => setCap(e.target.value)}
            />
          </Field>
          <Button
            type="submit"
            disabled={!/^\+1[2-9]\d{9}$/.test(phone.trim())}
          >
            Add to pool
          </Button>
        </form>
      </Panel>
    </details>
  );
}
