import { useRef, useState } from "react";
import { Check } from "lucide-react";
import { localEdit } from "../api/client";
import { label, useWorkspace } from "../context";
import type { Call } from "../types";

export function WrapUp({ call, onSaved }: { call: Call; onSaved: (call: Call) => Promise<void> }) {
  const { api, run, notice } = useWorkspace();
  const [outcome, setOutcome] = useState("right_party");
  const [contactRole, setContactRole] = useState("unknown");
  const [callbackAt, setCallbackAt] = useState("");
  const [wrapNote, setWrapNote] = useState("");
  const [saving, setSaving] = useState(false);
  const pending = useRef<{fingerprint: string; request_id: string} | null>(null);
  async function save() {
    if (saving) return;
    const body = { outcome, contact_role: contactRole, callback_note: wrapNote,
      expected_version: call.version,
      callback_at: outcome === "callback_requested" && callbackAt ? new Date(callbackAt).toISOString() : null };
    const fingerprint = JSON.stringify(body);
    if (pending.current?.fingerprint !== fingerprint)
      pending.current = { fingerprint, request_id: crypto.randomUUID() };
    setSaving(true);
    try {
      const result = await localEdit<Call>(api, `/v1/calls/${call.id}/disposition`, "POST",
        { ...body, request_id: pending.current.request_id });
      await onSaved(result);
      notice(outcome === "meeting_booked" || outcome === "meeting_held"
        ? "Meeting recorded as rep-reported. Outreach is paused."
        : "Outcome saved. Analysis continues in the background.");
    } finally { setSaving(false); }
  }
  return (
        <section className="wrap-up card">
          <div>
            <span className="eyebrow">LEAVE A CLEAR NEXT STEP</span>
            <h3>How did the conversation go?</h3>
          </div>
          {call.suggested_outcome && (
            <div>
              <p>
                Suggested outcome: {label(call.suggested_outcome.code)}.{" "}
                {call.suggested_outcome.reason}
              </p>
              <button
                className="secondary"
                onClick={() => {
                  setOutcome(call.suggested_outcome!.code);
                  setContactRole(call.suggested_outcome!.contact_role);
                }}
              >
                Use suggested outcome
              </button>
              <small>Review the outcome and contact role before saving.</small>
            </div>
          )}
          <label>
            Disposition
            <select
              value={outcome}
              onChange={(e) => setOutcome(e.target.value)}
            >
              {[
                "right_party",
                "qualified_conversation",
                "sale_won",
                "callback_requested",
                "meeting_booked",
                "meeting_held",
                "gatekeeper",
                "screening",
                "ivr",
                "voicemail",
                "no_answer",
                "busy",
                "wrong_party",
                "invalid_endpoint",
                "not_interested",
                "opt_out",
                "temporary_failure",
              ].map((o) => (
                <option key={o} value={o}>
                  {label(o)}
                </option>
              ))}
            </select>
          </label>
          {outcome === "callback_requested" && (
            <label>
              Confirmed callback time
              <input
                type="datetime-local"
                value={callbackAt}
                onChange={(e) => setCallbackAt(e.target.value)}
              />
            </label>
          )}
          <label>
            Who did you speak with?
            <select
              value={contactRole}
              onChange={(event) => setContactRole(event.target.value)}
            >
              <option value="unknown">Not confirmed</option>
              <option value="right_party">Target contact</option>
              <option value="gatekeeper">Gatekeeper</option>
              <option value="wrong_party">Wrong person</option>
            </select>
          </label>
          <label>
            Wrap-up note
            <textarea
              value={wrapNote}
              maxLength={1000}
              onChange={(event) => setWrapNote(event.target.value)}
            />
          </label>
          {call.rundown_status && (
            <p role="status">
              Rundown: {label(call.rundown_status)}. You can continue eligible
              calling while analysis finishes.
            </p>
          )}
          <button
            className="primary"
            disabled={saving}
            onClick={() =>
              void run(save)
            }
          >
            Save outcome
            <Check size={15} />
          </button>
        </section>

  );
}
