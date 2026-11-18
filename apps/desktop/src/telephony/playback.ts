const PLAYBACK_WAIT_MS = 5_000;

export async function waitForPlayback<T>(work: Promise<T>, message: string) {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      work,
      new Promise<never>((_, reject) => {
        timer = setTimeout(() => reject(new Error(message)), PLAYBACK_WAIT_MS);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

export async function playOutput(element: HTMLAudioElement, name: string) {
  try {
    await waitForPlayback(
      element.play(),
      `${name} did not start within five seconds.`,
    );
  } catch (error) {
    const reason =
      error instanceof DOMException && error.name === "NotAllowedError"
        ? "audio playback was blocked"
        : error instanceof DOMException
          ? error.name
          : error instanceof Error
            ? error.message
            : "playback failed";
    throw new Error(`${name}: ${reason}`);
  }
}

export async function setPlaybackOutput(
  element: HTMLAudioElement,
  device: string,
) {
  if (typeof element.setSinkId === "function") {
    await element.setSinkId(device); // An empty ID explicitly restores the system default.
  } else if (device) {
    throw new Error(
      "This app window cannot select a speaker. Choose the output in your system sound settings.",
    );
  }
}

/** Watch the existing speaker stream for the first audible frame. Never route or store these samples. */
export function onFirstRemoteSound(
  audio: AudioContext,
  element: HTMLAudioElement,
  heard: () => void,
) {
  const analyser = audio.createAnalyser();
  analyser.fftSize = 1024;
  const samples = new Float32Array(analyser.fftSize);
  let source: MediaStreamAudioSourceNode | undefined;
  let stream: MediaStream | undefined;
  const stop = () => {
    clearInterval(timer);
    source?.disconnect();
  };
  const timer = setInterval(() => {
    const current = element.srcObject;
    if (
      !(current instanceof MediaStream) ||
      !current.getAudioTracks().length ||
      element.paused
    )
      return;
    if (current !== stream) {
      source?.disconnect();
      try {
        source = audio.createMediaStreamSource(current);
        source.connect(analyser);
        stream = current;
      } catch (error) {
        console.warn("Could not detect the start of remote audio:", error);
        stop(); // The carrier answer event still stops ringback.
        return;
      }
    }
    analyser.getFloatTimeDomainData(samples);
    let energy = 0;
    for (const sample of samples) energy += sample * sample;
    // Ignore silence and very low-level comfort noise on the rep leg.
    if (energy / samples.length >= 0.006 ** 2) {
      stop();
      heard();
    }
  }, 25);
  return stop;
}
