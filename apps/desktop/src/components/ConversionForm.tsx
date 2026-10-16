import { useCallback, useEffect, useState } from "react";
import { date, label, useWorkspace } from "../context";
import type { Call } from "../types";

export function ConversionForm({ call }: { call: Call }) {
  const { api, run, notice, refresh, snapshot } = useWorkspace();
  const [kind, setKind] = useState("meeting_held");
  const [reference, setReference] = useState("");
  const [eventId, setEventId] = useState(() => crypto.randomUUID());
  const [saved, setSaved] = useState(false);
  const [correction, setCorrection] = useState("");
  const [events, setEvents] = useState<
    {
      id: string;
      kind: string;
      source_reference: string;
      occurred_at: string;
      retracted?: boolean;
    }[]
  >([]);
  const load = useCallback(async () => {
    setEvents(await api("/v1/leads/" + call.lead_id + "/conversions"));
  }, [api, call.lead_id]);
  useEffect(() => {
    void run(load);
  }, [load, run]);
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
            await load();
            await refresh();
            notice("Conversion recorded");
          })
        }
      >
        Record conversion
      </button>
      {events.length > 0 && (
        <details open>
          <summary>Observed conversion history</summary>
          <label>
            Conversion correction reason
            <input
              value={correction}
              onChange={(e) => setCorrection(e.target.value)}
            />
          </label>
          {events.map((event) => (
            <div key={event.id} className="conversion-entry">
              <p>
                {label(event.kind)} · {date(event.occurred_at)} ·{" "}
                {event.source_reference}
                {event.retracted ? " · Retracted" : ""}
              </p>
              {!event.retracted && (
                <button
                  className="text-button"
                  disabled={correction.trim().length < 3}
                  onClick={() =>
                    void run(async () => {
                      await api(
                        "/v1/leads/" +
                          call.lead_id +
                          "/conversions/" +
                          event.id +
                          "/retract",
                        "POST",
                        { reason: correction },
                      );
                      await load();
                      await refresh();
                      notice("Conversion retracted");
                    })
                  }
                >
                  Retract conversion
                </button>
              )}
            </div>
          ))}
        </details>
      )}
    </section>
  );
}
