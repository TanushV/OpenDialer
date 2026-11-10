/**
 * The Dialer in three fixed regions (R5.3): a status bar, the lead and script in their own scroll, and an
 * action bar. Each region has its own error boundary (R5.10), so the action bar survives a crash elsewhere.
 */
import { useEffect, useRef, useState } from "react";
import { label, useWorkspace } from "../context";
import { isActiveCall } from "../types";
import { ApiError, openSource } from "../api/client";
import { ErrorBoundary } from "../components/ErrorBoundary";
import { DueTasks } from "../components/FollowupTasks";
import { InboundCall } from "../components/InboundCall";
import { Keypad } from "../components/Keypad";
import { DialLists } from "../components/LeadLists";
import { LiveCoach } from "../components/LiveCoach";
import { ProspectBrief } from "../components/ProspectBrief";
import { ScriptReader } from "../components/ScriptReader";
import { CallbackReviewNotice, WrapUp } from "../components/WrapUp";
import { Button } from "../ui/kit";
import { sessionAudio } from "../telephony/speech";
import { headline, keys, type Action, type Phase } from "./screen";
import { OUTCOME_KEYS, useCallLoop } from "./useCallLoop";

type Loop = ReturnType<typeof useCallLoop>;

/** The page where the rep fixes a blocker, by the blocker's `fix` place. */
const FIX_PAGE: Record<string, string> = {
  settings: "Settings",
  numbers: "Numbers",
  leads: "Leads",
  attention: "Review",
};

export function Dialer({
  visible = true,
  onNavigate,
}: {
  visible?: boolean;
  /** Opens the page that fixes the current blocker. */
  onNavigate?: (page: string) => void;
}) {
  const loop = useCallLoop(visible);
  const { phase, act } = loop;
  useKeys(phase, act, visible, loop.call?.suggested_outcome?.code);
  usePhaseSound(phase);
  return (
    <div className="dialer" data-phase={phase.kind}>
      <audio ref={loop.audio} autoPlay />
      <ErrorBoundary>
        <StatusBar loop={loop} onNavigate={onNavigate} />
      </ErrorBoundary>
      <div className="dialer-body">
        <ErrorBoundary>
          <Body loop={loop} />
        </ErrorBoundary>
      </div>
      <ErrorBoundary>
        <ActionBar loop={loop} />
      </ErrorBoundary>
    </div>
  );
}

function StatusBar({
  loop,
  onNavigate,
}: {
  loop: Loop;
  onNavigate?: (page: string) => void;
}) {
  const { phase, packet, act } = loop;
  const lead = packet?.lead;
  const paused = phase.kind === "paused";
  return (
    <header className="status-bar">
      <div
        className="status-text"
        role="status"
        aria-live="polite"
        aria-label="Call status"
      >
        <strong>{headline(phase)}</strong>
        {lead && !paused && (
          <span className="muted">
            {lead.name}
            {lead.company ? ` · ${lead.company}` : ""}
          </span>
        )}
        {paused && phase.blocker && phase.blocker.fix !== "dialer" && (
          <span className="muted">
            Fix this in {label(phase.blocker.fix)}; calling resumes on Start.{" "}
            {onNavigate && FIX_PAGE[phase.blocker.fix] && (
              <Button
                variant="text"
                onClick={() => onNavigate(FIX_PAGE[phase.blocker!.fix])}
              >
                Open {FIX_PAGE[phase.blocker.fix]}
              </Button>
            )}
          </span>
        )}
      </div>
      {loop.stopping ? (
        <span className="muted">Calling stops after this call</span>
      ) : paused ? (
        <Button
          variant="primary"
          kbd="Space"
          disabled={!!phase.blocker}
          onClick={() => void act.start()}
        >
          Start calling
        </Button>
      ) : (
        <Button variant="secondary" kbd="P" onClick={() => void act.pause()}>
          Pause
        </Button>
      )}
    </header>
  );
}

