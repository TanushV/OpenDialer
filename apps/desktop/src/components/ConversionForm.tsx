import { useState } from "react";
import { useWorkspace } from "../context";
import type { Call } from "../types";

export function ConversionForm({ call }: { call: Call }) {
  const { api, run, notice, refresh, snapshot } = useWorkspace();
  const [kind, setKind] = useState("meeting_held");
  const [reference, setReference] = useState("");
  const [eventId, setEventId] = useState(() => crypto.randomUUID());
  const [saved, setSaved] = useState(false);
  return (
    <section className="card padded optimization-panel">
      <h3>Record a later conversion</h3>
      <p className="fine">
        Attach an observed result to this lead and call. Open outcome windows
        stay pending until evidence arrives or the window closes.
      </p>
      <label>
        Conversion event
        <select
          value={kind}
          onChange={(e) => {
            setKind(e.target.value);
            setEventId(crypto.randomUUID());
            setSaved(false);
          }}
        >
          <option value="qualified_conversation">Qualified conversation</option>
          <option value="meeting_held">Held meeting</option>
          <option value="sale_won">Won sale</option>
        </select>
      </label>
      <label>
        Conversion source reference
        <input
          value={reference}
          onChange={(e) => setReference(e.target.value)}
        />
      </label>
      <button
        className="secondary"
        disabled={!reference || saved}
        onClick={() =>
          void run(async () => {
            await api("/v1/leads/" + call.lead_id + "/conversions", "POST", {
              event_id: eventId,
              kind,
              call_id: call.id,
              source_reference: reference,
              occurred_at: snapshot.simulation
                ? call.ended_at || call.started_at
                : new Date().toISOString(),
            });
            setSaved(true);
            await refresh();
            notice("Conversion recorded");
          })
        }
      >
        Record conversion
      </button>
    </section>
  );
}
