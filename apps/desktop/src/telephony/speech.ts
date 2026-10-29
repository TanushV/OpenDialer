import processorURL from "./pcm-worklet.ts?worker&url";

/** Uses only the SDK's existing call tracks. Never opens another microphone. */
export function captureSpeech(
  base: string,
  token: string,
  call: string,
  streams: { rep: MediaStream; remote: MediaStream },
  failed: () => void,
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
      r.source?.disconnect();
      r.node?.disconnect();
      r.gain?.disconnect();
      void r.context.close();
      if (r.socket.readyState === WebSocket.OPEN) {
        r.socket.send(JSON.stringify({ stop: true }));
        setTimeout(() => r.socket.close(), 2500);
      } else r.socket.close();
    }
  };
  const fail = () => {
    if (!stopped && !reported) {
      reported = true;
      failed();
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
      const context = new AudioContext();
      const r: (typeof resources)[number] = { side, socket, context };
      resources.push(r);
      const timer = setTimeout(fail, 15000);
      socket.onopen = () => {
        if (!stopped) socket.send(JSON.stringify({ token }));
      };
      socket.onerror = fail;
      socket.onclose = () => {
        clearTimeout(timer);
        if (!stopped) fail();
      };
      socket.onmessage = (event) => {
        let status: string;
        try {
          status = JSON.parse(event.data).status;
        } catch {
          fail();
          return;
        }
        if (status === "unavailable") fail();
        if (status === "stopped") {
          socket.close();
          return;
        }
        if (status !== "ready" || stopped || r.node) return;
        clearTimeout(timer);
        void (async () => {
          await context.audioWorklet.addModule(processorURL);
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
              fail();
              return;
            }
            socket.send(message.data);
          };
          r.source.connect(r.node).connect(r.gain).connect(context.destination);
          await context.resume();
          if (context.state !== "running") fail();
        })().catch(fail);
      };
    }
  } catch {
    fail();
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
