import processorURL from "./pcm-worklet.ts?worker&url";

/** Why live speech capture stopped, sent to the backend's log with the gap report. */
export type SpeechStop =
  | "audio_suspended"
  | "no_audio_engine"
  | "worklet_failed"
  | "socket_error"
  | "socket_closed"
  | "no_ready"
  | "provider_unavailable"
  | "backpressure"
  | "bad_message";

/**
 * One audio engine for the whole calling session. WebKit starts an AudioContext only from the rep's own click or
 * key, and auto-dialed calls start from a timer, so Start calling unlocks it once and every call reuses it
 * (live check, November 10: with a new engine per call, no transcript ever started).
 */
let shared: AudioContext | null = null;
let worklet: Promise<void> | null = null;

function engine() {
  if (!shared || shared.state === "closed") {
    shared = new AudioContext();
    worklet = null;
  }
  return shared;
}

/** The session's audio engine, for short sounds such as the end-of-call tone. */
export function sessionAudio() {
  return engine();
}

/** Call synchronously inside the rep's click or key press (Start calling). */
export function unlockSpeechAudio() {
  try {
    void engine()
      .resume()
      .catch(() => undefined);
  } catch {
    /* No audio engine here: capture reports it when a call starts. */
  }
}

function loadWorklet(context: AudioContext) {
  worklet ??= context.audioWorklet.addModule(processorURL);
  return worklet;
}

/** Uses only the SDK's existing call tracks. Never opens another microphone. */
export function captureSpeech(
  base: string,
  token: string,
  call: string,
  streams: { rep: MediaStream; remote: MediaStream },
  failed: (reason: SpeechStop) => void,
) {
  let stopped = false,
    reported = false;
  const resources: {
    side: "rep" | "remote";
    socket: WebSocket;
    context: AudioContext;
    source?: MediaStreamAudioSourceNode;
    node?: AudioWorkletNode;
    gain?: GainNode;
  }[] = [];
  const stop = () => {
    if (stopped) return;
    stopped = true;
    for (const r of resources) {
      // The engine is shared by the session's calls: disconnect this call's nodes, never close it.
      r.source?.disconnect();
      r.node?.disconnect();
      r.gain?.disconnect();
      if (r.socket.readyState === WebSocket.OPEN) {
        r.socket.send(JSON.stringify({ stop: true }));
        setTimeout(() => r.socket.close(), 2500);
      } else r.socket.close();
    }
  };
  const fail = (reason: SpeechStop) => {
    if (!stopped && !reported) {
      reported = true;
      failed(reason);
      stop();
    }
  };
  try {
    for (const side of ["rep", "remote"] as const) {
      const url = new URL(
        `/v1/calls/${encodeURIComponent(call)}/audio/${side}`,
        base,
      );
      url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
      const socket = new WebSocket(url);
      const context = engine();
      const r: (typeof resources)[number] = { side, socket, context };
      resources.push(r);
      const timer = setTimeout(() => fail("no_ready"), 15000);
      socket.onopen = () => {
        if (!stopped) socket.send(JSON.stringify({ token }));
      };
      socket.onerror = () => fail("socket_error");
      socket.onclose = () => {
        clearTimeout(timer);
        if (!stopped) fail("socket_closed");
      };
      socket.onmessage = (event) => {
        let status: string;
        try {
          status = JSON.parse(event.data).status;
        } catch {
          fail("bad_message");
          return;
        }
        if (status === "unavailable") fail("provider_unavailable");
        if (status === "stopped") {
          socket.close();
          return;
        }
        if (status !== "ready" || stopped || r.node) return;
        clearTimeout(timer);
        void (async () => {
          await loadWorklet(context).catch((error: unknown) => {
            fail("worklet_failed");
            throw error;
          });
          if (stopped) return;
          r.source = context.createMediaStreamSource(streams[side]);
          r.node = new AudioWorkletNode(context, "speech-pcm");
          r.gain = context.createGain();
          r.gain.gain.value = 0;
          r.node.port.onmessage = (message) => {
            if (stopped) return;
            if (
              socket.readyState !== WebSocket.OPEN ||
              socket.bufferedAmount > 64000
            ) {
              fail("backpressure");
              return;
            }
            socket.send(message.data);
          };
          r.source.connect(r.node).connect(r.gain).connect(context.destination);
          await context.resume();
          if (context.state !== "running") fail("audio_suspended");
        })().catch(() => fail("worklet_failed"));
      };
    }
  } catch {
    fail("no_audio_engine");
  }
  return {
    stop,
    update(next: typeof streams) {
      streams = next;
      for (const r of resources) {
        if (!r.node || stopped) continue;
        r.source?.disconnect();
        r.source = r.context.createMediaStreamSource(streams[r.side]);
        r.source.connect(r.node);
      }
    },
  };
}
