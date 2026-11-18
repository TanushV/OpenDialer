// Only served by Vite in the browser test. The production client still owns
// leg selection, playback, and hang-up. A local peer supplies the SDK's media.
import { sessionAudio } from "../src/telephony/speech";

function tone(audio: AudioContext, hz: number, volume: number) {
  const oscillator = audio.createOscillator();
  const gain = audio.createGain();
  const output = audio.createMediaStreamDestination();
  oscillator.frequency.value = hz;
  gain.gain.value = volume;
  oscillator.connect(gain).connect(output);
  oscillator.start();
  return {
    stream: output.stream,
    volume: (value: number) => (gain.gain.value = value),
    stop: () => {
      oscillator.stop();
      output.stream.getTracks().forEach((track) => track.stop());
    },
  };
}

function exchangeIce(from: RTCPeerConnection, to: RTCPeerConnection) {
  const queued: RTCIceCandidate[] = [];
  from.onicecandidate = ({ candidate }) => {
    if (!candidate) return;
    if (to.remoteDescription) void to.addIceCandidate(candidate);
    else queued.push(candidate);
  };
  return () => Promise.all(queued.splice(0).map((c) => to.addIceCandidate(c)));
}

function received(peer: RTCPeerConnection) {
  return new Promise<MediaStream>((resolve) => {
    peer.ontrack = ({ streams }) => resolve(streams[0]);
  });
}

async function audioPair() {
  const audio = sessionAudio();
  const local = new RTCPeerConnection();
  const remote = new RTCPeerConnection();
  const rep = tone(audio, 670, 0.12);
  const prospect = tone(audio, 310, 0);
  const localTrack = received(local);
  const remoteTrack = received(remote);
  const localIce = exchangeIce(local, remote);
  const remoteIce = exchangeIce(remote, local);
  local.addTrack(rep.stream.getAudioTracks()[0], rep.stream);
  remote.addTrack(prospect.stream.getAudioTracks()[0], prospect.stream);
  await local.setLocalDescription(await local.createOffer());
  await remote.setRemoteDescription(local.localDescription!);
  await localIce();
  await remote.setLocalDescription(await remote.createAnswer());
  await local.setRemoteDescription(remote.localDescription!);
  await remoteIce();
  const [repReceives, prospectReceives] = await Promise.all([
    localTrack,
    remoteTrack,
  ]);
  return {
    rep: rep.stream,
    repReceives,
    prospectReceives,
    speak: () => prospect.volume(0.12),
    status: async () => ({
      local: local.connectionState,
      remote: remote.connectionState,
      incoming: [...(await local.getStats()).values()]
        .filter((report) => report.type === "inbound-rtp")
        .map(({ packetsReceived, totalAudioEnergy }) => ({
          packetsReceived,
          totalAudioEnergy,
        })),
    }),
    close: () => {
      local.close();
      remote.close();
      rep.stop();
      prospect.stop();
    },
  };
}

async function energy(stream: MediaStream) {
  const audio = sessionAudio();
  const input = audio.createMediaStreamSource(stream);
  const analyser = audio.createAnalyser();
  const samples = new Float32Array(analyser.fftSize);
  const silent = audio.createGain();
  silent.gain.value = 0;
  input.connect(analyser).connect(silent).connect(audio.destination);
  await new Promise((resolve) => setTimeout(resolve, 80));
  analyser.getFloatTimeDomainData(samples);
  input.disconnect();
  analyser.disconnect();
  silent.disconnect();
  return Math.sqrt(
    samples.reduce((sum, value) => sum + value * value, 0) / samples.length,
  );
}

type Media = Awaited<ReturnType<typeof audioPair>>;
type Agent = {
  state: string;
  direction: string;
  options: object;
  telnyxIDs: { telnyxCallControlId: string };
  localStream?: MediaStream;
  remoteStream?: MediaStream;
  answer(): Promise<void>;
  hangup(): Promise<void>;
};

declare global {
  interface Window {
    telnyxMedia: TelnyxRTC;
    denyTestMicrophone: boolean;
  }
}

export class TelnyxRTC {
  private listeners = new Map<string, (event?: unknown) => void>();
  private media?: Media;
  private calls = new Set<Agent>();
  stopped: string[] = [];

  constructor() {
    window.telnyxMedia = this;
  }

  on(name: string, handler: (event?: unknown) => void) {
    this.listeners.set(name, handler);
  }

  connect() {
    this.listeners.get("telnyx.ready")?.();
  }

  disconnect() {
    for (const call of this.calls) void call.hangup();
  }

  offer(id: string) {
    let media: Media | undefined;
    const notify = () =>
      this.listeners.get("telnyx.notification")?.({ type: "callUpdate", call });
    const call: Agent = {
      direction: "inbound",
      state: "ringing",
      options: {},
      telnyxIDs: { telnyxCallControlId: id },
      answer: async () => {
        media = await audioPair();
        this.media = media;
        call.localStream = media.rep;
        call.remoteStream = media.repReceives;
        call.state = "active";
        notify();
      },
      hangup: async () => {
        media?.close();
        this.calls.delete(call);
        if (this.media === media) this.media = undefined;
        this.stopped.push(id);
        call.state = "hangup";
        notify();
      },
    };
    this.calls.add(call);
    notify();
  }

  speak() {
    this.media?.speak();
  }

  async status() {
    return this.media?.status();
  }

  async levels() {
    const [voice, cue] =
      document.querySelectorAll<HTMLAudioElement>(".dialer audio");
    const level = (element: HTMLAudioElement) =>
      element.paused || !(element.srcObject instanceof MediaStream)
        ? 0
        : energy(element.srcObject);
    const [ringback, repHears, prospectHears] = await Promise.all([
      level(cue),
      level(voice),
      this.media ? energy(this.media.prospectReceives) : 0,
    ]);
    return { ringback, repHears, prospectHears };
  }
}
