import { useEffect, useState } from "react";
import { date, useWorkspace } from "../context";

type Goal = { id: string; text: string; status: "active" | "completed" };
type Overview = {
  goals: Goal[];
  goals_version: number;
  inspected_calls: number;
  history_limit: number;
  recent_tips: { call_id: string; at: string; tips: { text: string }[] }[];
  improvement_groups: {
    id: string;
    concept: string;
    approach: string | null;
    count: number;
    examples: { call_id: string; observation_id: string; text: string }[];
  }[];
  examples: {
    id: string;
    version: number;
    text: string;
    raw_evidence_status: string;
  }[];
};
/** The review agent writes "Work on: <area>. How: <step>."; show the area and the step apart. */
function practicePoint(text: string) {
  const match = /^Work on:\s*(.+?)\s*How:\s*(.+)$/is.exec(text.trim());
  return match
    ? { title: match[1].replace(/[.\s]+$/, ""), how: match[2] }
    : { title: text };
}

export function Coaching({
  onReview,
}: {
  onReview: (id: string) => Promise<void>;
}) {
  const { api, run, session, notice } = useWorkspace();
  const [overview, setOverview] = useState<Overview | null>(null);
  const [patterns, setPatterns] = useState<
    { label: string; samples: number }[]
  >([]);
  const [goal, setGoal] = useState("");
  const [status, setStatus] = useState("");
  useEffect(() => {
    let active = true;
    void Promise.all([
      api<Overview>("/v1/coaching"),
      api<{
        report: {
          patterns: { label: string; samples: number; status: string }[];
        };
      }>("/v1/optimization/script"),
    ])
      .then(([data, learning]) => {
        if (active) {
          setOverview(data);
          setPatterns(
            (learning.report.patterns || [])
              .filter((p) => p.status === "supported_negative")
              .slice(0, 3),
          );
        }
      })
      .catch(() => {
        if (active)
          setStatus("Coaching could not load. Reopen this view to retry.");
      });
    return () => {
      active = false;
    };
  }, [api]);
  async function save(goals: Goal[]) {
    if (!overview) return;
    try {
      const result = await api<{ goals: Goal[]; goals_version: number }>(
        "/v1/coaching/goals",
        "PUT",
        {
          goals,
          expected_version: overview.goals_version,
        },
      );
      setOverview({ ...overview, ...result });
      setGoal("");
    } catch (error) {
      setOverview(await api<Overview>("/v1/coaching"));
      throw error;
    }
  }
  type Item = {
    key: string;
    title: string;
    /** How to work on it, in one line. */
    how?: string;
    detail?: string;
    review?: string;
    goal?: Goal;
    example?: Overview["examples"][number];
  };
  const items: Item[] = overview
    ? [
        ...overview.goals
          .filter((g) => g.status === "active")
          .map((g) => ({
            key: "goal-" + g.id,
            title: g.text,
            detail: "Your goal",
            goal: g,
          })),
        ...patterns.map((pattern) => ({
          key: "pattern-" + pattern.label,
          title: pattern.label,
          how: "Notice it in your next calls and try a different approach.",
          detail: `Linked to fewer good outcomes across ${pattern.samples} calls (not proven to cause them)`,
        })),
        ...(overview.improvement_groups ?? []).map((group) => ({
          key: "group-" + group.id,
          title: group.concept,
          how: group.approach || "Try a shorter, clearer response.",
          detail: `Seen in ${group.count} call${group.count === 1 ? "" : "s"}`,
          review: group.examples[0]?.call_id,
        })),
        ...(overview.recent_tips[0]?.tips ?? []).slice(0, 5).map((tip, i) => ({
          key: "tip-" + i,
          ...practicePoint(tip.text),
          detail: `From your call on ${date(overview.recent_tips[0].at)}`,
          review: overview.recent_tips[0].call_id,
        })),
        ...overview.examples.map((example) => ({
          key: "example-" + example.id,
          title: example.text,
          detail: "Team example",
          example,
        })),
      ]
    : [];
  const admin = ["owner", "admin"].includes(session.role);
  return (
    <section className="card padded coaching" aria-label="Coaching">
      <div className="section-heading">
        <div>
          <h2>Coaching</h2>
          <p className="muted small">
            What to practice and how. The call review agent updates this after
            each call.
          </p>
        </div>
      </div>
      {status && <p role="status">{status}</p>}
      {overview && !items.length && (
        <p className="muted">
          Nothing yet. After your calls are reviewed, what to work on appears
          here.
        </p>
      )}
      <ol className="coach-list">
        {items.map((item) => (
          <li key={item.key}>
            <div>
              <strong>{item.title}</strong>
              {item.how && <span className="coach-how">How: {item.how}</span>}
              {item.detail && <small>{item.detail}</small>}
            </div>
            <div className="coach-actions">
              {item.review && (
                <button
                  className="text-button"
                  onClick={() => void run(() => onReview(item.review!))}
                >
                  Open call
                </button>
              )}
              {item.goal && overview && (
                <>
                  <button
                    className="text-button"
                    onClick={() =>
                      void run(() =>
                        save(
                          overview.goals.map((g) =>
                            g.id === item.goal!.id
                              ? { ...g, status: "completed" }
                              : g,
                          ),
                        ),
                      )
                    }
                  >
                    Done
                  </button>
                  <button
                    className="text-button"
                    aria-label={`Remove goal ${item.goal.text}`}
                    onClick={() =>
                      void run(() =>
                        save(
                          overview.goals.filter((g) => g.id !== item.goal!.id),
                        ),
                      )
                    }
                  >
                    Remove
                  </button>
                </>
              )}
              {item.example && admin && (
                <button
                  className="text-button"
                  onClick={() =>
                    void run(async () => {
                      await api(
                        `/v1/admin/snippets/${item.example!.id}`,
                        "PATCH",
                        {
                          expected_version: item.example!.version,
                          approved: false,
                        },
                      );
                      setOverview(await api<Overview>("/v1/coaching"));
                      notice("Example withdrawn");
                    })
                  }
                >
                  Withdraw
                </button>
              )}
            </div>
          </li>
        ))}
      </ol>
      {overview &&
        overview.goals.filter((g) => g.status === "active").length < 3 && (
          <div className="inline-add">
            <input
              aria-label="Add a goal"
              placeholder="Add a goal, like “ask about timing before pitching”"
              value={goal}
              maxLength={300}
              onChange={(e) => setGoal(e.target.value)}
            />
            <button
              className="secondary"
              disabled={!goal.trim()}
              onClick={() =>
                void run(() =>
                  save([
                    ...overview.goals,
                    {
                      id: crypto.randomUUID(),
                      text: goal.trim(),
                      status: "active",
                    },
                  ]),
                )
              }
            >
              Add goal
            </button>
          </div>
        )}
    </section>
  );
}

export function ReviewTip({
  callId,
  version,
  pointId,
}: {
  callId: string;
  version: number;
  pointId: string;
}) {
  const { api, run, session, notice } = useWorkspace();
  const [review, setReview] = useState("");
  const [open, setOpen] = useState(false);
  const [saved, setSaved] = useState(false);
  if (!["owner", "admin"].includes(session.role)) return null;
  if (saved) return <small> · Published as a reviewed example</small>;
  return (
    <span>
      <button className="text-button" onClick={() => setOpen(!open)}>
        Review for team
      </button>
      {open && (
        <span>
          <label>
            Example review reference
            <input
              aria-label="Example review reference"
              value={review}
              maxLength={300}
              onChange={(e) => setReview(e.target.value)}
            />
          </label>
          <button
            disabled={!review.trim()}
            onClick={() =>
              void run(async () => {
                await api("/v1/admin/snippets", "POST", {
                  call_id: callId,
                  coaching_point_id: pointId,
                  expected_transcript_version: version,
                  review_reference: review,
                });
                setSaved(true);
                notice("Reviewed example published");
              })
            }
          >
            Publish reviewed example
          </button>
        </span>
      )}
    </span>
  );
}
