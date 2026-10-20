import { useState } from "react";
import { useWorkspace } from "../context";
import { useDefinitions } from "../state/definitions";

export function AccessAndRecovery() {
  const { api, run, notice, refresh } = useWorkspace();
  const bounds = useDefinitions();
  const minimum = bounds?.setup?.password?.min_length;
  const [reference, setReference] = useState("");
  const [user, setUser] = useState({
    name: "",
    email: "",
    password: "",
    role: "rep",
    sip_username: "",
    telephony_credential_id: "",
  });
  const short = minimum === undefined || user.password.length < minimum;
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
              minLength={k === "password" ? minimum : undefined}
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
          disabled={!user.email || short}
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
        {(!user.email || short) && (
          <p className="fine">
            Enter an email and an initial password of at least{" "}
            {minimum ?? "the required"} characters.
          </p>
        )}
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
