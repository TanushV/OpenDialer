/**
 * The calling loop (R5.5, R6.2, R6.5): one state stream in, one phase out, and the actions keys and
 * buttons take. The screen reads only `phase` and the lead; no call state lives in components.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, localEdit } from "../api/client";
import { useWorkspace } from "../context";
import { OptOutJournal } from "../state/optouts";
import { nextLeadWithRetry } from "../state/queue";
import { afterAudioStatus, Telephony } from "../telephony/client";
import { captureSpeech } from "../telephony/speech";
import type { Call, Packet, Reservation } from "../types";
import { isActiveCall, isTerminal } from "../types";
import {
  screen,
  withIntent,
  type Blocker,
  type Intent,
  type Phase,
} from "./screen";

/** One key saves an outcome (R5.7). `wrong_party` stops dialing that number. */
export const OUTCOME_KEYS: [string, string][] = [
  ["meeting_booked", "Meeting booked"],
  ["callback_requested", "Callback requested"],
  ["not_interested", "Not interested"],
  ["gatekeeper", "Gatekeeper"],
  ["wrong_party", "Wrong number"],
  ["voicemail", "Voicemail"],
];
/** Audio stops a moment before the hangup event when the prospect hangs up. */
const GAP_GRACE_MS = 2500;
/** How often a paused or finished screen re-reads what blocks calling. */
const RECHECK_MS = 10_000;
/** An outcome key pressed while the call is ending waits this long, in steps, for the end to be confirmed. */
const ENDING_WAIT_MS = 250;
const ENDING_WAIT_STEPS = 40;
/** Blockers the loop clears itself: the rep's audio leg is renewed before each call. */
const SELF_CLEARING = new Set(["REP_AUDIO_NOT_READY"]);

const owesOutcome = (call: Call | null) =>
  !!call && isTerminal(call) && call.state !== "FINALIZED";

