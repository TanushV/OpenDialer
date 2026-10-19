import { useEffect, useRef, useState } from "react";
import { date, useWorkspace } from "../context";
import type { Call, Coach } from "../types";
import { receiveSpeech } from "../state/privacy";
import { useSpeechExpiry } from "../state/useSpeechExpiry";

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
export function Coaching({
  onReview,
}: {
  onReview: (id: string) => Promise<void>;
}) {
  const { api, run, session, notice } = useWorkspace();
  const [overview, setOverview] = useState<Overview | null>(null);
  const [goal, setGoal] = useState("");
  const [scenario, setScenario] = useState("");
  const [status, setStatus] = useState("");
  const [waiting, setWaiting] = useState(false);
  const [answer, setAnswer] = useState<Pick<
    Call,
    "coach" | "transcript"
  > | null>(null);
  const pending = useRef<string | null>(null);
  useSpeechExpiry(answer, setAnswer);
  const hasScenario = !!scenario;
  useEffect(() => {
    const timer = setTimeout(() => setScenario(""), 300000);
    return () => clearTimeout(timer);
  }, [hasScenario]);
  useEffect(() => {
    let active = true;
    void api<Overview>("/v1/coaching")
      .then((data) => {
        if (active) setOverview(data);
      })
      .catch(() => {
        if (active)
          setStatus("Coaching could not load. Reopen this view to retry.");
      });
    return () => {
      active = false;
      pending.current = null;
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
  function discard() {
    pending.current = null;
    setScenario("");
    setAnswer(null);
    setWaiting(false);
    setStatus("");
  }
  async function practice() {
    if (!scenario.trim() || pending.current) return;
    const id = crypto.randomUUID();
    pending.current = id;
    setWaiting(true);
    setAnswer(null);
    setStatus("");
    const question = scenario;
    setScenario("");
    try {
      const result = await api<{
        status: string;
        guidance?: Coach;
        raw_remaining_ms?: number;
      }>("/v1/coaching/practice", "POST", {
        request_id: id,
        scenario: question,
      });
      if (pending.current !== id) return;
      if (result.status === "ready" && result.guidance) {
        setAnswer(
          receiveSpeech({
            transcript: [],
            coach: {
              ...result.guidance,
              id,
              private: true,
              remaining_ms: result.raw_remaining_ms,
            },
          }),
        );
      } else
        setStatus(
          "AI practice is unavailable. Your approved knowledge remains available.",
        );
    } finally {
      if (pending.current === id) {
        pending.current = null;
        setWaiting(false);
      }
    }
  }
  return (
    <section className="card padded" aria-label="Coaching and practice">
      <h2>Coaching and practice</h2>
      <p className="small muted">
        Practice never places a call or records a business outcome.
      </p>
      {status && <p role="status">{status}</p>}
      {overview && (
        <>
          <h3>Your practice goals</h3>
          {overview.goals.map((item) => (
            <div key={item.id}>
              <span>{item.text}</span>{" "}
              {item.status === "completed" ? (
                <span className="pill subtle">Completed</span>
              ) : (
                <button
                  className="text-button"
                  onClick={() =>
                    void run(() =>
                      save(
                        overview.goals.map((g) =>
                          g.id === item.id ? { ...g, status: "completed" } : g,
                        ),
                      ),
                    )
                  }
                >
                  Complete goal
                </button>
              )}
              <button
                className="text-button"
                aria-label={`Remove goal ${item.text}`}
                onClick={() =>
                  void run(() =>
                    save(overview.goals.filter((g) => g.id !== item.id)),
                  )
                }
              >
                Remove
              </button>
            </div>
          ))}
          <label>
            Practice goal
            <input
              aria-label="Practice goal"
              value={goal}
              maxLength={300}
              onChange={(e) => setGoal(e.target.value)}
            />
          </label>
          <button
            disabled={!goal.trim() || overview.goals.length >= 3}
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
            Save practice goal
          </button>
          <p className="fine">
            Up to three goals. Completion is your assessment; it does not imply
            conversion lift.
          </p>
          <details>
            <summary>Recent coaching opportunities</summary>
            <p>
              Up to 20 reviewed opportunities, grouped within comparable
              campaign and call-purpose context. These counts do not measure
              conversion lift.
            </p>
            {overview.improvement_groups?.map((group) => (
              <article key={group.id}>
                <strong>
                  {group.concept} · {group.count} reviewed opportunities
                </strong>
                {group.approach && <p>{group.approach}</p>}
                {group.examples.map((example) => (
                  <p key={example.observation_id}>
                    {example.text}{" "}
                    <button
                      className="text-button"
                      onClick={() => void run(() => onReview(example.call_id))}
                    >
                      Review example
                    </button>
                  </p>
                ))}
              </article>
            ))}
            <p className="small muted">
              Your latest {overview.inspected_calls} calls, up to{" "}
              {overview.history_limit}. Tips are interpretations; review the
              call context. Controlled comparisons are available in
              Intelligence.
            </p>
            {overview.recent_tips.map((entry) => (
              <div key={entry.call_id}>
                <button
                  className="text-button"
                  onClick={() => void run(() => onReview(entry.call_id))}
                >
                  Review coaching from {date(entry.at)}
                </button>
                <ul>
                  {entry.tips.map((tip, i) => (
                    <li key={i}>{tip.text}</li>
                  ))}
                </ul>
              </div>
            ))}
            {!overview.recent_tips.length && (
              <p>No retained coaching tips yet.</p>
            )}
          </details>
          <details>
            <summary>Reviewed examples</summary>
            {!overview.examples.length && (
              <p>
                No team examples have been approved yet. Current product
                knowledge still guides the helper.
              </p>
            )}
            {overview.examples.map((example) => (
              <div key={example.id}>
                <p>{example.text}</p>
                <small>
                  Human reviewed example
                  {example.raw_evidence_status === "expired"
                    ? " · Original speech expired"
                    : ""}
                </small>
                {["owner", "admin"].includes(session.role) && (
                  <button
                    className="text-button"
                    onClick={() =>
                      void run(async () => {
                        await api(`/v1/admin/snippets/${example.id}`, "PATCH", {
                          expected_version: example.version,
                          approved: false,
                        });
                        setOverview(await api<Overview>("/v1/coaching"));
                        notice("Example withdrawn");
                      })
                    }
                  >
                    Withdraw example
                  </button>
                )}
              </div>
            ))}
          </details>
        </>
      )}
      <h3>Private practice</h3>
      <label>
        Practice scenario
        <textarea
          aria-label="Practice scenario"
          value={scenario}
          maxLength={2000}
          onChange={(e) => setScenario(e.target.value)}
        />
      </label>
      <button
        disabled={waiting || !scenario.trim()}
        onClick={() => void run(practice)}
      >
        Practice privately
      </button>
      {(waiting || answer?.coach) && (
        <button className="text-button" onClick={discard}>
          Discard practice
        </button>
      )}
      {waiting && <p role="status">Working through the scenario…</p>}
      {answer?.coach && (
        <div aria-live="polite">
          <p>{answer.coach.say_or_ask}</p>
          <p className="small muted">{answer.coach.reason}</p>
        </div>
      )}
      <p className="fine">
        Private text expires within five minutes. Connected AI practice uses
        your configured PAYG budget; discarding a submitted request cannot
        reverse a provider charge.
      </p>
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
