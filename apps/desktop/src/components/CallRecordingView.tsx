import { useEffect, useRef, useState } from "react";
import { useWorkspace } from "../context";
import type { Recording } from "../types";
import { recordingReason } from "./RecordingStatus";

interface Kept {
  recording: Partial<Recording>;
  transcript: {
    segment_id: string;
    side: "rep" | "remote";
    text: string;
    start_ms: number | null;
    end_ms: number | null;
  }[];
}

/** What a call kept (call insights plan P1 to P4): its audio, with the kept transcript in sync. */
/** The call's own numbers in one line (plan phase 6). */
export function metricsLine(metrics?: Record<string, unknown> | null) {
  if (!metrics) return null;
  const number = (key: string) =>
    typeof metrics[key] === "number" ? (metrics[key] as number) : null;
  const parts: string[] = [];
  const talk = number("talk_share");
  if (talk !== null) parts.push(`You talked ${Math.round(talk * 100)}%`);
  const monologue = number("longest_rep_monologue_ms");
  if (monologue) parts.push(`longest stretch ${Math.round(monologue / 1000)}s`);
  const gap = number("median_response_ms");
  if (gap !== null) parts.push(`answered in ${(gap / 1000).toFixed(1)}s`);
  const questions = number("rep_questions");
  if (questions) parts.push(`${questions} questions`);
  const pace = number("rep_words_per_minute");
  if (pace) parts.push(`${pace} words a minute`);
  const pitch = number("rep_pitch_range_semitones");
  if (pitch !== null) parts.push(`pitch range ${pitch} semitones`);
  return parts.length ? parts.join(" · ") : null;
}

export function CallRecordingView({
  callId,
  metrics,
}: {
  callId: string;
  metrics?: Record<string, unknown> | null;
}) {
  const { api, run, runtime, session } = useWorkspace();
  const [kept, setKept] = useState<Kept | null>(null);
  const [source, setSource] = useState<string | null>(null);
  const [at, setAt] = useState(0);
  const player = useRef<HTMLAudioElement>(null);
  useEffect(() => {
    let live = true;
    let url: string | null = null;
    void (async () => {
      const result = await api<Kept>(`/v1/calls/${callId}/recording`);
      if (!live) return;
      setKept(result);
      if (result.recording.status !== "saved" || !runtime) return;
      const response = await fetch(
        `${runtime.url.replace(/\/$/, "")}/v1/calls/${callId}/recording/audio`,
        { headers: { Authorization: `Bearer ${session.access_token}` } },
      );
      if (!live || !response.ok) return;
      url = URL.createObjectURL(await response.blob());
      setSource(url);
    })().catch(() => undefined);
    return () => {
      live = false;
      if (url) URL.revokeObjectURL(url);
    };
  }, [api, callId, runtime, session.access_token]);
  if (!kept) return null;
  const rules = kept.recording;
  if (!rules.status) return null;
  const gone =
    rules.status === "deleted" ||
    rules.status === "opt_out" ||
    rules.status === "expired";
  const why = gone
    ? {
        deleted: "Deleted.",
        opt_out: "Deleted when this number opted out.",
        expired: "Deleted automatically.",
      }[rules.status as "deleted" | "opt_out" | "expired"]
    : rules.prospect || rules.consent_at
      ? "Both sides kept."
      : `Your side kept. Prospect not kept: ${recordingReason(rules.reason ?? "unknown")}.`;
  return (
    <section className="card padded" aria-label="Recording">
      <h2>Recording</h2>
      <p className="fine">{why}</p>
      {metricsLine(metrics) && (
        <p className="fine" data-testid="call-numbers">
          {metricsLine(metrics)}
        </p>
      )}
      {source && (
        <audio
          ref={player}
          controls
          src={source}
          data-testid="call-audio"
          onTimeUpdate={(event) =>
            setAt(Math.round(event.currentTarget.currentTime * 1000))
          }
        />
      )}
      {!gone && rules.status === "no_audio" && (
        <p className="fine">No audio was captured on this call.</p>
      )}
      {!gone && rules.status === "failed" && (
        <p className="fine">
          The audio could not be saved; the transcript is kept.
        </p>
      )}
      {kept.transcript.length > 0 && (
        <ol className="kept-transcript" aria-label="Kept transcript">
          {kept.transcript.map((line) => {
            const current =
              line.start_ms !== null &&
              line.end_ms !== null &&
              at >= line.start_ms &&
              at < line.end_ms + 300;
            return (
              <li
                key={line.segment_id}
                className={current ? "current" : undefined}
                onClick={() => {
                  if (player.current && line.start_ms !== null)
                    player.current.currentTime = line.start_ms / 1000;
                }}
              >
                <strong>{line.side === "rep" ? "You" : "Prospect"}:</strong>{" "}
                {line.text}
              </li>
            );
          })}
        </ol>
      )}
      {!gone && (
        <button
          className="text"
          onClick={() =>
            void run(async () => {
              if (
                !window.confirm(
                  "Delete this call's recording and transcript? This cannot be undone.",
                )
              )
                return;
              await api(`/v1/calls/${callId}/recording`, "DELETE");
              setSource(null);
              setKept(await api<Kept>(`/v1/calls/${callId}/recording`));
            })
          }
        >
          Delete this recording
        </button>
      )}
    </section>
  );
}
