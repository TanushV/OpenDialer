/**
 * The Dialer in three fixed regions (R5.3): a status bar, the lead and script in their own scroll, and an
 * action bar. Each region has its own error boundary (R5.10), so the action bar survives a crash elsewhere.
 */
import { useEffect, useRef, useState, type ComponentProps } from "react";
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
import { revealScriptPart, ScriptReader } from "../components/ScriptReader";
import { CallbackReviewNotice, WrapUp } from "../components/WrapUp";
import { Button } from "../ui/kit";
import { headline, keys, type Action, type Phase } from "./screen";
import { OUTCOME_KEYS, useCallLoop } from "./useCallLoop";
import { useCallPlayback } from "./useCallPlayback";

type Loop = ReturnType<typeof useCallLoop>;

/** The page where the rep fixes a blocker, by the blocker's `fix` place. */
const FIX_PAGE: Record<string, string> = {
  settings: "Settings",
  numbers: "Numbers",
  leads: "Leads",
  script: "Script",
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
  const loop = useCallLoop();
  const { phase, act, call } = loop;
  useKeys(phase, act, visible, loop.call?.suggested_outcome?.code);
  const playback = useCallPlayback(
    call,
    phase.kind === "dialing" && !!call?.provider_id && !loop.inbound,
    loop.audio,
    loop.cueAudio,
    loop.telephony.current,
  );
  return (
    <div className="dialer" data-phase={phase.kind}>
      <audio ref={loop.audio} autoPlay />
      <audio ref={loop.cueAudio} />
      <ErrorBoundary>
        <StatusBar loop={loop} playback={playback} onNavigate={onNavigate} />
      </ErrorBoundary>
      <div className="dialer-body">
        <ErrorBoundary>
          <Body loop={loop} visible={visible} />
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
  playback,
  onNavigate,
}: {
  loop: Loop;
  playback: ReturnType<typeof useCallPlayback>;
  onNavigate?: (page: string) => void;
}) {
  const { phase, packet, act, testPhone, testList, problem, settling } = loop;
  const { run } = useWorkspace();
  const outcome = loop.call?.state === "FINALIZED" ? loop.call.outcome : null;
  const lead = packet?.lead ?? loop.testContact;
  const identity = lead?.company
    ? `${lead.name} · ${lead.company}`
    : lead?.name;
  const paused = phase.kind === "paused";
  const test = testPhone || testList;
  const target =
    phase.kind === "finished" ? FIX_PAGE[phase.fix ?? ""] : undefined;
  return (
    <header
      className="status-bar"
      data-call-blocked={Boolean(
        !problem &&
        ((paused && phase.blocker) ||
          (phase.kind === "finished" && phase.reason)),
      )}
    >
      <div
        className="status-text"
        role="status"
        aria-live="polite"
        aria-label="Call status"
      >
        <strong>
          {paused && test && !phase.blocker
            ? testList
              ? "Test list"
              : "Test call"
            : phase.kind === "saved"
              ? `${label(phase.outcome)} saved`
              : headline(phase)}
        </strong>
        {problem && (
          <div className="call-problem" role="alert" aria-label="Call problem">
            <strong>{problem.title}</strong>
            <span>{problem.message}</span>
            {problem.nextAction && <span>{problem.nextAction}</span>}
            {problem.fix && FIX_PAGE[problem.fix] && onNavigate && (
              <Button
                variant="text"
                onClick={() => onNavigate(FIX_PAGE[problem.fix!])}
              >
                Open {FIX_PAGE[problem.fix]}
              </Button>
            )}
          </div>
        )}
        {playback.warning && (
          <div
            className="call-problem"
            role="alert"
            aria-label="Audio playback problem"
          >
            <span>{playback.warning}</span>
            <Button variant="text" onClick={() => void run(playback.retry)}>
              Retry on system output
            </Button>
          </div>
        )}
        {paused && !test && !phase.blocker && (
          <Readiness onNavigate={onNavigate} />
        )}
        {paused && outcome && (
          <span className="muted">{label(outcome)} saved</span>
        )}
        {identity && !paused && phase.kind !== "finished" && (
          <span className="muted status-lead" title={identity}>
            {identity}
          </span>
        )}
        {phase.kind === "finished" && (
          <div className="muted">
            {phase.nextAction}
            {target && onNavigate && (
              <Button variant="text" onClick={() => onNavigate(target)}>
                Open {target}
              </Button>
            )}
          </div>
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
        <>
          {test && (
            <Button
              variant="text"
              disabled={settling}
              onClick={() => void act.endTest()}
            >
              End test session
            </Button>
          )}
          {!test && (
            <DialButton
              disabled={settling}
              onDial={(event) =>
                act.testPhone(event, () => onNavigate?.("Settings"))
              }
            >
              Test call
            </DialButton>
          )}
          <DialButton
            variant="primary"
            kbd="Space"
            disabled={settling || !!phase.blocker}
            onDial={act.start}
          >
            {testList
              ? "Run test list"
              : testPhone
                ? "Call test phone"
                : "Start calling"}
          </DialButton>
        </>
      ) : (
        <Button variant="secondary" kbd="P" onClick={() => void act.pause()}>
          Pause
        </Button>
      )}
    </header>
  );
}

