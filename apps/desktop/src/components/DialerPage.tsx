import { useSpeechExpiry } from "../state/useSpeechExpiry";
import { ScriptReader } from "./ScriptReader";
import { LiveCoach } from "./LiveCoach";
import { CallingHealth } from "./WorkTime";
import { ProspectBrief } from "./ProspectBrief";
import {
  ArrowRight,
  Check,
  Clock3,
  FileText,
  Headphones,
  Layers3,
  Pause,
  Phone,
  Plus,
  ShieldCheck,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { openSource } from "../api/client";
import { label, useWorkspace } from "../context";
import { OptOutJournal } from "../state/optouts";
import { pausesPresence, Telephony } from "../telephony/client";
import { InboundCall } from "./InboundCall";
import { CallControls } from "./CallControls";
import { CallbackReviewNotice, WrapUp } from "./WrapUp";
import { nextLeadWithRetry } from "../state/queue";
import { DueTasks } from "./FollowupTasks";
import type { Call, Packet, Reservation } from "../types";
import { isActiveCall, isTerminal } from "../types";

/** The rep owes an outcome: the call ended and its wrap-up is unsaved. */
const owesWrapUp = (call: Call | null) =>
  !!call && isTerminal(call) && call.state !== "FINALIZED";

export function DialerPage({ visible = true }: { visible?: boolean }) {
  const {
    snapshot: w,
    session,
    api,
    refresh,
    notice,
    run,
    subscribe,
    liveStatus,
  } = useWorkspace();
  const [reservation, setReservation] = useState<Reservation | null>(
    w.reservation ?? null,
  );
  const [packet, setPacket] = useState<Packet | null>(null);
  const [call, setCall] = useState<Call | null>(null);
  const [callbackReview, setCallbackReview] =
    useState<Call["callback_review"]>(null);
  const currentCall = useRef(call);
  currentCall.current = call;
  useEffect(() => {
    const restored = w.reservation;
    if (!restored) return;
    let cancelled = false;
    void run(async () => {
      const value = await api<Packet>(`/v1/leads/${restored.lead.id}/packet`);
      if (!cancelled) setPacket(value);
    });
    return () => {
      cancelled = true;
    };
  }, []);
  const callReadGeneration = useRef(0);
  const hangupGeneration = useRef(0);
  useSpeechExpiry(call, setCall);
  const [dialing, setDialing] = useState(false);
  const [autoNext, setAutoNext] = useState(false);
  const [pauseAfter, setPauseAfter] = useState(false);
  const pausedAfterCall = useRef<string | null>(null);
  const [autoPending, setAutoPending] = useState(false);
  const advancedVoicemail = useRef<string | null>(null);
  const advancingCall = useRef(false);
  const advanceRef = useRef<() => Promise<void>>(async () => {});
  const [finding, setFinding] = useState(false);
  const [readying, setReadying] = useState(false);
  const readyingNow = useRef(false);
  const [transcript, setTranscript] = useState("");
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const [audioStatus, setAudioStatus] = useState("");
  const [vmAsset, setVMAsset] = useState("");
  const eventReceived = useRef<number | null>(null);
  const responseReceived = useRef<number | null>(null);
  const audio = useRef<HTMLAudioElement>(null);
  const telephony = useRef(new Telephony());
  const journal = useRef(new OptOutJournal(localStorage, session.tenant_id));
  const [journalReady, setJournalReady] = useState(false);
  const [pendingOptouts, setPendingOptouts] = useState(
    journal.current.blocksCalling,
  );
  const ready = w.user.presence.status === "ready";
  const active = isActiveCall(call);
  const wrapUpPending = owesWrapUp(call);
  const inbound = w.tasks.find(
    (t) =>
      t.inbound && t.routed_rep_id === w.user.id && t.inbound_state !== "ended",
  );
  const returnProvider = inbound?.routing_attempts
    ?.slice()
    .reverse()
    .find(
      (attempt) =>
        attempt.rep_id === w.user.id &&
        !["ended", "failed", "cancelled"].includes(attempt.state),
    )?.provider_id;
  const incomingAudio = telephony.current.hasIncoming;
  const selectedLead = packet?.lead;
  const callingNumber =
    reservation?.number ??
    w.numbers.find(
      (n) => n.id === (call?.number_id ?? packet?.bound_number_id),
    );
  // The backend makes the final decision; this only hides an unusable control.
  const finalAttempt =
    !!call?.attempt_ordinal &&
    !!call.attempt_allowance &&
    call.attempt_ordinal >=
      Math.max(call.attempt_allowance, w.policy.max_attempts ?? 4);

  useEffect(() => {
    const controller = telephony.current;
    return () => {
      controller.onStatus = () => {};
      controller.disconnect();
    };
  }, []);

  function applyCallUpdate(value: Call) {
    setCall((current) =>
      current?.id === value.id &&
      value.version >= current.version &&
      value.transcript_version >= current.transcript_version
        ? value
        : current,
    );
  }
  async function reloadCall(id: string) {
    const generation = ++callReadGeneration.current;
    const value = await api<Call>(`/v1/calls/${id}`);
    if (generation === callReadGeneration.current) applyCallUpdate(value);
  }
  useEffect(() => {
    const owned = w.calls.filter((c) => c.rep_id === w.user.id);
    const current =
      owned.find((c) => !isTerminal(c)) || owned.find((c) => owesWrapUp(c));
    if (current && !call && !reservation) {
      setCall(current);
      void run(async () =>
        setPacket(await api<Packet>(`/v1/leads/${current.lead_id}/packet`)),
      );
    }
  }, [w.calls]);
  useEffect(() => {
    if (!call) return;
    let disposed = false;
    let pending = false;
    let dirty = false;
    const load = () => {
      if (disposed) return;
      if (pending) {
        // The in-flight read predates this event: it is discarded on arrival
        // and read again. A slow read must not also discard a newer explicit
        // reload (a rep's own mute or help request) issued meanwhile.
        dirty = true;
        return;
      }
      const generation = ++callReadGeneration.current;
      pending = true;
      const current = () =>
        !disposed && !dirty && generation === callReadGeneration.current;
      void api<Call>("/v1/calls/" + call.id)
        .then((value) => {
          if (current()) {
            responseReceived.current = performance.now();
            applyCallUpdate(value);
          }
        })
        .catch(() => {
          if (current())
            setAudioStatus("Authority unavailable; calling is paused.");
        })
        .finally(() => {
          pending = false;
          if (dirty && !disposed) {
            dirty = false;
            load();
          }
        });
    };
    load();
    const unsubscribe = subscribe((events) => {
      if (
        events.some(
          (e) => e.entity_id === call.id || e.type === "knowledge.updated",
        )
      ) {
        eventReceived.current = performance.now();
        load();
      }
    });
    return () => {
      disposed = true;
      ++callReadGeneration.current;
      unsubscribe();
    };
  }, [call?.id, api, subscribe]);
  useEffect(() => {
    telephony.current.onStatus = (status) => {
      setAudioStatus(status);
      // A transient reconnect mid-call, or the registration that Go ready is
      // replacing, must not post a paused presence.
      if (
        readyingNow.current ||
        !pausesPresence(status, isActiveCall(currentCall.current))
      )
        return;
      void api("/v1/rep/presence", "POST", {
        status: "paused",
        audio_ready: false,
        telephony_ready: false,
      }).catch(() => {});
    };
  }, [api]);
  useEffect(() => {
    if (returnProvider) telephony.current.expectReturnLeg(returnProvider);
  }, [returnProvider]);

  async function syncOptouts() {
    await journal.current.synchronize((event) =>
      api("/v1/suppressions", "POST", {
        phone: event.phone,
        reason: event.reason,
        event_id: event.id,
      }),
    );
    setPendingOptouts(journal.current.blocksCalling);
  }
  useEffect(() => {
    void run(async () => {
      await syncOptouts();
      setJournalReady(true);
    });
  }, []);
  useEffect(() => {
    if (!journalReady) return;
    void syncOptouts();
    const handler = () => {
      void syncOptouts();
    };
    window.addEventListener("online", handler);
    return () => window.removeEventListener("online", handler);
  }, [journalReady]);
  async function goReady() {
    // One registration at a time: a second click must not create another agent leg.
    if (readyingNow.current) return;
    readyingNow.current = true;
    setReadying(true);
    try {
      if (inbound || telephony.current.hasIncoming)
        throw new Error(
          "Complete the incoming call before becoming ready for outbound calls.",
        );
      if (!journalReady)
        throw new Error("Wait for pending opt-outs to synchronize.");
      await syncOptouts();
      if (journal.current.blocksCalling)
        throw new Error(
          "Sync the pending opt-out before further outbound calls.",
        );
      let agent = "agent-simulated";
      if (!w.simulation) {
        setAudioStatus("Checking microphone and registering…");
        const result = await api<{ token: string }>(
          "/v1/telephony/token",
          "POST",
        );
        await telephony.current.connect(result.token, audio.current!);
        const leg = await api<{ agent_call_control_id: string }>(
          "/v1/telephony/agent",
          "POST",
        );
        agent = leg.agent_call_control_id;
        telephony.current.expectAgentLeg(agent);
        let answered = false;
        for (let i = 0; i < 60; i++) {
          const status = await api<{ state: string }>(
            `/v1/telephony/agent/${encodeURIComponent(agent)}`,
          );
          if (status.state === "answered") {
            answered = true;
            break;
          }
          await new Promise((r) => setTimeout(r, 500));
        }
        if (!answered)
          throw new Error("The verified agent audio leg did not become ready.");
      }
      await api("/v1/rep/presence", "POST", {
        status: "ready",
        audio_ready: true,
        telephony_ready: true,
        agent_call_control_id: agent,
        pending_optouts: 0,
      });
      setAudioStatus(w.simulation ? "Simulated audio ready" : "Audio ready");
      await refresh();
    } finally {
      readyingNow.current = false;
      setReadying(false);
    }
  }
  async function pause(afterHangup = false) {
    await api("/v1/rep/presence", "POST", {
      status: "paused",
      audio_ready: w.user.presence.audio_ready ?? false,
      telephony_ready: false,
    });
    // Read the current call rather than this render's: a hang-up just ended it.
    if (afterHangup || !isActiveCall(currentCall.current)) {
      await telephony.current.releaseIdleAgent();
      setReservation(null);
    }
    await refresh();
  }
  async function getNext() {
    if (inbound || telephony.current.hasIncoming)
      throw new Error(
        "Complete the incoming call before selecting another lead.",
      );
    if (pendingOptouts)
      throw new Error(
        "A pending opt-out must synchronize before another call.",
      );
    if (owesWrapUp(currentCall.current))
      throw new Error("Save this call's outcome before the next lead.");
    const next = await nextLeadWithRetry(
      () =>
        api<Reservation>("/v1/queue/next", "POST", {
          campaign_id: w.campaign.id,
        }),
      { onWaiting: setFinding },
    );
    setReservation(next);
    setCallbackReview(null);
    setPacket(await api(`/v1/leads/${next.lead.id}/packet`));
    setCall(null);
    return next;
  }
  async function startCall(selected = reservation) {
    if (inbound || telephony.current.hasIncoming)
      throw new Error(
        "Complete the incoming call before starting an outbound call.",
      );
    if (!selected || dialing || pendingOptouts) return;
    setDialing(true);
    try {
      setCall(
        await api("/v1/calls/start", "POST", {
          reservation_id: selected.reservation_id,
          idempotency_key: selected.reservation_id,
        }),
      );
      await refresh();
    } finally {
      setDialing(false);
    }
  }
  async function advanceCall() {
    if (advancingCall.current) return;
    advancingCall.current = true;
    try {
      const next = reservation && !call ? reservation : await getNext();
      await startCall(next);
    } finally {
      advancingCall.current = false;
    }
  }
  async function hangup(pauseAfter = false) {
    if (!call) return;
    const id = call.id;
    const controller = telephony.current;
    const audioIdentity = controller.activeIdentity;
    const generation = ++hangupGeneration.current;
    const current = () =>
      currentCall.current?.id === id && generation === hangupGeneration.current;
    try {
      const result = await api<Call>(`/v1/calls/${id}/hangup`, "POST");
      if (!current()) return;
      ++callReadGeneration.current;
      applyCallUpdate(result);
      if (audioIdentity) await controller.emergencyHangup(audioIdentity);
    } catch (e) {
      if (!current()) return;
      if (audioIdentity) await controller.emergencyHangup(audioIdentity);
      throw e;
    }
    if (!current()) return;
    if (pauseAfter) {
      pausedAfterCall.current = id;
      await pause(true);
    }
    await refresh();
  }
  async function optOut() {
    if (!selectedLead) return;
    // Journal locally, end the conversation at once, then synchronize.
    journal.current.add(selectedLead.phone, "opt_out");
    setPendingOptouts(true);
    const [ended] = await Promise.allSettled([
      isActiveCall(currentCall.current) ? hangup() : Promise.resolve(),
    ]);
    await syncOptouts();
    void refresh().catch(() => {});
    notice(
      journal.current.blocksCalling
        ? "Opt-out saved locally. Outbound calling is blocked until synchronization."
        : "Opt-out saved. This endpoint is suppressed.",
    );
    if (ended.status === "rejected") throw ended.reason;
  }
  function wrapUpSaved(result: Call) {
    setCall(result);
    setCallbackReview(result.callback_review ?? null);
    // The outcome is final. Refreshing next-call context reports separately.
    void run(async () => {
      try {
        await refresh();
        const updated = await api<Packet>(`/v1/leads/${result.lead_id}/packet`);
        setPacket((shown) =>
          shown?.lead.id === result.lead_id ? updated : shown,
        );
      } catch (error) {
        throw new Error(
          `Outcome saved. Refreshing the lead brief failed: ${error instanceof Error ? error.message : "unknown error"}`,
        );
      }
    });
  }
  async function simulate(type: string) {
    if (!call) return;
    await api("/v1/simulation/event", "POST", {
      event_id: crypto.randomUUID(),
      event_type: type,
      call_id: call.id,
    });
    await reloadCall(call.id);
  }
  async function addTurn() {
    if (!call || !transcript.trim()) return;
    await api(`/v1/calls/${call.id}/transcript`, "POST", {
      segment_id: crypto.randomUUID(),
      revision: 1,
      side: "remote",
      text: transcript,
      is_final: true,
    });
    setTranscript("");
    await reloadCall(call.id);
    await refresh();
  }
  useEffect(() => {
    if (
      pauseAfter &&
      ready &&
      call &&
      isTerminal(call) &&
      pausedAfterCall.current !== call.id
    ) {
      pausedAfterCall.current = call.id;
      void run(() => pause(true));
    }
  }, [pauseAfter, ready, call?.id, call?.state, run]);
  advanceRef.current = advanceCall;
  useEffect(() => {
    if (
      !autoNext ||
      pauseAfter ||
      !visible ||
      !ready ||
      pendingOptouts ||
      !journalReady ||
      inbound ||
      incomingAudio ||
      dialing ||
      finding ||
      call?.state !== "FINALIZED" ||
      call.outcome !== "voicemail" ||
      advancedVoicemail.current === call.id
    )
      return;
    setAutoPending(true);
    const id = call.id;
    const timer = setTimeout(() => {
      advancedVoicemail.current = id;
      setAutoPending(false);
      void run(() => advanceRef.current());
    }, 5000);
    return () => {
      clearTimeout(timer);
      setAutoPending(false);
    };
  }, [
    autoNext,
    pauseAfter,
    visible,
    ready,
    pendingOptouts,
    journalReady,
    inbound,
    incomingAudio,
    dialing,
    finding,
    call?.id,
    call?.state,
    call?.outcome,
    run,
  ]);
  const idle = !packet && !call;
  // Next work shows between calls: not mid-call, owed a wrap-up or about to dial.
  const betweenCalls = !active && !wrapUpPending && !(reservation && !call);
  return (
    <>
      <audio ref={audio} autoPlay />
      {inbound && (
        <InboundCall
          key={inbound.id + ":" + (returnProvider || "waiting")}
          task={inbound}
          canAccept={Boolean(
            returnProvider &&
            telephony.current.incoming?.providerId === returnProvider,
          )}
          accept={async () => {
            if (!returnProvider)
              throw new Error("Wait for the verified return-call identity.");
            await telephony.current.acceptIncoming(returnProvider);
          }}
          audioActive={Boolean(
            (w.simulation && inbound.inbound_state === "connected") ||
            (returnProvider && telephony.current.returnActive(returnProvider)),
          )}
          mute={() => {
            if (returnProvider) telephony.current.mute(returnProvider);
          }}
          localHangup={async () => {
            if (returnProvider)
              await telephony.current.emergencyHangup(returnProvider);
          }}
        />
      )}
      {!idle && (!call || active) && (
        <LiveCoach
          deliveryTiming={{
            receivedAt: responseReceived.current,
            eventAt: eventReceived.current,
          }}
          call={call}
          onChange={() => (call ? reloadCall(call.id) : Promise.resolve())}
        />
      )}
      <div className={`page-heading ${packet ? "calling-heading" : ""}`}>
        <div>
          <span className="eyebrow">{w.campaign.name}</span>
          {!packet && (
            <>
              <h1>
                Your next conversation<span className="soft-dot">.</span>
              </h1>
              <p>Arrive prepared. Listen closely. Leave a clear next step.</p>
            </>
          )}
        </div>
        <button
          className={ready ? "secondary ready-button" : "primary"}
          disabled={pendingOptouts || !journalReady || readying}
          onClick={() => void run(ready ? pause : goReady)}
        >
          {ready ? <Pause size={16} /> : <Headphones size={17} />}{" "}
          {ready ? "Pause dialing" : readying ? "Getting ready…" : "Go ready"}
        </button>
      </div>
      {pendingOptouts && (
        <div className="alert">
          <ShieldCheck size={18} /> A local opt-out is awaiting synchronization.
          New calls are blocked.
          <button onClick={() => void run(syncOptouts)}>Retry sync</button>
        </div>
      )}
      {!ready && !active && !wrapUpPending && (
        <details
          className="card padded check-audio"
          onToggle={(event) => {
            if (event.currentTarget.open && !w.simulation)
              void run(async () =>
                setDevices(await telephony.current.devices()),
              );
          }}
        >
          <summary>Check audio</summary>
          <div className="two-column">
            <label>
              Microphone
              <select
                onChange={(e) =>
                  void run(() => telephony.current.input(e.target.value))
                }
              >
                <option value="">System default</option>
                {devices
                  .filter((d) => d.kind === "audioinput")
                  .map((d) => (
                    <option key={d.deviceId} value={d.deviceId}>
                      {d.label || "Microphone"}
                    </option>
                  ))}
              </select>
            </label>
            <label>
              Speaker / headset
              <select
                onChange={(e) =>
                  void run(() => telephony.current.output(e.target.value))
                }
              >
                <option value="">System default</option>
                {devices
                  .filter((d) => d.kind === "audiooutput")
                  .map((d) => (
                    <option key={d.deviceId} value={d.deviceId}>
                      {d.label || "Audio output"}
                    </option>
                  ))}
              </select>
            </label>
          </div>
          <p className="fine">
            {w.simulation
              ? "Simulation uses no microphone or carrier audio."
              : "Device names appear after microphone permission. Bluetooth, routing and long-call behavior need a hardware check."}
          </p>
        </details>
      )}
      {betweenCalls && <DueTasks />}
      {idle && <CallingHealth />}
      {idle && (
        <details className="dialer-details">
          <summary>Details</summary>
          <p className="fine">Conversation updates: {label(liveStatus)}</p>
          <div className="metric-row">
            <Metric
              title="LEADS IN YOUR COHORT"
              value={String(w.metrics.original_cohort ?? "—")}
              detail={`${w.metrics.eligible_now ?? "Unknown"} currently eligible`}
            />
            <Metric
              title="UNIQUE RIGHT-PARTY REACH"
              value={String(w.metrics.right_party_unique ?? "—")}
              detail="Across the original cohort"
            />
            <Metric
              title="HELD MEETINGS"
              value={String(w.metrics.held_meetings ?? "—")}
              detail="Confirmed outcomes"
            />
            <Metric
              title="COVERAGE OVERDUE"
              value={String(w.metrics.coverage_overdue ?? "—")}
              detail="No lead left behind"
              last
            />
          </div>
        </details>
      )}
      {call && wrapUpPending && (
        <WrapUp
          key={call.id}
          call={call}
          timezone={packet?.lead.timezone}
          onSaved={wrapUpSaved}
        />
      )}
      {visible && call?.state === "FINALIZED" && (
        <details className="card padded">
          <summary>
            Saved: {label(call.outcome || "Outcome")} ·{" "}
            {call.outcome_source === "ai_reviewed"
              ? "AI reviewed"
              : call.outcome_source === "carrier"
                ? "Carrier result"
                : "Your outcome"}{" "}
            · Correct outcome
          </summary>
          <WrapUp
            key={call.id}
            call={call}
            timezone={packet?.lead.timezone}
            onSaved={wrapUpSaved}
          />
        </details>
      )}
      {callbackReview && (
        <CallbackReviewNotice
          review={callbackReview}
          onDismiss={() => setCallbackReview(null)}
        />
      )}
      <div className="dialer-grid">
        <section className="card lead-card">
          <div className="card-eyebrow">
            <span>
              <UsersIcon /> LEAD PACKET
            </span>
            <span className="pill subtle">
              {selectedLead ? "Ready to review" : "Up next"}
            </span>
          </div>
          {selectedLead ? (
            <>
              <div className="lead-identity">
                <span className="lead-avatar">
                  {selectedLead.name
                    .split(" ")
                    .map((n) => n[0])
                    .slice(0, 2)
                    .join("")}
                </span>
                <div>
                  <h2 data-testid="lead-name">{selectedLead.name}</h2>
                  <p>{selectedLead.company || "Company not provided"}</p>
                </div>
              </div>
              <div className="lead-details">
                <p>
                  <Phone size={14} />
                  {selectedLead.phone}
                </p>
                <p>
                  <Clock3 size={14} />
                  {selectedLead.attempt_count} previous attempts
                </p>
              </div>
              <div className="section-rule" />
              <ProspectBrief
                key={packet.lead.id}
                packet={packet}
                onChange={setPacket}
                number={callingNumber}
              />
              <span className="eyebrow">WHAT WE KNOW</span>
              {packet.research.facts.length ? (
                packet.research.facts.map((f) => (
                  <div className="fact" key={f.id}>
                    <Check size={13} />
                    <div>
                      {f.statement}
                      <button
                        className="source-link"
                        onClick={() => void openSource(f.source_url)}
                      >
                        View source ↗
                      </button>
                    </div>
                  </div>
                ))
              ) : (
                <p className="muted small">
                  No verified company research yet. Use the approved
                  introduction and ask what is relevant.
                </p>
              )}
              <div className="lead-bottom">
                <ShieldCheck size={14} />
                <span>
                  {selectedLead.suppressed
                    ? "Endpoint suppressed"
                    : "Permission is rechecked when dialing"}
                </span>
              </div>
            </>
          ) : (
            <div className="empty-lead">
              <div className="orb">
                <Phone size={29} strokeWidth={1.5} />
                <span className="orb-spark">✦</span>
              </div>
              <h3>A person, not another dial.</h3>
              <p>
                Your next lead arrives with context,
                <br />a clear introduction, and room to listen.
              </p>
              <button
                className="primary"
                disabled={
                  !ready || active || pendingOptouts || !journalReady || finding
                }
                onClick={() =>
                  void run(async () => {
                    await getNext();
                  })
                }
              >
                View next lead
                <ArrowRight size={16} />
              </button>
              {!ready && <small>Go ready to begin your session</small>}
            </div>
          )}
        </section>
        <section className="card script-card" data-testid="prepared-script">
          <div className="card-eyebrow">
            <span>
              <FileText size={15} /> YOUR TALK TRACK
            </span>
            <span className="pill subtle">
              {packet
                ? packet.script.fallback
                  ? packet.preparation?.status === "failed"
                    ? "Preparation failed · generic"
                    : packet.preparation?.status === "stale"
                      ? "Refresh due · generic"
                      : "Generic baseline"
                  : "Prepared"
                : "Approved playbook"}
            </span>
          </div>
          <ScriptReader
            script={
              packet?.script || {
                opener: w.active_playbook?.opener.replace("{rep}", w.user.name),
                screening_intro: w.active_playbook?.screening_intro.replace(
                  "{rep}",
                  w.user.name,
                ),
              }
            }
          />
          <div className="script-footer">
            <Layers3 size={13} />
            <span>
              Version{" "}
              {packet?.script.script_version.slice(0, 12) || "approved-v1"}
            </span>
          </div>
        </section>
      </div>
      {call && (
        <details className="card transcript-card">
          <summary>
            <ActivityIcon /> Live transcript{" "}
            <span className="pill subtle">{call.state}</span>
          </summary>
          <div className="transcript-scroll">
            {call.transcript?.length ? (
              call.transcript.map((s) => (
                <div key={s.segment_id} className={`utterance ${s.side}`}>
                  <span>
                    {s.side === "rep"
                      ? "YOU"
                      : s.side === "remote"
                        ? "PROSPECT SIDE"
                        : "UNKNOWN SIDE"}
                  </span>
                  <p>{s.text}</p>
                </div>
              ))
            ) : (
              <p className="fine">
                {active
                  ? "Listening for permitted transcript events…"
                  : "No transcript is available."}
              </p>
            )}
          </div>
          <small className="fine">
            Speaker labels describe the audio side, not a verified identity.
            Transcript text expires within five minutes.
          </small>
        </details>
      )}
      {import.meta.env.DEV && w.simulation && active && (
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
                void run(() =>
                  simulate("call.machine.greeting.ended:beep_detected"),
                )
              }
            >
              Mailbox beep
            </button>
          </div>
          <div className="input-row">
            <input
              aria-label="Simulation transcript"
              value={transcript}
              placeholder="Add a fictional prospect turn…"
              onChange={(e) => setTranscript(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void run(addTurn);
              }}
            />
            <button
              className="icon-button"
              aria-label="Add prospect turn"
              onClick={() => void run(addTurn)}
            >
              <Plus size={19} />
            </button>
          </div>
        </div>
      )}
      {call?.state === "VOICEMAIL_READY" &&
        (w.policy.voicemail_enabled && finalAttempt ? (
          <section className="card padded" aria-label="Voicemail">
            <h3>Mailbox ready · final permitted attempt</h3>
            <label>
              Approved voicemail
              <select
                value={vmAsset}
                onChange={(e) => setVMAsset(e.target.value)}
              >
                <option value="">Choose a reviewed message</option>
                {w.policy.approved_assets?.map((a) => (
                  <option key={a} value={a}>
                    {a}
                  </option>
                ))}
              </select>
            </label>
            <button
              className="secondary"
              disabled={!vmAsset}
              onClick={() =>
                void run(async () => {
                  setCall(
                    await api(`/v1/calls/${call.id}/voicemail`, "POST", {
                      asset_id: vmAsset,
                      detach: false,
                    }),
                  );
                })
              }
            >
              Start monitored voicemail
            </button>
            {w.policy.detached_topology_verified && (
              <details>
                <summary>Other voicemail options</summary>
                <button
                  className="secondary"
                  disabled={!vmAsset}
                  onClick={() =>
                    void run(async () => {
                      setCall(
                        await api(`/v1/calls/${call.id}/voicemail`, "POST", {
                          asset_id: vmAsset,
                          detach: true,
                        }),
                      );
                    })
                  }
                >
                  Start verified detached workflow
                </button>
              </details>
            )}
          </section>
        ) : (
          <p className="fine" role="status">
            Mailbox reached. A message is left only on the final permitted
            attempt; hang up when ready.
          </p>
        ))}
      <label className="check-row">
        <input
          type="checkbox"
          checked={autoNext}
          disabled={pauseAfter}
          onChange={(e) => setAutoNext(e.target.checked)}
        />
        Next scheduled call automatically after voicemail ends
      </label>
      {autoPending && (
        <p role="status">
          Next scheduled call in five seconds.{" "}
          <button
            onClick={() => {
              advancedVoicemail.current = call!.id;
              setAutoNext(false);
            }}
          >
            Cancel
          </button>
        </p>
      )}
      <CallControls
        visible={visible}
        pauseAfter={pauseAfter}
        onPauseAfterChange={setPauseAfter}
        onAdvance={advanceCall}
        call={call}
        reservation={reservation}
        controller={telephony.current}
        ready={ready}
        audioStatus={audioStatus}
        dialing={dialing}
        finding={finding}
        canOptOut={Boolean(selectedLead)}
        wrapUpPending={wrapUpPending}
        blocked={
          pendingOptouts ||
          !journalReady ||
          Boolean(inbound) ||
          incomingAudio ||
          // Dial only once the rep can see who they are calling and the script.
          Boolean(
            reservation && !call && packet?.lead.id !== reservation.lead.id,
          )
        }
        onStart={startCall}
        onNext={async () => {
          await getNext();
        }}
        onOptOut={optOut}
        onHangup={hangup}
      />
    </>
  );
}
function UsersIcon() {
  return <span className="tiny-circle">◎</span>;
}
function ActivityIcon() {
  return <span className="tiny-circle">≋</span>;
}
function Metric({
  title,
  value,
  detail,
  last,
}: {
  title: string;
  value: string;
  detail: string;
  last?: boolean;
}) {
  return (
    <div className={`metric ${last ? "last" : ""}`}>
      <span>{title}</span>
      <strong>
        {value}
        <small>{title.includes("REACH") ? " people" : ""}</small>
      </strong>
      <p>{detail}</p>
    </div>
  );
}
