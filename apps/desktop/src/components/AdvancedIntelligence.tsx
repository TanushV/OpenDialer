import { useHistory } from "../state/useHistory";
import { HistoryMore } from "./HistoryMore";
import { Check, Plus } from "lucide-react";
import { useState } from "react";
import { label, useWorkspace } from "../context";

export function AdvancedIntelligence() {
  const { snapshot: w, api, run, refresh, notice } = useWorkspace();
  const history = useHistory("trackers", w.trackers);
  const [tracker, setTracker] = useState({
    name: "",
    definition: "",
    kind: "keyword",
    phrases: "",
    positive: "",
    negative: "",
  });
  const [billing, setBilling] = useState("");
  const [reference, setReference] = useState("");
  const lines = (value: string) =>
    value
      .split("\n")
      .map((s) => s.trim())
      .filter(Boolean);
  return (
    <>
      <div className="two-column">
        <section className="card padded">
          <span className="eyebrow">ASK THE SAME QUESTION CONSISTENTLY</span>
          <h2>Conversation trackers</h2>
          {history.items.map((t) => (
            <p key={t.id}>
              <span className="pill green">{t.name}</span> · {label(t.kind)} · v
              {t.version}
            </p>
          ))}
          <HistoryMore history={history} label="Load more trackers" />
          <label>
            Tracker name
            <input
              value={tracker.name}
              onChange={(e) => setTracker({ ...tracker, name: e.target.value })}
            />
          </label>
          <label>
            Tracker definition
            <textarea
              rows={2}
              value={tracker.definition}
              onChange={(e) =>
                setTracker({ ...tracker, definition: e.target.value })
              }
            />
          </label>
          <label>
            Tracker type
            <select
              aria-label="Tracker type"
              value={tracker.kind}
              onChange={(e) => setTracker({ ...tracker, kind: e.target.value })}
            >
              <option value="keyword">Keyword phrases</option>
              <option value="semantic">Semantic concept with evidence</option>
            </select>
          </label>
          {tracker.kind === "keyword" ? (
            <label>
              Keyword phrases
              <textarea
                rows={3}
                value={tracker.phrases}
                onChange={(e) =>
                  setTracker({ ...tracker, phrases: e.target.value })
                }
              />
            </label>
          ) : (
            <>
              <label>
                Positive examples
                <textarea
                  rows={2}
                  value={tracker.positive}
                  onChange={(e) =>
                    setTracker({ ...tracker, positive: e.target.value })
                  }
                />
              </label>
              <label>
                Negative examples
                <textarea
                  rows={2}
                  value={tracker.negative}
                  onChange={(e) =>
                    setTracker({ ...tracker, negative: e.target.value })
                  }
                />
              </label>
            </>
          )}
          <button
            className="primary"
            disabled={!tracker.name || !tracker.definition}
            onClick={() =>
              void run(async () => {
                await api("/v1/admin/trackers", "POST", {
                  name: tracker.name,
                  definition: tracker.definition,
                  kind: tracker.kind,
                  phrases: lines(tracker.phrases),
                  positive_examples: lines(tracker.positive),
                  negative_examples: lines(tracker.negative),
                });
                await refresh();
                notice("Tracker published as a new reviewable version.");
              })
            }
          >
            <Plus size={15} />
            Publish tracker
          </button>
          <p className="fine">
            Semantic trackers use configured inference and validated segment
            evidence. Preview new definitions on a reviewed replay set before
            broad reprocessing.
          </p>
        </section>
        <section className="card padded">
          <h2>Authority-owned model evaluation</h2>
          <p className="muted small">
            Use the operational training dataset and reviewed model pilot above.
            Each candidate is trained from frozen pre-call records, calibrated
            on independent chronological data, and compared with the deployed
            pooled baseline.
          </p>
          <p className="fine">
            Saved live cohort evidence and explicit review are required before
            production activation. Restore the pooled baseline at any time from
            the pilot controls.
          </p>
        </section>
      </div>
      <section className="card import-card">
        <div>
          <span className="eyebrow">COUNT EVERY LEG AND TRACK</span>
          <h2>Reconcile provider records</h2>
          <p className="muted small">
            Import actual carrier records with provider_record_id, call_id,
            leg_id, duration_seconds, cost_usd and kind. Repeated imports do not
            double-charge the ledger.
          </p>
          <p className="fine">
            Matching carrier records establishes measured units. Invoice
            verification is a separate step.
          </p>
        </div>
        <div>
          <label>
            Original provider source reference
            <input
              value={reference}
              onChange={(e) => setReference(e.target.value)}
            />
          </label>
          <label>
            Carrier record JSON
            <textarea
              rows={4}
              value={billing}
              onChange={(e) => setBilling(e.target.value)}
            />
          </label>
          <button
            className="secondary"
            disabled={!reference || !billing}
            onClick={() =>
              void run(async () => {
                const r = await api<{ matched: number; duplicates: number }>(
                  "/v1/admin/billing/reconcile",
                  "POST",
                  { source_reference: reference, rows: JSON.parse(billing) },
                );
                await refresh();
                notice(
                  `${r.matched} carrier records matched; ${r.duplicates} duplicates ignored.`,
                );
              })
            }
          >
            <Check size={15} />
            Reconcile records
          </button>
        </div>
      </section>
    </>
  );
}
