import { sessionAudio } from "./speech";

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
) {
  let stop: (() => void) | null = null;
  return {
    get ringing() {
      return stop !== null;
    },
    start() {
      if (stop) return;
      const audio = context();
      void audio.resume?.().catch(() => undefined);
      const gain = audio.createGain();
      gain.gain.value = 0;
      gain.connect(audio.destination);
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