function Body({ loop, visible }: { loop: Loop; visible: boolean }) {
  const { snapshot: w } = useWorkspace();
  const { phase, packet, call, telephony, testPhone, testList } = loop;
  // The call's own snapshot wins; a test call has no outreach packet, so it uses its base script.
  const script =
    (call?.script_snapshot &&
      (call.test_contact || call.lead_id === packet?.lead.id) &&
      call.script_snapshot) ||
    packet?.script ||
    (loop.testContact ? loop.testScript : null) ||
    undefined;
  const position = visible && isActiveCall(call) ? call?.script_position : null;
  const scriptPane = useRef<HTMLElement>(null);
  const between = phase.kind === "paused" || phase.kind === "finished";
  const testing = testList || testPhone;
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
          <AudioCheck telephony={telephony.current} act={loop.act} />
          {testList ? (
            <section className="card padded" aria-label="Test destinations">
              <p>
                Call each test phone with Dial now. You can test at any hour.
              </p>
              <ul>
                {testList.phones.map((phone) => (
                  <li key={phone}>{phone}</li>
                ))}
              </ul>
            </section>
          ) : testPhone ? (
            <p className="muted">
              This test calls {testPhone}. You can test at any hour.
            </p>
          ) : (
            <ListChoice listsKey={loop.listsKey} />
          )}
        </>
      )}
      {between && !testing && <DueTasks />}
      {(phase.kind === "talking" ||
        (phase.kind === "voicemail" && phase.final)) &&
        call && (
          <LiveCoach
            call={call}
            onChange={loop.act.reload}
            onShowSection={(key) => revealScriptPart(scriptPane.current, key)}
          />
        )}
      {loop.editing && call && (
        <WrapUp
          key={call.id}
          call={call}
          timezone={packet?.lead.timezone}
          onSaved={loop.act.saved}
        />
      )}
      {loop.testContact && (
        <div className="lead-and-script">
          <section className="card lead-card" aria-label="Test phone">
            <h2 data-testid="lead-name">{loop.testContact.name}</h2>
            <p className="muted">{loop.testContact.phone}</p>
            <p className="muted">
              Practice call: nothing here changes your lists, holds or CRM.
            </p>
          </section>
          <section
            ref={scriptPane}
            className="card script-card"
            aria-label="Script"
            data-testid="prepared-script"
          >
            <div className="card-eyebrow">
              <span>Script</span>
              <span className="pill subtle">Your script as written</span>
            </div>
            {script?.ready_for_rep === false || !script ? (
              <p className="hint" role="status">
                No saved script yet. Save one on the Script page and it shows
                here on your next test.
              </p>
            ) : (
              <ScriptReader
                key={call?.id ?? loop.testContact.id}
                script={script}
                position={position}
              />
            )}
          </section>
        </div>
      )}
      {packet && script && !loop.testContact ? (
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
            ref={scriptPane}
            className="card script-card"
            aria-label="Script"
            data-testid="prepared-script"
          >
            <div className="card-eyebrow">
              <span>Script</span>
              <span className={`pill ${script.fallback ? "subtle" : "green"}`}>
                {script.fallback
                  ? "Your script as written"
                  : "Written for this lead"}
              </span>
            </div>
            <ScriptReader
              key={call?.id ?? packet.lead.id}
              script={script}
              position={position}
            />
          </section>
        </div>
      ) : null}
      {import.meta.env.DEV && w.simulation && call && isActiveCall(call) && (
        <SimulationTools callId={call.id} allowTranscript />
      )}
    </>
  );
}

