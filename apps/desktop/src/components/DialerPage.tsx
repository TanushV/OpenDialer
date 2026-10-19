import { useSpeechExpiry } from "../state/useSpeechExpiry";
import { LiveCoach } from "./LiveCoach";
import { CallingHealth } from "./WorkTime";
import { ProspectBrief } from "./ProspectBrief";
import {
  ArrowRight,
  Check,
  Clock3,
  FileText,
  Globe2,
  Headphones,
  Layers3,
  Pause,
  Phone,
  Plus,
  ShieldCheck,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { native, openSource, readCache, writeCache } from "../api/client";
import { label, useWorkspace } from "../context";
import { MemoryStorage, OptOutJournal, type OptOut } from "../state/optouts";
import { Telephony } from "../telephony/client";
import { InboundCall } from "./InboundCall";
import { CallControls } from "./CallControls";
import { WrapUp } from "./WrapUp";
import type { Call, Packet, Reservation } from "../types";
import { terminal } from "../types";

export function DialerPage() {
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
  const [reservation, setReservation] = useState<Reservation | null>(null);
  const [packet, setPacket] = useState<Packet | null>(null);
  const [call, setCall] = useState<Call | null>(null);
  useSpeechExpiry(call, setCall);
  const [dialing, setDialing] = useState(false);
  const [transcript, setTranscript] = useState("");
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const [audioStatus, setAudioStatus] = useState("");
  const [showDevices, setShowDevices] = useState(false);
  const [vmAsset, setVMAsset] = useState("");
  const eventReceived = useRef<number | null>(null);
  const responseReceived = useRef<number | null>(null);
  const audio = useRef<HTMLAudioElement>(null);
  const telephony = useRef(new Telephony());
  const journalStore = useRef(native() ? new MemoryStorage() : localStorage);
  const journal = useRef(
    new OptOutJournal(journalStore.current, session.tenant_id),
  );
  const [journalReady, setJournalReady] = useState(!native());
  const [pendingOptouts, setPendingOptouts] = useState(
    journal.current.blocksCalling,
  );
  const ready = w.user.presence.status === "ready";
  const active = !call?.detached && !terminal(call);
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

  useEffect(() => {
    const controller = telephony.current;
    return () => {
      controller.onStatus = () => {};
      controller.disconnect();
    };
  }, []);

  async function reloadCall(id: string) {
    setCall(await api<Call>(`/v1/calls/${id}`));
  }
  useEffect(() => {
    const owned = w.calls.filter((c) => c.rep_id === w.user.id);
    const current =
      owned.find((c) => !terminal(c)) ||
      owned.find((c) => c.state !== "FINALIZED" && terminal(c));
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
        dirty = true;
        return;
      }
      pending = true;
      void api<Call>("/v1/calls/" + call.id)
        .then((value) => {
          if (!disposed) {
            responseReceived.current = performance.now();
            setCall(value);
          }
        })
        .catch(() => {
          if (!disposed)
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
      unsubscribe();
    };
  }, [call?.id, api, subscribe]);
  useEffect(() => {
    telephony.current.onStatus = (status) => {
      setAudioStatus(status);
      if (
        [
          "agent-disconnected",
          "reconnecting",
          "disconnected",
          "return-ended",
        ].includes(status)
      )
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

  async function persistJournal() {
    await writeCache(
      `optouts-${session.tenant_id}`,
      journal.current.pending,
      w.simulation,
    );
  }
  async function syncOptouts() {
    await journal.current.synchronize((event) =>
      api("/v1/suppressions", "POST", {
        phone: event.phone,
        reason: event.reason,
        event_id: event.id,
      }),
    );
    await persistJournal();
    setPendingOptouts(journal.current.blocksCalling);
  }
  useEffect(() => {
    void run(async () => {
      if (native()) {
        const saved = await readCache(
          `optouts-${session.tenant_id}`,
          w.simulation,
        );
        if (saved) {
          const items = JSON.parse(saved) as OptOut[];
          if (!Array.isArray(items) || items.some((e) => !e.phone || !e.id))
            throw new Error(
              "Protected opt-out journal needs recovery; calling remains paused.",
            );
          journalStore.current.setItem(
            `opendialer.optouts.${session.tenant_id}`,
            JSON.stringify(items),
          );
        }
      }
      setPendingOptouts(journal.current.blocksCalling);
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
    if (inbound || telephony.current.hasIncoming)
      throw new Error(
        "Complete the incoming call before becoming ready for outbound calls.",
      );
    if (!journalReady)
      throw new Error("Wait for the protected opt-out journal to open.");
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
  }
  async function pause() {
    await api("/v1/rep/presence", "POST", {
      status: "paused",
      audio_ready: w.user.presence.audio_ready ?? false,
      telephony_ready: false,
    });
    if (!active) {
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
    const next = await api<Reservation>("/v1/queue/next", "POST", {
      campaign_id: w.campaign.id,
    });
    setReservation(next);
    setPacket(await api(`/v1/leads/${next.lead.id}/packet`));
    setCall(null);
  }
  async function startCall() {
    if (inbound || telephony.current.hasIncoming)
      throw new Error(
        "Complete the incoming call before starting an outbound call.",
      );
    if (!reservation || dialing || pendingOptouts) return;
    setDialing(true);
    try {
      setCall(
        await api("/v1/calls/start", "POST", {
          reservation_id: reservation.reservation_id,
          idempotency_key: reservation.reservation_id,
        }),
      );
      await refresh();
    } finally {
      setDialing(false);
    }
  }
  async function hangup(pauseAfter = false) {
    if (!call) return;
    try {
      setCall(await api(`/v1/calls/${call.id}/hangup`, "POST"));
      await telephony.current.emergencyHangup();
    } catch (e) {
      await telephony.current.emergencyHangup();
      throw e;
    }
    if (pauseAfter) await pause();
    await refresh();
  }
  async function optOut() {
    if (!selectedLead) return;
    journal.current.add(selectedLead.phone, "opt_out");
    setPendingOptouts(true);
    await persistJournal();
    await syncOptouts();
    if (call && active) await hangup();
    await refresh();
    notice(
      journal.current.blocksCalling
        ? "Opt-out saved locally. Outbound calling is blocked until synchronization."
        : "Opt-out saved. This endpoint is suppressed.",
    );
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
      <LiveCoach
        deliveryTiming={{
          receivedAt: responseReceived.current,
          eventAt: eventReceived.current,
        }}
        call={call}
        onChange={() => (call ? reloadCall(call.id) : Promise.resolve())}
      />
      <div className="dialer-status">
        <span className="fine">Conversation updates: {label(liveStatus)}</span>
        <button
          className="text-button"
          onClick={() =>
            void run(async () => {
              setShowDevices(!showDevices);
              if (!w.simulation) setDevices(await telephony.current.devices());
            })
          }
        >
          Audio devices
        </button>
      </div>
      {showDevices && (
        <section className="card padded">
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
        </section>
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
          disabled={pendingOptouts || !journalReady}
          onClick={() => void run(ready ? pause : goReady)}
        >
          {ready ? <Pause size={16} /> : <Headphones size={17} />}{" "}
          {ready ? "Pause dialing" : "Go ready"}
        </button>
      </div>
      {pendingOptouts && (
        <div className="alert">
          <ShieldCheck size={18} /> A local opt-out is awaiting synchronization.
          New calls are blocked.
          <button onClick={() => void run(syncOptouts)}>Retry sync</button>
        </div>
      )}
      {!packet && (
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
      )}
      {!packet && <CallingHealth />}
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
                  <Globe2 size={14} />
                  {selectedLead.timezone?.replaceAll("_", " ") ||
                    "Timezone needs review"}
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
                disabled={!ready || active || pendingOptouts || !journalReady}
                onClick={() => void run(getNext)}
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
          <div className="script-opening">
            <span className="section-number">01</span>
            <div>
              <span className="eyebrow">OPEN WITH A LITTLE CONTEXT</span>
              <blockquote>
                {packet?.script.opener ||
                  w.active_playbook?.opener.replace("{rep}", w.user.name) ||
                  "Start with your name, business, and a truthful reason for calling."}
              </blockquote>
            </div>
          </div>
          <div className="section-rule" />
          <div className="script-discovery">
            <span className="section-number">02</span>
            <div>
              <span className="eyebrow">MAKE SPACE FOR THEIR STORY</span>
              {(
                packet?.script.discovery_questions || [
                  "What would make this conversation useful for you?",
                  "How are you handling this today?",
                ]
              ).map((question, i) => (
                <p className="question" key={i}>
                  <span>{i + 1}</span>
                  {question}
                </p>
              ))}
            </div>
          </div>
          <div className="screening-intro">
            <span>
              <ShieldCheck size={14} /> iPhone screening
            </span>
            <p>
              {packet?.script.screening_intro ||
                w.active_playbook?.screening_intro.replace(
                  "{rep}",
                  w.user.name,
                )}
            </p>
          </div>
          <div className="screening-intro">
            <span>Gatekeeper</span>
            <p>
              {packet?.script.gatekeeper ||
                "Who is the right person to speak with about this?"}
            </p>
          </div>
          {packet?.script.proof && (
            <div className="screening-intro">
              <span>Supported proof</span>
              <p>{packet.script.proof}</p>
            </div>
          )}
          {packet?.script.closing_question && (
            <div className="screening-intro">
              <span>Next step</span>
              <p>{packet.script.closing_question}</p>
            </div>
          )}
          <div className="script-footer">
            {packet?.script.final_voicemail_asset && (
              <p className="fine">
                A reviewed voicemail message is referenced for the final
                permitted attempt. Use the call controls only when playback is
                authorized.
              </p>
            )}
            <Layers3 size={13} />
            <span>
              Version{" "}
              {packet?.script.script_version.slice(0, 12) || "approved-v1"}
            </span>
            <button
              className="text-button"
              onClick={() =>
                notice(
                  "Flag this script in Intelligence for review before publishing a new version.",
                )
              }
            >
              Flag an inaccuracy
            </button>
          </div>
        </section>
      </div>
      <div className="live-grid">
        <section className="card transcript-card">
          <div className="card-eyebrow">
            <span>
              <ActivityIcon /> LIVE CONVERSATION
            </span>
            <span className="pill subtle">
              {call ? call.state : "Standing by"}
            </span>
          </div>
          <div className="transcript-scroll">
            {call?.transcript?.length ? (
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
              <div className="transcript-empty">
                <span className="audio-lines">
                  <i />
                  <i />
                  <i />
                  <i />
                  <i />
                </span>
                <p>
                  {active
                    ? "Listening for permitted transcript events…"
                    : "The conversation lives here."}
                </p>
                <small>
                  Speaker labels describe the audio side, not a verified
                  identity.
                </small>
              </div>
            )}
          </div>
          {w.simulation && active && (
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
        </section>
      </div>
      {call?.state === "VOICEMAIL_READY" && w.policy.voicemail_enabled && (
        <section className="card padded">
          <h3>Mailbox ready</h3>
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
          )}
        </section>
      )}
      {call && terminal(call) && call.state !== "FINALIZED" && (
        <WrapUp
          key={call.id}
          call={call}
          onSaved={async (result) => {
            setCall(result);
            await refresh();
            setPacket(await api<Packet>(`/v1/leads/${call.lead_id}/packet`));
          }}
        />
      )}
      <CallControls
        call={call}
        reservation={reservation}
        controller={telephony.current}
        ready={ready}
        audioStatus={audioStatus}
        dialing={dialing}
        canOptOut={Boolean(selectedLead)}
        blocked={
          pendingOptouts || !journalReady || Boolean(inbound) || incomingAudio
        }
        onStart={startCall}
        onNext={getNext}
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
