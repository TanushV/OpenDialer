import { useEffect, useRef, useState, type RefObject } from "react";
import { useWorkspace } from "../context";
import {
  callSoundOutput,
  callSoundsPlaying,
  createRinger,
  playEndTone,
  prepareCallSounds,
} from "../telephony/ringer";
import type { Telephony } from "../telephony/client";
import { onFirstRemoteSound } from "../telephony/playback";
import { sessionAudio } from "../telephony/speech";
import { isActiveCall, type Call } from "../types";

type PlaybackEvent =
  | "ringback_playing"
  | "ringback_unavailable"
  | "remote_playing"
  | "remote_unavailable";

/** Playback is separate from WebRTC registration and transcription. Watch the actual media elements. */
export function useCallPlayback(
  call: Call | null,
  ringing: boolean,
  voice: RefObject<HTMLAudioElement | null>,
  cues: RefObject<HTMLAudioElement | null>,
  phone: Telephony,
) {
  const {
    api,
    snapshot: { simulation },
  } = useWorkspace();
  const [warning, setWarning] = useState("");
  const id = call?.id;
  const agent = call?.agent_provider_id;
  const active = isActiveCall(call) && call?.state !== "ENDING";
  const connected =
    active && !ringing && Boolean(call?.answered_at || call?.bridged_at);

  useEffect(() => {
    setWarning("");
    const cue = cues.current;
    const element = voice.current;
    if (!id || !active || !cue || !element) return;
    let last: PlaybackEvent | undefined;
    function report(event: PlaybackEvent, problem: string) {
      if (event === last) return;
      last = event;
      setWarning(problem);
      void api(`/v1/calls/${id}/playback`, "POST", { event }).catch((error) =>
        console.warn("Could not log call playback:", error),
      );
    }
    if (ringing) {
      let output: MediaStreamAudioDestinationNode;
      let tone: ReturnType<typeof createRinger>;
      try {
        output = callSoundOutput(cue);
        tone = createRinger(sessionAudio, undefined, output);
        tone.start();
      } catch (error) {
        console.warn("Could not start outbound ringback:", error);
        report(
          "ringback_unavailable",
          "Ringback could not start. The call is still dialing.",
        );
        return;
      }
      let remote = false;
      const observe = () => {
        if (remote) return;
        if (callSoundsPlaying(cue) && tone.ringing) {
          report("ringback_playing", "");
        } else {
          report(
            "ringback_unavailable",
            "Ringback playback stopped. The call is still dialing.",
          );
        }
      };
      // Event delivery can lag the bridge. Stop the local tone as soon as the rep's media arrives.
      const stopWatch = onFirstRemoteSound(sessionAudio(), element, () => {
        remote = true;
        tone.stop();
        report("remote_playing", "");
      });
      observe();
      output.context.addEventListener("statechange", observe);
      cue.addEventListener("playing", observe);
      cue.addEventListener("pause", observe);
      cue.addEventListener("error", observe);
      return () => {
        stopWatch();
        tone.stop();
        output.context.removeEventListener("statechange", observe);
        cue.removeEventListener("playing", observe);
        cue.removeEventListener("pause", observe);
        cue.removeEventListener("error", observe);
      };
    }
    if (!connected || simulation || !agent) return;
    let grace: ReturnType<typeof setTimeout> | undefined;
    const observe = () => {
      if (phone.hasAgentPlayback(agent)) {
        clearTimeout(grace);
        grace = undefined;
        report("remote_playing", "");
      } else {
        grace ??= setTimeout(() => {
          grace = undefined;
          report(
            "remote_unavailable",
            "The call connected, but the speaker is not playing the call audio.",
          );
        }, 1500);
      }
    };
    // Most hang-ups close the WebRTC track shortly before the carrier's event arrives.
    const check = setInterval(observe, 500);
    observe();
    return () => {
      clearInterval(check);
      clearTimeout(grace);
    };
  }, [
    id,
    active,
    connected,
    ringing,
    agent,
    simulation,
    phone,
    voice,
    cues,
    api,
  ]);

  const ended = call?.state === "ENDED" || call?.state === "FINALIZED";
  useEndCue(id, ended, cues);

  return {
    warning,
    retry: async () => {
      // Begin playback inside the click. A lost headset can also be bypassed using the OS default.
      const sounds = prepareCallSounds(cues.current!);
      const callAudio =
        connected && !simulation && agent ? phone.playAgent(agent) : null;
      await Promise.all([
        sounds,
        callAudio,
        phone.output("", cues.current!, voice.current),
      ]);
    },
  };
}

/** One cue per observed call, including a carrier hang-up that went straight to FINALIZED. */
function useEndCue(
  id: string | undefined,
  ended: boolean,
  cues: RefObject<HTMLAudioElement | null>,
) {
  const live = useRef<string | null>(null);
  useEffect(() => {
    if (!id) return;
    if (!ended) live.current = id;
    else if (live.current === id) {
      live.current = null;
      try {
        if (callSoundsPlaying(cues.current))
          playEndTone(callSoundOutput(cues.current!));
      } catch (error) {
        console.warn("Could not play the call-end tone:", error);
      }
    }
  }, [id, ended, cues]);
}