function Body({ loop }: { loop: Loop }) {
  const { snapshot: w } = useWorkspace();
  const { phase, packet, call, telephony } = loop;
  const between = phase.kind === "paused" || phase.kind === "finished";
  return (
    <>
      {loop.inbound && (
        <InboundCall
          key={loop.inbound.id + ":" + (loop.returnProvider || "waiting")}
          task={loop.inbound}
          canAccept={Boolean(
            loop.returnProvider &&
            telephony.current.incoming?.providerId === loop.returnProvider,
          )}
          accept={async () => {
            if (!loop.returnProvider)
              throw new Error("Wait for the verified return-call identity.");
            await telephony.current.acceptIncoming(loop.returnProvider);
          }}
          audioActive={Boolean(
            (w.simulation && loop.inbound.inbound_state === "connected") ||
            (loop.returnProvider &&
              telephony.current.returnActive(loop.returnProvider)),
          )}
          mute={() => {
            if (loop.returnProvider)
              telephony.current.mute(loop.returnProvider);
          }}
          localHangup={async () => {
            if (loop.returnProvider)
              await telephony.current.emergencyHangup(loop.returnProvider);
          }}
        />
      )}
      {loop.callbackReview && (
        <CallbackReviewNotice
          review={loop.callbackReview}
          onDismiss={loop.dismissReview}
        />
      )}
      {phase.kind === "paused" && (
        <>
          <AudioCheck telephony={telephony.current} />
          <ListChoice listsKey={loop.listsKey} />
        </>
      )}
      {between && <DueTasks />}
      {(phase.kind === "talking" || phase.kind === "voicemail_final") &&
        call && <LiveCoach call={call} onChange={loop.act.reload} />}
      {loop.editing && call && (
        <WrapUp
          key={call.id}
          call={call}
          timezone={packet?.lead.timezone}
          onSaved={loop.act.saved}
        />
      )}
      {packet ? (
        <div className="lead-and-script">
          <section className="card lead-card" aria-label="Lead">
            <h2 data-testid="lead-name">{packet.lead.name}</h2>
            <p className="muted">
              {packet.lead.company || "Company not provided"} ·{" "}
              {packet.lead.phone}
            </p>
            <ProspectBrief
              key={packet.lead.id}
              packet={packet}
              onChange={loop.setPacket}
            />
            {packet.research.facts.map((fact) => (
              <p className="fact" key={fact.id}>
                {fact.statement}{" "}
                <button
                  className="source-link"
                  onClick={() => void openSource(fact.source_url)}
                >
                  Source ↗
                </button>
              </p>
            ))}
          </section>
          <section
            className="card script-card"
            aria-label="Script"
            data-testid="prepared-script"
          >
            <div className="card-eyebrow">
              <span>Script</span>
              <span
                className={`pill ${packet.script.fallback ? "subtle" : "green"}`}
              >
                {packet.script.fallback
                  ? "Your script as written"
                  : "Written for this lead"}
              </span>
            </div>
            <ScriptReader script={packet.script} />
          </section>
        </div>
      ) : (
        between && (
          <p className="muted empty-lead">
            {phase.kind === "finished" ? (
              (phase.reason ??
              "Everyone eligible has been called for now. New leads and callbacks start by themselves.")
            ) : (
              <Readiness />
            )}
          </p>
        )
      )}
      {import.meta.env.DEV && w.simulation && call && isActiveCall(call) && (
        <SimulationTools callId={call.id} />
      )}
    </>
  );
}

/** Development only: drive a simulated call the way the carrier would. */
function SimulationTools({ callId }: { callId: string }) {
  const { api, run } = useWorkspace();
  const [text, setText] = useState("");
  const simulate = (type: string) =>
    api("/v1/simulation/event", "POST", {
      event_id: crypto.randomUUID(),
      event_type: type,
      call_id: callId,
    });
  async function addTurn() {
    if (!text.trim()) return;
    await api(`/v1/calls/${callId}/transcript`, "POST", {
      segment_id: crypto.randomUUID(),
      revision: 1,
      side: "remote",
      text,
      is_final: true,
    });
    setText("");
  }
  return (
    <div className="simulation-tools">
      <div className="simulation-buttons">
        <button
          className="small secondary"
          onClick={() =>
            void run(async () => {
              await simulate("call.answered");
              await simulate("call.machine.detection.ended:human");
            })
          }
        >
          Simulate human answer
        </button>
        <button
          className="small text-button"
          onClick={() =>
            void run(async () => {
              await simulate("call.answered");
              await simulate("call.machine.detection.ended:machine");
            })
          }
        >
          Mailbox greeting
        </button>
        <button
          className="small text-button"
          onClick={() =>
            void run(async () => {
              await simulate("call.machine.greeting.ended:beep_detected");
            })
          }
        >
          Mailbox beep
        </button>
      </div>
      <div className="input-row">
        <input
          aria-label="Simulation transcript"
          value={text}
          placeholder="Add a fictional prospect turn…"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void run(addTurn);
          }}
        />
        <button
          className="icon-button"
          aria-label="Add prospect turn"
          onClick={() => void run(addTurn)}
        >
          +
        </button>
      </div>
    </div>
  );
}

