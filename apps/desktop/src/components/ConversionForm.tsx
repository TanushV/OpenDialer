import { useState } from "react";
import { date, label, useWorkspace } from "../context";
import { useLoad } from "../state/useLoad";
import { options, useDefinitions } from "../state/definitions";
import type { Call } from "../types";

export function ConversionForm({ call }: { call: Call }) {
  const { api, run, notice, refresh, snapshot } = useWorkspace();
  const kinds = options(useDefinitions()?.conversion?.kind);
  // The campaign's own goal is the natural default; the authority lists the kinds.
  const [chosen, setKind] = useState<string | null>(null);
  const kind =
    chosen ??
    (kinds.includes(snapshot.metrics.outcome_goal ?? "")
      ? snapshot.metrics.outcome_goal!
      : (kinds[0] ?? ""));
  const [reference, setReference] = useState("");
  const [eventId, setEventId] = useState(() => crypto.randomUUID());
  const [saved, setSaved] = useState(false);
  const [correction, setCorrection] = useState("");
  const conversions = useLoad<
    {
      id: string;
      kind: string;
      source_reference: string;
      occurred_at: string;
      retracted?: boolean;
    }[]
  >(`/v1/leads/${call.lead_id}/conversions`);
  const events = conversions.data ?? [];
  const load = conversions.reload;
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
          {kinds.map((value) => (
            <option key={value} value={value}>
              {label(value)}
            </option>
          ))}
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
        disabled={!reference || saved || !kind}
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