/** Development only: drive a simulated call the way the carrier would. */
function SimulationTools({
  callId,
  allowTranscript,
}: {
  callId: string;
  allowTranscript: boolean;
}) {
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
      {allowTranscript && (
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
      )}
    </div>
  );
}

/** Choose the microphone and headset before calling. */
function AudioCheck({
  telephony,
  act,
}: {
  telephony: Loop["telephony"]["current"];
  act: Loop["act"];
}) {
  const { snapshot: w, run } = useWorkspace();
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const [output, setOutput] = useState(telephony.outputDevice);
  const pick = (
    kind: MediaDeviceKind,
    name: string,
    choose: (id: string) => Promise<void>,
  ) => (
    <label>
      {name}
      <select
        value={kind === "audiooutput" ? output : undefined}
        onChange={(e) => {
          const id = e.target.value;
          void run(() => choose(id));
        }}
      >
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
        {pick("audiooutput", "Speaker / headset", async (id) => {
          await act.output(id);
          setOutput(id);
        })}
        <Button variant="secondary" onClick={() => void act.sampleAudio()}>
          Play test tone
        </Button>
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
  if (phase.kind === "paused" && !phase.blocker) return null;
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
        <>
          <Button kbd="Space" onClick={() => void act.skip()} disabled={!call}>
            Skip
          </Button>
          <HangUp act={act} disabled={!call} />
        </>
      )}
      {phase.kind === "voicemail" && (
        <>
          <Button
            variant="primary"
            kbd="Space"
            onClick={() => void act.voicemail()}
          >
            End as voicemail
          </Button>
          <Button variant="secondary" kbd="M" onClick={act.mute}>
            {muted ? "Unmute" : "Mute"}
          </Button>
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
          {phase.answer === "identifying" && (
            <Button variant="secondary" onClick={() => void act.voicemail()}>
              End as voicemail
            </Button>
          )}
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
      {phase.kind === "saved" && (
        <>
          <DialButton
            variant="primary"
            kbd="Space"
            disabled={
              loop.editing || (call?.is_test && !call.dial_scope?.test_run_id)
            }
            onDial={act.dial_now}
          >
            Dial now
          </DialButton>
          <Button variant="text" kbd="C" onClick={act.change_outcome}>
            Change outcome
          </Button>
        </>
      )}
      {phase.kind === "finished" && (
        <DialButton variant="primary" kbd="Space" onDial={act.dial_now}>
          Try dialing again
        </DialButton>
      )}
      {keypad && call && phase.kind === "talking" && (
        <Keypad key={call.id} callId={call.id} close={() => setKeypad(false)} />
      )}
    </footer>
  );
}

function HangUp({ act, disabled }: { act: Loop["act"]; disabled?: boolean }) {
  return (
    <Button
      variant="danger"
      kbd="Esc"
      disabled={disabled}
      onClick={() => void act.hangup()}
    >
      Hang up
    </Button>
  );
}

function DialButton({
  onDial,
  ...props
}: Omit<ComponentProps<typeof Button>, "onClick" | "onKeyDown"> & {
  onDial: (event: Event) => Promise<void>;
}) {
  return (
    <Button
      {...props}
      onClick={(event) => void onDial(event.nativeEvent)}
      onKeyDown={(event) => {
        if (event.code !== "Space" && event.key !== "Enter") return;
        // A held key can synthesize clicks on a focused button. Only handle its fresh keydown.
        event.preventDefault();
        void onDial(event.nativeEvent);
      }}
    />
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
        event.defaultPrevented ||
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
      else if (action === "start" || action === "dial_now")
        void act[action](event);
      else void act[action]();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);
}

interface ReadinessCheck {
  blockers: { code: string }[];
  callable_now: number;
  why_none: string | null;
  fix?: string;
  next_action?: string;
}

/** R9E.5: while paused, what Start would do, checked every minute so a problem shows before Start. */
function Readiness({ onNavigate }: { onNavigate?: (page: string) => void }) {
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
  if (!check || check.blockers.length) return null;
  const target = FIX_PAGE[check.fix ?? ""];
  return (
    <div className="muted" data-testid="readiness">
      {check.callable_now
        ? `${check.callable_now} ${check.callable_now === 1 ? "lead" : "leads"} can be called now.`
        : check.why_none}
      {check.next_action && <div>{check.next_action}</div>}
      {target && onNavigate && (
        <Button variant="text" onClick={() => onNavigate(target)}>
          Open {target}
        </Button>
      )}
    </div>
  );
}
