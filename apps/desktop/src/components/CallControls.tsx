import { useEffect, useRef, useState } from "react";
import {
  LoaderCircle,
  Mic,
  MicOff,
  Phone,
  PhoneOff,
  ShieldCheck,
} from "lucide-react";
import { useWorkspace } from "../context";
import { canAdvanceWithSpace } from "../state/call-shortcuts";
import { Keypad } from "./Keypad";
import { isActiveCall, type Call, type Reservation } from "../types";
import type { Telephony } from "../telephony/client";

type Props = {
  visible: boolean;
  pauseAfter: boolean;
  onPauseAfterChange: (value: boolean) => void;
  onAdvance: () => Promise<void>;
  call: Call | null;
  reservation: Reservation | null;
  controller: Telephony;
  ready: boolean;
  audioStatus: string;
  blocked: boolean;
  dialing: boolean;
  /** The authority is still scanning past leads that cannot be called now. */
  finding?: boolean;
  canOptOut: boolean;
  /** The ended call's outcome is unsaved; the next lead would discard it. */
  wrapUpPending: boolean;
  onNext: () => Promise<void>;
  onStart: () => Promise<void>;
  onOptOut: () => Promise<void>;
  onHangup: (pauseAfter: boolean) => Promise<void>;
};
export function CallControls({
  visible,
  pauseAfter,
  onPauseAfterChange,
  onAdvance,
  call,
  reservation,
  controller,
  ready,
  audioStatus,
  blocked,
  dialing,
  finding = false,
  canOptOut,
  wrapUpPending,
  onNext,
  onStart,
  onOptOut,
  onHangup,
}: Props) {
  const { run } = useWorkspace();
  const [mute, setMute] = useState(false);
  const [keypad, setKeypad] = useState(false);
  const active = isActiveCall(call);
  useEffect(() => {
    setMute(false);
    setKeypad(false);
  }, [call?.id]);
  const advancing = useRef(false);
  async function advance() {
    if (advancing.current) return;
    advancing.current = true;
    try {
      await onAdvance();
    } finally {
      advancing.current = false;
    }
  }
  const shortcut = useRef({
    visible,
    ready,
    blocked,
    active,
    busy: dialing || finding,
    wrapUpPending,
    advance,
  });
  shortcut.current = {
    visible,
    ready,
    blocked,
    active,
    busy: dialing || finding,
    wrapUpPending,
    advance,
  };
  // F2/F4 read the latest call state; the listener is registered once.
  const keys = useRef({ active, controller, onHangup, pauseAfter });
  keys.current = { active, controller, onHangup, pauseAfter };
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      const target = event.target;
      const interactive =
        target instanceof Element &&
        !!target.closest(
          "input, textarea, select, button, a, [contenteditable], [role=dialog]",
        );
      if (canAdvanceWithSpace(event, shortcut.current, interactive)) {
        event.preventDefault();
        void run(shortcut.current.advance);
        return;
      }
      const current = keys.current;
      if (!current.active) return;
      if (event.key === "F2") {
        event.preventDefault();
        current.controller.mute();
        setMute((value) => !value);
      }
      if (event.key === "F4") {
        event.preventDefault();
        void run(() => current.onHangup(current.pauseAfter));
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [run]);
  return (
    <>
      <div className="call-dock">
        <div className="dock-status">
          <span className={`status-dot ${ready ? "" : "paused"}`} />
          <div>
            <strong>
              {active
                ? "In conversation"
                : ready
                  ? "Ready when you are"
                  : "Session paused"}
            </strong>
            <small>
              {wrapUpPending
                ? "Save this call's outcome before the next lead."
                : reservation
                  ? `${reservation.number.phone} · Your calling identity`
                  : audioStatus || "One active prospect at a time"}
            </small>
          </div>
        </div>
        <div className="dock-controls">
          {!active && !(reservation && !call) && (
            <button
              className="primary dial-button"
              title="Next scheduled call · Space"
              disabled={
                !ready || blocked || wrapUpPending || finding || dialing
              }
              onClick={() => void run(advance)}
            >
              <Phone size={17} /> Next call <kbd>Space</kbd>
            </button>
          )}

          <button
            className="dock-tool"
            disabled={!active}
            title="Mute · F2"
            onClick={() => {
              controller.mute();
              setMute(!mute);
            }}
          >
            {mute ? <MicOff size={18} /> : <Mic size={18} />}
            <span>{mute ? "Unmute" : "Mute"}</span>
          </button>
          <button
            className="dock-tool"
            disabled={!active}
            onClick={() => setKeypad(!keypad)}
          >
            <span className="keypad-icon">⠿</span>
            <span>Keypad</span>
          </button>
          <button
            className="dock-tool optout"
            disabled={!canOptOut}
            onClick={() => void run(onOptOut)}
          >
            <ShieldCheck size={18} />
            <span>Opt out</span>
          </button>
          {active ? (
            <button
              className="danger-button"
              title="Hang up · F4"
              onClick={() => void run(() => onHangup(pauseAfter))}
            >
              <PhoneOff size={17} />
              Hang up
            </button>
          ) : reservation && !call ? (
            <button
              className="primary dial-button"
              disabled={blocked || dialing}
              onClick={() => void run(onStart)}
            >
              {dialing ? (
                <LoaderCircle size={17} className="spin" />
              ) : (
                <Phone size={17} />
              )}
              Start call
            </button>
          ) : (
            <button
              className="secondary"
              disabled={!ready || blocked || wrapUpPending || finding}
              onClick={() => void run(onNext)}
            >
              {finding ? (
                <LoaderCircle size={17} className="spin" />
              ) : (
                <Phone size={17} />
              )}
              {finding ? "Finding an eligible lead…" : "Get next lead"}
            </button>
          )}
        </div>
        <label className="pause-after">
          <input
            type="checkbox"
            checked={pauseAfter}
            onChange={(e) => onPauseAfterChange(e.target.checked)}
          />{" "}
          Pause after call
        </label>
      </div>
      {keypad && call && active && (
        <Keypad key={call.id} callId={call.id} close={() => setKeypad(false)} />
      )}
    </>
  );
}