/** Choose the microphone and headset before calling. */
function AudioCheck({
  telephony,
}: {
  telephony: Loop["telephony"]["current"];
}) {
  const { snapshot: w, run } = useWorkspace();
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const pick = (
    kind: MediaDeviceKind,
    name: string,
    choose: (id: string) => Promise<void>,
  ) => (
    <label>
      {name}
      <select onChange={(e) => void run(() => choose(e.target.value))}>
        <option value="">System default</option>
        {devices
          .filter((d) => d.kind === kind)
          .map((d) => (
            <option key={d.deviceId} value={d.deviceId}>
              {d.label || name}
            </option>
          ))}
      </select>
    </label>
  );
  return (
    <details
      className="card padded check-audio"
      onToggle={(event) => {
        if (event.currentTarget.open && !w.simulation)
          void run(async () => setDevices(await telephony.devices()));
      }}
    >
      <summary>Check audio</summary>
      <div className="two-column">
        {pick("audioinput", "Microphone", (id) => telephony.input(id))}
        {pick("audiooutput", "Speaker / headset", (id) => telephony.output(id))}
      </div>
    </details>
  );
}

function ListChoice({ listsKey }: { listsKey: string }) {
  const { api, refresh } = useWorkspace();
  const [names, setNames] = useState<string[] | null>(() => {
    try {
      const saved = JSON.parse(localStorage.getItem(listsKey) || "null");
      return Array.isArray(saved) ? saved : null;
    } catch {
      return null;
    }
  });
  return (
    <DialLists
      names={names}
      disabled={false}
      change={async (next) => {
        const previous = names;
        setNames(next);
        localStorage.setItem(listsKey, JSON.stringify(next));
        try {
          await api("/v1/queue/release", "POST", {});
          await refresh();
        } catch (error) {
          // Only a refusal from the authority undoes the choice; a request cut off by a reload keeps it.
          if (error instanceof ApiError) {
            setNames(previous);
            localStorage.setItem(listsKey, JSON.stringify(previous));
          }
          throw error;
        }
      }}
      callback={async () => undefined}
    />
  );
}

function ActionBar({ loop }: { loop: Loop }) {
  const { phase, act, call, muted } = loop;
  const { api, run, refresh } = useWorkspace();
  const [keypad, setKeypad] = useState(false);
  useEffect(() => setKeypad(false), [call?.id]);
  const suggested = call?.suggested_outcome?.code;
  return (
    <footer className="action-bar" aria-label="Call actions">
      {phase.kind === "paused" && phase.blocker && (
        <p className="blocker" role="alert">
          {phase.blocker.message}
        </p>
      )}
      {phase.kind === "paused" &&
        phase.blocker?.stuck &&
        phase.blocker.code.startsWith("CONNECTION_") && (
          <Button
            onClick={() =>
              void run(async () => {
                await api("/v1/admin/managed-connection/retry", "POST");
                await refresh();
              })
            }
          >
            Reconnect
          </Button>
        )}
      {phase.kind === "connecting" && (
        <span className="muted">{loop.audioStatus || "Connecting audio"}</span>
      )}
      {phase.kind === "dialing" && (
        <Button kbd="Space" onClick={() => void act.skip()} disabled={!call}>
          Skip
        </Button>
      )}
      {phase.kind === "voicemail_skipping" && (
        <>
          <Button
            variant="secondary"
            kbd="Space"
            onClick={() => void act.skip()}
          >
            Skip now
          </Button>
          <Button variant="secondary" kbd="S" onClick={() => void act.stay()}>
            Stay
          </Button>
        </>
      )}
      {phase.kind === "voicemail_final" && (
        <>
          <span className="muted">
            Leave a short message: who you are, why you called, your number.
          </span>
          <HangUp act={act} />
        </>
      )}
      {phase.kind === "talking" && (
        <>
          <Button variant="secondary" kbd="M" onClick={act.mute}>
            {muted ? "Unmute" : "Mute"}
          </Button>
          <button className="secondary" onClick={() => setKeypad(!keypad)}>
            Keypad
          </button>
          <button className="secondary" onClick={() => void act.optOut()}>
            Opt out
          </button>
          <HangUp act={act} />
        </>
      )}
      {phase.kind === "ended" && (
        <div className="outcome-keys" role="group" aria-label="Save outcome">
          {OUTCOME_KEYS.map(([code, name], index) => (
            <Button
              key={code}
              variant={code === suggested ? "primary" : "secondary"}
              className={code === suggested ? "suggested" : ""}
              onClick={() => void act.save(code)}
            >
              <kbd className="ui-kbd">{index + 1}</kbd> {name}
            </Button>
          ))}
          <Button variant="text" onClick={() => act.edit(true)}>
            Add detail
          </Button>
        </div>
      )}
      {(phase.kind === "saved" || phase.kind === "skipped") && (
        <>
          <span className="muted">
            {label(phase.outcome)}
            {phase.kind === "skipped" ? " saved by the carrier" : " saved"}
          </span>
          <Button
            variant="primary"
            kbd="Space"
            onClick={() => void act.dial_now()}
          >
            Dial now
          </Button>
          {phase.kind === "saved" && (
            <Button variant="text" kbd="C" onClick={act.change_outcome}>
              Change outcome
            </Button>
          )}
        </>
      )}
      {keypad && call && phase.kind === "talking" && (
        <Keypad key={call.id} callId={call.id} close={() => setKeypad(false)} />
      )}
    </footer>
  );
}

