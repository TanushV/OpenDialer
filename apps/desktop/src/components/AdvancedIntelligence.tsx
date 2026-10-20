import { useHistory } from "../state/useHistory";
import { HistoryMore } from "./HistoryMore";
import { Plus } from "lucide-react";
import { useState } from "react";
import { label, useWorkspace } from "../context";

/** Reviewed conversation trackers (Settings → Advanced). */
export function Trackers() {
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
  const lines = (value: string) =>
    value
      .split("\n")
      .map((s) => s.trim())
      .filter(Boolean);
  return (
    <section className="card padded" aria-label="Conversation trackers">
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
        evidence. Preview new definitions on a reviewed replay set before broad
        reprocessing.
      </p>
    </section>
  );
}
