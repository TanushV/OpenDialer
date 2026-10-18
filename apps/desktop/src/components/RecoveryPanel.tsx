import { useEffect, useState } from "react";
import { label, useWorkspace } from "../context";

interface RecoveryItem {
  id: string;
  version: number;
  kind?: string;
  action?: string;
  status: string;
  provider_id: string | null;
  reason: string;
  can_end?: boolean;
  can_cancel?: boolean;
}
interface RecoveryState {
  commands: RecoveryItem[];
  agents: RecoveryItem[];
  jobs: {
    id: string;
    queue: string;
    attempts: number;
    reason: string;
    can_retry: boolean;
  }[];
  limited: boolean;
}

export function RecoveryPanel() {
  const { api, refresh } = useWorkspace();
  const [data, setData] = useState<RecoveryState | null>(null);
  const [references, setReferences] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");
  async function load() {
    setData(await api<RecoveryState>("/v1/admin/recovery"));
  }
  useEffect(() => {
    let current = true;
    void api<RecoveryState>("/v1/admin/recovery")
      .then((result) => {
        if (current) setData(result);
      })
      .catch((reason: unknown) => {
        if (current) setError(String(reason));
      });
    return () => {
      current = false;
    };
  }, [api]);
  async function act(id: string, path: string, body: unknown) {
    setBusy(id);
    setError("");
    try {
      await api(path, "POST", body);
      await load();
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(null);
    }
  }
  const verify = (item: RecoveryItem, kind: "commands" | "agents") => {
    const provider = references[item.id]?.trim();
    return act(item.id, `/v1/admin/recovery/${kind}/${item.id}`, {
      expected_version: item.version,
      action: "reconcile",
      ...(provider ? { provider_id: provider } : {}),
    });
  };
  const reference = (item: RecoveryItem) =>
    !item.provider_id && (
      <label>
        Original carrier reference
        <input
          aria-label="Original carrier reference"
          value={references[item.id] ?? ""}
          onChange={(event) =>
            setReferences({ ...references, [item.id]: event.target.value })
          }
        />
      </label>
    );
  return (
    <section className="card padded" aria-label="Work needing recovery">
      <h2>Work needing recovery</h2>
      <p className="muted small">
        Verify the original carrier receipt before retrying an uncertain effect.
        An active call alone does not confirm a transfer or transcription
        command. Retrying preparation may use the existing PAYG budget.
      </p>
      {error && (
        <p role="status" className="notice">
          {error}
        </p>
      )}
      {!data ? (
        <p>Loading recovery status…</p>
      ) : (
        <>
          {!data.commands.length &&
            !data.agents.length &&
            !data.jobs.length && <p>No work needs recovery.</p>}
          {data.commands.map((item) => (
            <article key={item.id} className="review-segment">
              <strong>
                {label(item.action || item.kind || "Call command")} ·{" "}
                {label(item.status)}
              </strong>
              <p>{item.reason}</p>
              {reference(item)}
              <button
                className="secondary"
                disabled={busy !== null}
                onClick={() => void verify(item, "commands")}
              >
                Verify carrier status
              </button>{" "}
              {item.can_end && (
                <button
                  className="secondary"
                  disabled={busy !== null}
                  onClick={() =>
                    void act(
                      item.id,
                      `/v1/admin/recovery/commands/${item.id}`,
                      { expected_version: item.version, action: "end_call" },
                    )
                  }
                >
                  End existing leg
                </button>
              )}{" "}
              {item.can_cancel && (
                <button
                  className="secondary"
                  disabled={busy !== null}
                  onClick={() =>
                    void act(
                      item.id,
                      `/v1/admin/recovery/commands/${item.id}`,
                      {
                        expected_version: item.version,
                        action: "cancel_unsent",
                      },
                    )
                  }
                >
                  Cancel unsent work
                </button>
              )}
            </article>
          ))}
          {data.agents.map((item) => (
            <article key={item.id} className="review-segment">
              <strong>Agent setup · {label(item.status)}</strong>
              <p>{item.reason}</p>
              {reference(item)}
              <button
                className="secondary"
                disabled={busy !== null}
                onClick={() => void verify(item, "agents")}
              >
                Verify agent status
              </button>
            </article>
          ))}
          {data.jobs.map((job) => (
            <article key={job.id} className="review-segment">
              <strong>
                {label(job.queue)} · {job.attempts} attempts
              </strong>
              <p>{job.reason}</p>
              {job.can_retry && (
                <button
                  className="secondary"
                  disabled={busy !== null}
                  onClick={() =>
                    void act(
                      job.id,
                      `/v1/admin/recovery/jobs/${job.id}/retry`,
                      { expected_attempts: job.attempts },
                    )
                  }
                >
                  Retry once
                </button>
              )}
            </article>
          ))}
          {data.limited && (
            <p className="fine">
              Showing a bounded page. Resolve items and refresh to continue.
            </p>
          )}
        </>
      )}
      <button
        className="text-button"
        disabled={busy !== null}
        onClick={() =>
          void load().catch((reason: unknown) => setError(String(reason)))
        }
      >
        Refresh recovery status
      </button>
    </section>
  );
}
