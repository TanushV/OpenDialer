import { useState } from "react";
import { useWorkspace } from "../context";

type Quote = {
  phone: string;
  monthly_cost_usd: number;
  initial_commitment_usd: number;
  expires_at: string;
};
export function Procurement() {
  const { api, run, refresh, notice, snapshot: w } = useWorkspace();
  const [region, setRegion] = useState("");
  const [quotes, setQuotes] = useState<Quote[]>([]);
  const [amount, setAmount] = useState("");
  return (
    <section className="card padded">
      <span className="eyebrow">APPROVE ACTUAL PRICES</span>
      <h2>Regional procurement</h2>
      <p className="muted small">
        Search based on the capacity proposal. Quotes expire after five minutes.
        Your monthly procurement ceiling must cover setup and the first month.
      </p>
      <div className="button-row">
        <label>
          Procurement area code
          <input
            value={region}
            onChange={(e) => setRegion(e.target.value)}
            placeholder="415"
            maxLength={3}
          />
        </label>
        <button
          className="secondary"
          disabled={!/^[2-9][0-9]{2}$/.test(region)}
          onClick={() =>
            void run(async () => {
              const r = await api<{ numbers: Quote[] }>(
                "/v1/admin/numbers/search",
                "POST",
                { region },
              );
              setQuotes(r.numbers);
            })
          }
        >
          Get carrier quotes
        </button>
      </div>
      {quotes.length > 0 && (
        <>
          <label>
            Approved initial amount (USD)
            <input
              type="number"
              min="0"
              step="0.01"
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
            />
          </label>
          <table>
            <thead>
              <tr>
                <th>NUMBER</th>
                <th>MONTHLY</th>
                <th>First month + setup</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {quotes.map((q) => (
                <tr key={q.phone}>
                  <td>{q.phone}</td>
                  <td>${q.monthly_cost_usd.toFixed(2)}</td>
                  <td>${q.initial_commitment_usd.toFixed(2)}</td>
                  <td>
                    <button
                      className="secondary small"
                      disabled={
                        !amount || Number(amount) < q.initial_commitment_usd
                      }
                      onClick={() =>
                        void run(async () => {
                          await api("/v1/admin/numbers/purchase", "POST", {
                            phone_numbers: [q.phone],
                            approved_budget_usd: Number(amount),
                            idempotency_key: `quote-${q.phone}-${q.expires_at}`,
                          });
                          setQuotes(quotes.filter((n) => n.phone !== q.phone));
                          await refresh();
                          notice(
                            w.simulation
                              ? "Simulated purchase recorded. No real number was purchased."
                              : "Order recorded. Complete identity and callback onboarding.",
                          );
                        })
                      }
                    >
                      {w.simulation
                        ? "Simulate approved purchase"
                        : "Purchase with approved budget"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </section>
  );
}
export function Variants() {
  const { api, run, notice, snapshot: w } = useWorkspace();
  const [name, setName] = useState("");
  const [opener, setOpener] = useState("");
  const [reference, setReference] = useState("");
  return (
    <section className="card padded">
      <span className="eyebrow">SMALL, REVIEWED EXPERIMENTS</span>
      <h2>Approved script treatments</h2>
      <p className="muted small">
        Variants retain your business identity. Nonurgent choices use a bounded
        exploration share with exact logged probabilities. Caller IDs stay
        stable.
      </p>
      <label>
        Variant name
        <input value={name} onChange={(e) => setName(e.target.value)} />
      </label>
      <label>
        Variant introduction
        <textarea
          rows={3}
          value={opener}
          onChange={(e) => setOpener(e.target.value)}
          placeholder={`Hi, this is {rep} from ${w.seller.name}.`}
        />
      </label>
      <label>
        Variant review reference
        <input
          value={reference}
          onChange={(e) => setReference(e.target.value)}
        />
      </label>
      <button
        className="primary"
        disabled={!name || !opener || !reference}
        onClick={() =>
          void run(async () => {
            await api("/v1/admin/variants", "POST", {
              campaign_id: w.campaign.id,
              name,
              opener,
              review_reference: reference,
              approved: true,
            });
            notice("Approved treatment published.");
            setName("");
            setOpener("");
            setReference("");
          })
        }
      >
        Publish approved variant
      </button>
    </section>
  );
}
export function AccessAndRecovery() {
  const { api, run, notice, refresh } = useWorkspace();
  const [reference, setReference] = useState("");
  const [user, setUser] = useState({
    name: "",
    email: "",
    password: "",
    role: "rep",
    sip_username: "",
    telephony_credential_id: "",
  });
  return (
    <div className="two-column">
      <section className="card padded">
        <h2>Team access</h2>
        {(
          [
            "name",
            "email",
            "password",
            "sip_username",
            "telephony_credential_id",
          ] as const
        ).map((k) => (
          <label key={k}>
            {
              {
                name: "Team member name",
                email: "Team member email",
                password: "Initial team password",
                sip_username: "Per-rep SIP username",
                telephony_credential_id: "Per-rep credential ID",
              }[k]
            }
            <input
              type={k === "password" ? "password" : "text"}
              value={user[k]}
              onChange={(e) => setUser({ ...user, [k]: e.target.value })}
            />
          </label>
        ))}
        <label>
          Member role
          <select
            value={user.role}
            onChange={(e) => setUser({ ...user, role: e.target.value })}
          >
            <option value="rep">Rep</option>
            <option value="admin">Administrator</option>
          </select>
        </label>
        <button
          className="secondary"
          disabled={!user.email || user.password.length < 12}
          onClick={() =>
            void run(async () => {
              await api("/v1/admin/users", "POST", user);
              setUser({ ...user, password: "" });
              notice("Member created with a scoped seller role.");
            })
          }
        >
          Create team member
        </button>
      </section>
      <section className="card padded">
        <h2>Restore authorization review</h2>
        <p className="muted small">
          After a restore, resolve any uncertain carrier submissions and
          reconcile the append-only authorization journal. This does not clear
          an opt-out.
        </p>
        <label>
          Restore reconciliation reference
          <input
            value={reference}
            onChange={(e) => setReference(e.target.value)}
          />
        </label>
        <button
          className="secondary"
          disabled={!reference}
          onClick={() =>
            void run(async () => {
              await api("/v1/admin/restore/reconcile", "POST", {
                review_reference: reference,
              });
              await refresh();
              notice(
                "Authorization journal reconciled. Check preflight before going ready.",
              );
            })
          }
        >
          Reconcile restored authority
        </button>
      </section>
    </div>
  );
}

export function DeferredScripts() {
  const { api, run, notice, snapshot: w, refresh } = useWorkspace();
  const [batch, setBatch] = useState("");
  const [result, setResult] = useState("");
  return (
    <section className="card padded">
      <span className="eyebrow">PREPARE AHEAD OF THE SHIFT</span>
      <h2>Deferred script preparation</h2>
      <p className="muted small">
        Use researched packets and the configured metered OpenAI route for work
        that can wait up to 24 hours. Immediate preparation remains available in
        Leads.
      </p>
      <button
        className="secondary"
        disabled={!w.leads.length}
        onClick={() =>
          void run(async () => {
            const r = await api<{ id: string }>(
              "/v1/admin/batches/scripts",
              "POST",
              {
                lead_ids: w.leads
                  .filter((l) => !l.suppressed)
                  .slice(0, 500)
                  .map((l) => l.id),
                idempotency_key: crypto.randomUUID(),
              },
            );
            setBatch(r.id);
            notice(
              "Deferred batch submitted. Its object ID is saved in the authority.",
            );
          })
        }
      >
        Submit deferred scripts
      </button>
      <label>
        Deferred batch record ID
        <input value={batch} onChange={(e) => setBatch(e.target.value)} />
      </label>
      <button
        className="secondary"
        disabled={!batch}
        onClick={() =>
          void run(async () => {
            const r = await api<{
              status: string;
              published: string[];
              retry_lead_ids: string[];
            }>(`/v1/admin/batches/${batch}/reconcile`, "POST");
            setResult(
              `${r.status}: ${r.published.length} published, ${r.retry_lead_ids.length} need retry.`,
            );
            await refresh();
          })
        }
      >
        Check and publish completed scripts
      </button>
      {result && <p>{result}</p>}
    </section>
  );
}