function HangUp({ act }: { act: Loop["act"] }) {
  return (
    <Button variant="danger" kbd="Esc" onClick={() => void act.hangup()}>
      Hang up
    </Button>
  );
}

/** Keys follow `keys(phase)`; typing in a field never triggers them. Enter confirms a suggested outcome. */
function useKeys(
  phase: Phase,
  act: Loop["act"],
  visible: boolean,
  suggested?: string,
) {
  const latest = useRef({ phase, act, visible, suggested });
  latest.current = { phase, act, visible, suggested };
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      const { phase, act, visible, suggested } = latest.current;
      if (
        !visible ||
        event.repeat ||
        event.altKey ||
        event.ctrlKey ||
        event.metaKey
      )
        return;
      const target = event.target;
      // Typing stays typing. A focused button or link keeps only its own Space and Enter: Chromium (WebView2
      // on Windows) leaves focus on a clicked button, and Esc or 1 to 6 must still act on the call.
      if (
        target instanceof Element &&
        (target.closest(
          "input, textarea, select, [contenteditable], [role=dialog]",
        ) ||
          (target.closest("button, a") &&
            (event.code === "Space" || event.key === "Enter")))
      )
        return;
      if (phase.kind === "ended" && event.key === "Enter" && suggested) {
        event.preventDefault();
        void act.save(suggested);
        return;
      }
      const action: Action | undefined = keys(phase)[event.code];
      if (!action) return;
      event.preventDefault();
      if (typeof action === "object")
        void act.save(OUTCOME_KEYS[action.save - 1][0]);
      else if (action === "mute") act.mute();
      else if (action === "change_outcome") act.change_outcome();
      else void act[action]();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);
}

/** A short tone when a call ends, so the rep hears it without looking (R5.8). */
function usePhaseSound(phase: Phase) {
  const previous = useRef(phase.kind);
  useEffect(() => {
    if (phase.kind === "ended" && previous.current !== "ended") tone();
    previous.current = phase.kind;
  }, [phase.kind]);
}

function tone() {
  try {
    // The session's one engine, which Start calling unlocked; a new one per call could start suspended.
    const context = sessionAudio();
    void context.resume().catch(() => undefined);
    const oscillator = context.createOscillator();
    const gain = context.createGain();
    oscillator.frequency.value = 660;
    gain.gain.setValueAtTime(0.08, context.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.0001, context.currentTime + 0.25);
    oscillator.connect(gain).connect(context.destination);
    oscillator.start();
    oscillator.stop(context.currentTime + 0.25);
  } catch {
    /* No audio output: the status line still says the call ended. */
  }
}

interface ReadinessCheck {
  blockers: { code: string }[];
  callable_now: number;
  why_none: string | null;
}

/** R9E.5: while paused, what Start would do, checked every minute so a problem shows before Start. */
function Readiness() {
  const { api } = useWorkspace();
  const [check, setCheck] = useState<ReadinessCheck | null>(null);
  useEffect(() => {
    let current = true;
    const read = () =>
      void api<ReadinessCheck>("/v1/calling/readiness")
        .then((value) => current && setCheck(value))
        .catch(() => current && setCheck(null));
    read();
    const timer = window.setInterval(read, 60_000);
    return () => {
      current = false;
      window.clearInterval(timer);
    };
  }, [api]);
  if (!check || check.blockers.length)
    return (
      <>
        Press Start calling. Each lead and its script appear here as the call
        starts.
      </>
    );
  return (
    <span data-testid="readiness">
      {check.callable_now
        ? `Ready: ${check.callable_now} ${check.callable_now === 1 ? "lead" : "leads"} can be called now. Press Start calling.`
        : check.why_none}
    </span>
  );
}
