import { sessionAudio } from "./speech";
import { playOutput, waitForPlayback } from "./playback";

const outputs = new WeakMap<
  HTMLAudioElement,
  MediaStreamAudioDestinationNode
>();

/** Ringback and the end tone use a media element so they can use the same output as the call. */
export function callSoundOutput(element: HTMLAudioElement) {
  const audio = sessionAudio();
  let output = outputs.get(element);
  if (!output || output.context !== audio) {
    output = audio.createMediaStreamDestination();
    outputs.set(element, output);
  }
  if (element.srcObject !== output.stream) element.srcObject = output.stream;
  return output;
}

/** Invoke from Start or Dial now, while WebKit still recognizes the rep's click or key. */
export async function prepareCallSounds(element: HTMLAudioElement) {
  const output = callSoundOutput(element);
  const audio = sessionAudio();
  await Promise.all([
    waitForPlayback(audio.resume(), "The call sound engine did not start."),
    playOutput(element, "Call sounds"),
  ]);
  if (!callSoundsPlaying(element))
    throw new Error(
      "Call sound playback is paused. Check your speaker or headset, then try again.",
    );
  return output;
}

export function callSoundsPlaying(element: HTMLAudioElement | null) {
  return (
    !!element &&
    outputs.get(element)?.context.state === "running" &&
    !element.paused &&
    !element.muted &&
    element.volume > 0 &&
    !element.error
  );
}

export function playEndTone(output: AudioNode) {
  const audio = output.context;
  const tone = audio.createOscillator();
  const gain = audio.createGain();
  const now = audio.currentTime;
  tone.frequency.value = 660;
  gain.gain.setValueAtTime(0.08, now);
  gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.25);
  tone.connect(gain).connect(output);
  tone.onended = () => {
    tone.disconnect();
    gain.disconnect();
  };
  tone.start();
  tone.stop(now + 0.25);
}

/**
 * D29: a US ring (440 and 480 Hz, two seconds on, four off) while a callback rings, through the session's one audio
 * engine. Starting twice rings once; stopping when silent does nothing.
 */
export function createRinger(
  context: () => AudioContext = sessionAudio,
  every: (run: () => void, ms: number) => () => void = (run, ms) => {
    const timer = setInterval(run, ms);
    return () => clearInterval(timer);
  },
  output?: AudioNode,
) {
  let stop: (() => void) | null = null;
  return {
    get ringing() {
      return stop !== null;
    },
    start() {
      if (stop) return;
      const audio = context();
      if (!output)
        void audio
          .resume?.()
          .catch((error) => console.warn("Ringtone playback:", error));
      const gain = audio.createGain();
      gain.gain.value = 0;
      gain.connect(output ?? audio.destination);
      const tones = [440, 480].map((hz) => {
        const tone = audio.createOscillator();
        tone.frequency.value = hz;
        tone.connect(gain);
        tone.start();
        return tone;
      });
      const ring = () => {
        const now = audio.currentTime;
        gain.gain.setValueAtTime(0.08, now);
        gain.gain.setValueAtTime(0, now + 2);
      };
      ring();
      const cancel = every(ring, 6000);
      stop = () => {
        cancel();
        for (const tone of tones) {
          tone.stop();
          tone.disconnect();
        }
        gain.disconnect();
      };
    },
    stop() {
      stop?.();
      stop = null;
    },
  };
}
