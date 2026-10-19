import { useEffect, useState } from "react";
import {
  LoaderCircle,
  Mic,
  MicOff,
  Phone,
  PhoneOff,
  ShieldCheck,
} from "lucide-react";
import { useWorkspace } from "../context";
import { Keypad } from "./Keypad";
import { terminal, type Call, type Reservation } from "../types";
import type { Telephony } from "../telephony/client";

type Props = {
  call: Call | null;
  reservation: Reservation | null;
  controller: Telephony;
  ready: boolean;
  audioStatus: string;
  blocked: boolean;
  dialing: boolean;
  canOptOut: boolean;
  onNext: () => Promise<void>;
  onStart: () => Promise<void>;
  onOptOut: () => Promise<void>;
  onHangup: (pauseAfter: boolean) => Promise<void>;
};
export function CallControls({
  call,
  reservation,
  controller,
  ready,
  audioStatus,
  blocked,
  dialing,
  canOptOut,
  onNext,
  onStart,
  onOptOut,
  onHangup,
}: Props) {
  const { run } = useWorkspace();
  const [mute, setMute] = useState(false);
  const [keypad, setKeypad] = useState(false);
  const [pauseAfter, setPauseAfter] = useState(false);
  const active = !call?.detached && !terminal(call);
  useEffect(() => {
    setMute(false);
    setKeypad(false);
  }, [call?.id]);
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (!active) return;
      if (event.key === "F2") {
        event.preventDefault();
        controller.mute();
        setMute((value) => !value);
      }
      if (event.key === "F4") {
        event.preventDefault();
        void run(() => onHangup(pauseAfter));
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [active, controller, onHangup, pauseAfter, run]);
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
              {reservation
                ? `${reservation.number.phone} · Your calling identity`
                : audioStatus || "One active prospect at a time"}
            </small>
          </div>
        </div>
        <div className="dock-controls">
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
              className="primary dial-button"
              disabled={!ready || blocked}
              onClick={() => void run(onNext)}
            >
              <Phone size={17} />
              Get next lead
            </button>
          )}
        </div>
        <label className="pause-after">
          <input
            type="checkbox"
            checked={pauseAfter}
            onChange={(e) => setPauseAfter(e.target.checked)}
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