export function useCallLoop(visible: boolean) {
  const {
    snapshot: w,
    runtime,
    session,
    api,
    refresh,
    notice,
    run,
    subscribe,
  } = useWorkspace();
  const telephony = useRef(new Telephony());
  const audio = useRef<HTMLAudioElement>(null);
  // Pause takes effect here at once; the server's presence follows.
  const [held, setHeld] = useState(false);
  // The rep's own Start and Pause decide the screen at once; a workspace re-read that left before the
  // server saw them must not swap Pause for "Calling stops" and back (a click in between was lost).
  const [started, setStarted] = useState<boolean | null>(null);
  const calling = (started ?? w.user.presence.status === "ready") && !held;
  const callingRef = useRef(calling);
  callingRef.current = calling;
  const [connecting, setConnecting] = useState(false);
  const [call, setCall] = useState<Call | null>(null);
  const callRef = useRef(call);
  callRef.current = call;
  const [packet, setPacket] = useState<Packet | null>(null);
  const [blockers, setBlockers] = useState<Blocker[]>([]);
  const [finished, setFinished] = useState<{
    next_eligible_at: string | null;
    reason?: string;
  } | null>(null);
  const [muted, setMuted] = useState(false);
  const [editing, setEditing] = useState(false);
  // A saved callback whose time a person already handled names the task to review.
  const [callbackReview, setCallbackReview] =
    useState<Call["callback_review"]>(null);
  const [audioStatus, setAudioStatus] = useState("");
  const busy = useRef(false);
  const skipping = useRef<string | null>(null);
  // R9E.8: the rep's own Hang up, Skip, Stay and outcome key show at once, for the call they were pressed on,
  // and a re-read from before the server saw them cannot bring the used control back.
  const [intent, setIntent] = useState<
    (Intent & { call_id: string; saving?: string | null }) | null
  >(null);
  const intentRef = useRef(intent);
  intentRef.current = intent;
  const pausing = useRef(false);

  // Local opt-outs are journaled first, so a lost network never forgets one (calling blocks until synced).
  const journal = useRef(new OptOutJournal(localStorage, session.tenant_id));
  const [pendingOptouts, setPendingOptouts] = useState(
    journal.current.blocksCalling,
  );
  const syncOptouts = useCallback(async () => {
    await journal.current.synchronize((event) =>
      api("/v1/suppressions", "POST", {
        phone: event.phone,
        reason: event.reason,
        event_id: event.id,
      }),
    );
    setPendingOptouts(journal.current.blocksCalling);
  }, [api]);

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

  const local: Blocker[] = useMemo(() => {
    const found: Blocker[] = [];
    if (pendingOptouts)
      found.push({
        code: "PENDING_LOCAL_OPTOUTS",
        message:
          "An opt-out is still being saved. Calling resumes when it is stored.",
        fix: "dialer",
      });
    if (inbound || telephony.current.hasIncoming)
      found.push({
        code: "INBOUND_CALL",
        message: "Finish the incoming call first.",
        fix: "dialer",
      });
    return found;
  }, [pendingOptouts, inbound]);
  const shown = useMemo(
    () => [...local, ...blockers.filter((b) => !SELF_CLEARING.has(b.code))],
    [local, blockers],
  );
  const mine = call && intent?.call_id === call.id ? intent : null;
  const shownCall = call && withIntent(call, mine);
  const phase: Phase = screen({
    calling,
    connecting,
    blockers: shown,
    call: shownCall,
    finished,
    saving: mine?.saving ?? null,
  });

  const readBlockers = useCallback(async () => {
    const result = await api<{ blockers: Blocker[] }>("/v1/blockers");
    setBlockers(result.blockers);
    return result.blockers;
  }, [api]);

  // ---- The call record: one read path, refreshed by its own events.
  // A response for another call, or from a read older than one already started, never replaces the shown call.
  const readGeneration = useRef(0);
  const applyCall = useCallback((value: Call) => {
    setCall((current) =>
      current && current.id !== value.id
        ? current
        : !current || value.version >= current.version
          ? value
          : current,
    );
  }, []);
  const reloadCall = useCallback(
    async (id: string) => {
      const generation = ++readGeneration.current;
      const value = await api<Call>(`/v1/calls/${id}`);
      if (generation === readGeneration.current) applyCall(value);
    },
    [api, applyCall],
  );
  useEffect(() => {
    if (!call) return;
    const id = call.id;
    void reloadCall(id).catch(() => {});
    return subscribe((events) => {
      if (
        // Published knowledge can withdraw the helper's advice on this call.
        events.some((e) => e.entity_id === id || e.type === "knowledge.updated")
      )
        void reloadCall(id).catch(() => {});
    });
  }, [call?.id, reloadCall, subscribe]);
  // Background events never redraw the call: only blockers and the rep's presence are re-read (R5.6).
  useEffect(
    () =>
      subscribe((events) => {
        if (
          events.some((e) =>
            /^(user|number|settings|suppression|connection)\./.test(e.type),
          )
        )
          void readBlockers().catch(() => {});
      }),
    [subscribe, readBlockers],
  );
  useEffect(() => {
    void readBlockers().catch(() => {});
    if (phase.kind !== "paused" && phase.kind !== "finished") return;
    const timer = setInterval(
      () => void readBlockers().catch(() => {}),
      RECHECK_MS,
    );
    return () => clearInterval(timer);
  }, [phase.kind, readBlockers]);
  // A call that was live or owed an outcome when the screen opened comes back.
  useEffect(() => {
    const mine = w.calls.filter((c) => c.rep_id === w.user.id);
    const current =
      mine.find((c) => !isTerminal(c)) ?? mine.find((c) => owesOutcome(c));
    if (current && !callRef.current) {
      setCall(current);
      void run(async () =>
        setPacket(await api<Packet>(`/v1/leads/${current.lead_id}/packet`)),
      );
    }
  }, [w.calls]);

  // ---- Audio: the client registers once; each call gets a fresh leg.
  const connectAudio = useCallback(async () => {
    let agent = "agent-simulated";
    if (!w.simulation) {
      if (!telephony.current.registered) {
        setAudioStatus("Checking microphone and registering…");
        const result = await api<{ token: string }>(
          "/v1/telephony/token",
          "POST",
        );
        await telephony.current.connect(result.token, audio.current!);
      }
      const leg = await api<{ agent_call_control_id: string }>(
        "/v1/telephony/agent",
        "POST",
      );
      agent = leg.agent_call_control_id;
      telephony.current.expectAgentLeg(agent);
      let answered = false;
      for (let i = 0; i < 60 && !answered; i++) {
        const status = await api<{ state: string }>(
          `/v1/telephony/agent/${encodeURIComponent(agent)}`,
        );
        answered = status.state === "answered";
        if (!answered) await new Promise((r) => setTimeout(r, 500));
      }
      if (!answered)
        throw new Error(
          "Your audio did not connect. Check your headset, then start again.",
        );
    }
    await api("/v1/rep/presence", "POST", {
      status: "ready",
      audio_ready: true,
      telephony_ready: true,
      agent_call_control_id: agent,
      pending_optouts: 0,
    });
    setAudioStatus(w.simulation ? "Simulated audio ready" : "Audio ready");
  }, [api, w.simulation]);
  useEffect(() => {
    const controller = telephony.current;
    controller.onStatus = (status) => {
      setAudioStatus(status);
      if (connectingRef.current) return;
      const next = afterAudioStatus(status, {
        inCall: isActiveCall(callRef.current),
        ready: callingRef.current,
        pausing: pausing.current,
      });
      if (next === "pause") void pauseCalling().catch(() => {});
    };
    return () => {
      controller.onStatus = () => {};
      controller.disconnect();
    };
  }, []);
  const connectingRef = useRef(false);
  async function withConnecting<T>(work: () => Promise<T>) {
    connectingRef.current = true;
    setConnecting(true);
    try {
      return await work();
    } finally {
      connectingRef.current = false;
      setConnecting(false);
    }
  }
  useEffect(() => {
    if (returnProvider) telephony.current.expectReturnLeg(returnProvider);
  }, [returnProvider]);

  // ---- Direct speech capture on the rep's own leg (unchanged behavior).
  const speechActive = !!call && isActiveCall(call) && call.state !== "ENDING";
  useEffect(() => {
    if (
      !runtime ||
      !call ||
      call.transcription_source !== "direct" ||
      call.transcription_layout !== "direct_tracks" ||
      call.transcription_disabled ||
      !speechActive ||
      !call.agent_provider_id
    )
      return;
    let capture: ReturnType<typeof captureSpeech> | undefined;
    let signature = "";
    let failed = false;
    let grace: ReturnType<typeof setTimeout> | undefined;
    const began = Date.now();
    const report = () => {
      if (failed) return;
      failed = true;
      capture?.stop();
      clearInterval(timer);
      // A prospect hang-up ends the audio first; only a call still live after the grace has a gap.
      grace = setTimeout(() => {
        void api<Call>(`/v1/calls/${call.id}`)
          .then((latest) => {
            if (!isActiveCall(latest) || latest.state === "ENDING") return;
            notice(
              "Live transcription stopped for this call. Calling continues; this call is marked as missing speech.",
            );
            return api(`/v1/calls/${call.id}/audio-gap`, "POST", {});
          })
          .catch(() => {});
      }, GAP_GRACE_MS);
    };
    const check = () => {
      const streams = telephony.current.audioStreams(call.agent_provider_id!);
      const tracks = streams && [
        ...streams.rep.getAudioTracks(),
        ...streams.remote.getAudioTracks(),
      ];
      const next = tracks?.map((t) => t.id).join(":") || "";
      if (
        !streams ||
        tracks?.length !== 2 ||
        tracks.some((t) => t.readyState === "ended")
      ) {
        if (capture || Date.now() - began > 15000) report();
        return;
      }
      if (next === signature) return;
      signature = next;
      if (capture) capture.update(streams);
      else
        capture = captureSpeech(
          runtime.url,
          session.access_token,
          call.id,
          streams,
          report,
        );
    };
    const timer = setInterval(check, 500);
    check();
    return () => {
      clearInterval(timer);
      if (grace) clearTimeout(grace);
      capture?.stop();
    };
  }, [
    call?.id,
    call?.transcription_source,
    call?.transcription_layout,
    call?.transcription_disabled,
    speechActive,
    call?.agent_provider_id,
    runtime?.url,
    session.access_token,
  ]);

  // ---- Actions.
  async function nextCall() {
    if (busy.current || !callingRef.current) return;
    if (owesOutcome(callRef.current) || isActiveCall(callRef.current)) return;
    busy.current = true;
    try {
      // The server's blockers, not this render's snapshot, say whether the audio leg needs renewing.
      const found = await readBlockers();
      if (found.some((b) => !SELF_CLEARING.has(b.code)) || local.length) return;
      if (found.some((b) => b.code === "REP_AUDIO_NOT_READY"))
        await withConnecting(connectAudio);
      let next: Reservation;
      try {
        next = await nextLeadWithRetry(
          () =>
            api<Reservation>("/v1/queue/next", "POST", {
              campaign_id: w.campaign.id,
              list_names: dialLists(),
            }),
          { onWaiting: () => {} },
        );
      } catch (error) {
        if (error instanceof ApiError && error.code === "NO_ELIGIBLE_LEADS") {
          // The queue says why nobody can be called; the Dialer shows that, not a generic line.
          setFinished({ next_eligible_at: null, reason: error.message });
          return;
        }
        throw error;
      }
      setFinished(null);
      setEditing(false);
      setPacket(await api<Packet>(`/v1/leads/${next.lead.id}/packet`));
      setCall(
        await api<Call>("/v1/calls/start", "POST", {
          reservation_id: next.reservation_id,
          idempotency_key: next.reservation_id,
        }),
      );
      setMuted(false);
    } finally {
      busy.current = false;
    }
  }
  function dialLists(): string[] | null {
    try {
      const saved = JSON.parse(localStorage.getItem(listsKey) || "null");
      return Array.isArray(saved) ? saved : null;
    } catch {
      return null;
    }
  }
  const listsKey = `dial-lists:${session.tenant_id}:${session.seller_id}:${session.user_id}`;

  async function startCalling() {
    if (local.length) throw new Error(local[0].message);
    await syncOptouts();
    if (journal.current.blocksCalling)
      throw new Error("Sync the pending opt-out before calling.");
    pausing.current = false;
    setHeld(false);
    setFinished(null);
    await withConnecting(connectAudio);
    setStarted(true);
    callingRef.current = true;
    await refresh();
    callingRef.current = true;
    await nextOrPause();
  }
  async function pauseCalling() {
    pausing.current = true;
    callingRef.current = false;
    setHeld(true);
    setStarted(false);
    await api("/v1/rep/presence", "POST", {
      status: "paused",
      audio_ready: w.user.presence.audio_ready ?? false,
      telephony_ready: false,
    });
    if (!isActiveCall(callRef.current)) {
      await api("/v1/queue/release", "POST", {});
      await telephony.current.releaseIdleAgent();
    }
    await refresh();
  }
  async function hangup() {
    const current = callRef.current;
    if (!current || !isActiveCall(current)) return;
    const identity = telephony.current.activeIdentity;
    setIntent((value) => ({
      ...(value?.call_id === current.id ? value : {}),
      call_id: current.id,
      hangup: true,
    }));
    const still = () => callRef.current?.id === current.id;
    let result: Call;
    try {
      result = await api<Call>(`/v1/calls/${current.id}/hangup`, "POST");
    } catch (error) {
      // The server did not take the hang-up: show the call as it is, with Hang up, again.
      setIntent((value) =>
        value?.call_id === current.id ? { ...value, hangup: false } : value,
      );
      if (identity && still())
        await telephony.current.emergencyHangup(identity);
      throw error;
    }
    // A late answer for a call already replaced by the next one must not touch the new call's audio.
    if (!still()) return;
    ++readGeneration.current;
    applyCall(result);
    if (identity) await telephony.current.emergencyHangup(identity);
  }
  /** Space while dialing: end this attempt and save No answer (section 6, Skipped). */
  async function skip() {
    const current = callRef.current;
    if (!current) return;
    if (
      current.state === "VOICEMAIL_GREETING" ||
      current.state === "VOICEMAIL_READY"
    )
      return hangup(); // The carrier settles a skipped mailbox as voicemail.
    skipping.current = current.id;
    await hangup();
  }
  async function stay() {
    const current = callRef.current;
    if (!current) return;
    setIntent((value) => ({
      ...(value?.call_id === current.id ? value : {}),
      call_id: current.id,
      stay: true,
    }));
    try {
      await api(`/v1/calls/${current.id}/stay`, "POST");
    } catch (error) {
      setIntent((value) =>
        value?.call_id === current.id ? { ...value, stay: false } : value,
      );
      throw error;
    }
    await reloadCall(current.id);
  }
  function mute() {
    telephony.current.mute();
    setMuted((value) => !value);
  }
  async function save(code: string) {
    const pressed = callRef.current;
    if (!pressed) return;
    // One save at a time for this call; its key shows at once and the others wait for the answer.
    if (intentRef.current?.call_id === pressed.id && intentRef.current.saving)
      return;
    // Set at once, not on the next render: a second key in the same moment sees this save.
    intentRef.current = {
      ...(intentRef.current?.call_id === pressed.id ? intentRef.current : {}),
      call_id: pressed.id,
      saving: code,
    };
    setIntent(intentRef.current);
    let result: Call;
    let current = pressed;
    try {
      // A key pressed while the call is still ending is kept, and sent once the end is confirmed.
      for (let i = 0; !isTerminal(current) && i < ENDING_WAIT_STEPS; i++) {
        await new Promise((r) => setTimeout(r, ENDING_WAIT_MS));
        current = await api<Call>(`/v1/calls/${pressed.id}`);
        applyCall(current);
      }
      if (!isTerminal(current))
        throw new Error(
          "The call is still ending. Press the outcome again in a moment.",
        );
      result = await localEdit<Call>(
        api,
        `/v1/calls/${current.id}/disposition`,
        "POST",
        {
          outcome: code,
          contact_role: "unknown",
          callback_note: "",
          callback_at: null,
          expected_version: current.version,
          expected_edit_version: current.edit_version ?? 0,
          request_id: crypto.randomUUID(),
        },
      );
      // The saved call is shown before the keys come back, so no render offers a second save.
      applyCall(result);
    } finally {
      setIntent((value) =>
        value?.call_id === pressed.id ? { ...value, saving: null } : value,
      );
    }
    setEditing(false);
    setCallbackReview(result.callback_review ?? null);
    if (code === "meeting_booked")
      notice(
        "Meeting recorded as rep-reported. Outreach to this lead is paused.",
      );
  }
  function saved(result: Call) {
    applyCall(result);
    setEditing(false);
    setCallbackReview(result.callback_review ?? null);
  }
  async function optOut() {
    const lead = packet?.lead;
    if (!lead) return;
    journal.current.add(lead.phone, "opt_out");
    setPendingOptouts(true);
    const [ended] = await Promise.allSettled([hangup()]);
    await syncOptouts();
    notice(
      journal.current.blocksCalling
        ? "Opt-out saved on this Mac. Calling waits until it is stored."
        : "Opt-out saved. This number will not be called again.",
    );
    if (ended.status === "rejected") throw ended.reason;
  }

  // A skipped ringing call is saved as No answer once it has ended.
  useEffect(() => {
    if (call && skipping.current === call.id && owesOutcome(call)) {
      skipping.current = null;
      void run(() => save("no_answer"));
    }
  }, [call?.id, call?.state]);
  /** A failed dial pauses the loop with its reason, so the screen never sits on "Dialing". */
  async function nextOrPause() {
    try {
      await nextCall();
    } catch (error) {
      await pauseCalling().catch(() => {});
      throw error;
    }
  }
  const advance = () => run(nextOrPause);
  // The loop: dial when idle, and after a saved or skipped call's countdown (R6.2).
  useEffect(() => {
    if (!visible) return;
    if (phase.kind === "dialing" && !call) {
      void advance();
      return;
    }
    if ((phase.kind === "saved" || phase.kind === "skipped") && !editing) {
      const timer = setTimeout(() => void advance(), phase.countdown_ms);
      return () => clearTimeout(timer);
    }
    if (phase.kind === "finished") {
      const timer = setTimeout(() => void advance(), 60_000);
      return () => clearTimeout(timer);
    }
  }, [phase.kind, call?.id, call?.state, editing, visible]);
  // Pending opt-outs retry when the network returns.
  useEffect(() => {
    void syncOptouts().catch(() => {});
    const handler = () => void syncOptouts().catch(() => {});
    window.addEventListener("online", handler);
    return () => window.removeEventListener("online", handler);
  }, [syncOptouts]);

  const act = {
    start: () => run(startCalling),
    pause: () => run(pauseCalling),
    skip: () => run(skip),
    stay: () => run(stay),
    hangup: () => run(hangup),
    mute,
    dial_now: advance,
    change_outcome: () => setEditing(true),
    save: (code: string) => run(() => save(code)),
    optOut: () => run(optOut),
    reload: () =>
      callRef.current ? reloadCall(callRef.current.id) : Promise.resolve(),
    saved,
    edit: setEditing,
  };
  /** The rep paused while a call is still on: it finishes, then calling stops. */
  const stopping = !calling && phase.kind !== "paused";
  return {
    phase,
    call,
    packet,
    setPacket,
    muted,
    editing,
    audio,
    telephony,
    audioStatus,
    inbound,
    returnProvider,
    listsKey,
    stopping,
    callbackReview,
    dismissReview: () => setCallbackReview(null),
    act,
  };
}
