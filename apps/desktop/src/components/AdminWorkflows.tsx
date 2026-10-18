import { useState } from "react";
import { useWorkspace } from "../context";

type Quote = {
  phone: string;
  monthly_cost_usd: number;
  initial_commitment_usd: number;
  expires_at: string;
};
export function Procurement() {
  const { api, run } = useWorkspace();
  const [region, setRegion] = useState("");
  const [quotes, setQuotes] = useState<Quote[]>([]);
  return (
    <section className="card padded">
      <span className="eyebrow">REFERENCE QUOTES ONLY</span>
      <h2>Regional number costs</h2>
      <p className="muted small">
        Reuse owned numbers first. New number rentals are recurring commitments
        and cannot be purchased under your cost policy. Quotes expire after five
        minutes.
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
              const result = await api<{ numbers: Quote[] }>(
                "/v1/admin/numbers/search",
                "POST",
                { region },
              );
              setQuotes(result.numbers);
            })
          }
        >
          Get carrier quotes
        </button>
      </div>
      {quotes.length > 0 && (
        <>
          <p className="pill">New rentals blocked</p>
          <table>
            <thead>
              <tr>
                <th>NUMBER</th>
                <th>MONTHLY</th>
                <th>First month + setup</th>
              </tr>
            </thead>
            <tbody>
              {quotes.map((q) => (
                <tr key={q.phone}>
                  <td>{q.phone}</td>
                  <td>${q.monthly_cost_usd.toFixed(2)}</td>
                  <td>${q.initial_commitment_usd.toFixed(2)}</td>
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

export { DeferredScripts } from "./DeferredScripts";
