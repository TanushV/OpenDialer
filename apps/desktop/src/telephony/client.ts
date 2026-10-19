import type { TelnyxRTC } from "@telnyx/webrtc";
import type { Call } from "@telnyx/webrtc/lib/src/Modules/Verto/webrtc/Call";

const providerId = (call: Call | null) => call?.telnyxIDs?.telnyxCallControlId;
const ended = (call: Call) => ["hangup", "destroy"].includes(call.state);

export class Telephony {
  private client: TelnyxRTC | null = null;
  private audio: HTMLAudioElement | null = null;
  private agent: Call | null = null;
  private returnCall: Call | null = null;
  private expectedAgent: string | null = null;
  private expectedReturn: string | null = null;
  private pending = new Map<string, Call>();
  private answers = new Map<string, Promise<void>>();
  private closed = new Set<string>();
  private session = 0;
  private connecting: Promise<void> | null = null;
  private cancelRegistration: (() => void) | null = null;
  private inputId = "";
  private outputId = "";
  status = "disconnected";
  onStatus: (status: string) => void = () => {};

  private report(status: string) {
    this.status = status;
    this.onStatus(status);
  }
  async connect(token: string, audio: HTMLAudioElement) {
    if (this.connecting) return this.connecting;
    if (this.client) {
      if (this.status === "reconnecting")
        throw new Error(
          "Wait for telephony to reconnect before becoming ready.",
        );
      return;
    }
    const session = ++this.session;
    const work = this.register(token, audio, session);
    this.connecting = work;
    try {
      await work;
    } catch (error) {
      if (this.session === session) this.disconnect();
      throw error;
    } finally {
      if (this.connecting === work) this.connecting = null;
    }
  }
  private async register(
    token: string,
    audio: HTMLAudioElement,
    session: number,
  ) {
    if (!navigator.mediaDevices?.getUserMedia)
      throw new Error(
        "This webview does not expose microphone/WebRTC support.",
      );
    const permission = await navigator.mediaDevices.getUserMedia({
      audio: true,
    });
    permission.getTracks().forEach((track) => track.stop());
    if (session !== this.session)
      throw new Error("Telephony connection cancelled.");
    const { TelnyxRTC } = await import("@telnyx/webrtc");
    if (session !== this.session)
      throw new Error("Telephony connection cancelled.");
    const client = new TelnyxRTC({
      login_token: token,
      debug: false,
      enableCallReports: false,
      enableCallRecording: false,
      keepConnectionAliveOnSocketClose: true,
      maxReconnectAttempts: 8,
    });
    this.client = client;
    this.audio = audio;
    client.remoteElement = audio;
    if (this.inputId) client.micId = this.inputId;
    if (this.outputId) client.speaker = this.outputId;
    const current = () => session === this.session && client === this.client;
    client.on("telnyx.notification", (event) => {
      if (current() && event.type === "callUpdate" && event.call)
        this.callUpdated(event.call);
    });
    client.on("telnyx.socket.close", () => {
      if (current()) this.report("reconnecting");
    });
    await new Promise<void>((resolve, reject) => {
      const timeout = setTimeout(
        () => reject(new Error("Telephony registration timed out")),
        15000,
      );
      const finish = (error?: Error) => {
        clearTimeout(timeout);
        this.cancelRegistration = null;
        if (error) reject(error);
        else resolve();
      };
      this.cancelRegistration = () =>
        finish(new Error("Telephony connection cancelled."));
      client.on("telnyx.ready", () => {
        if (!current()) return;
        this.report("registered");
        finish();
      });
      client.on("telnyx.error", () => {
        if (!current()) return;
        this.report("reconnecting");
        finish(new Error("Telephony registration failed"));
      });
      client.connect();
    });
  }
  private closeIdentity(id: string) {
    this.pending.delete(id);
    this.answers.delete(id);
    this.closed.add(id);
    if (this.closed.size > 32)
      this.closed.delete(this.closed.values().next().value!);
    if (id === this.expectedAgent) {
      this.expectedAgent = null;
      this.agent = null;
    }
    if (id === this.expectedReturn) {
      this.expectedReturn = null;
      this.returnCall = null;
    }
  }
  private callUpdated(call: Call) {
    const id = providerId(call);
    if (!id || this.closed.has(id)) return;
    if (ended(call)) {
      const wasAgent = id === this.expectedAgent;
      const wasReturn = id === this.expectedReturn;
      this.closeIdentity(id);
      if (wasReturn && this.agent?.remoteStream && this.audio)
        this.audio.srcObject = this.agent.remoteStream;
      if (wasAgent) this.report("agent-disconnected");
      else if (wasReturn) this.report("return-ended");
      else this.onStatus(this.status);
      return;
    }
    if (call.state === "ringing" && call.direction === "inbound") {
      if (!this.pending.has(id) && this.pending.size >= 4)
        this.pending.delete(this.pending.keys().next().value!);
      this.pending.set(id, call);
      if (id === this.expectedAgent) this.answerAgent(call);
      else
        this.report(
          id === this.expectedReturn ? "incoming" : "incoming-unverified",
        );
    } else if (call.state === "active") {
      this.pending.delete(id);
      if (id === this.expectedAgent) {
        this.agent = call;
        this.report("active");
      } else if (id === this.expectedReturn && this.answers.has(id)) {
        this.returnCall = call;
        this.report("return-active");
      }
    }
  }
  private answer(call: Call): Promise<void> {
    const id = providerId(call)!;
    const existing = this.answers.get(id);
    if (existing) return existing;
    const result = Promise.resolve().then(async () => {
      if (
        !this.client ||
        this.closed.has(id) ||
        ![this.expectedAgent, this.expectedReturn].includes(id)
      )
        throw new Error("Incoming call is no longer current.");
      await call.answer({ remoteElement: this.audio!, video: false });
    });
    this.answers.set(id, result);
    return result;
  }
  private answerAgent(call: Call) {
    this.agent = call;
    const session = this.session;
    void this.answer(call).catch(() => {
      if (this.session === session) this.report("agent-disconnected");
    });
  }
  expectAgentLeg(id: string) {
    this.expectedAgent = id;
    const pending = this.pending.get(id);
    if (pending && !this.closed.has(id)) this.answerAgent(pending);
  }
  expectReturnLeg(id: string) {
    this.expectedReturn = id;
    if (this.pending.has(id) && !this.closed.has(id)) this.report("incoming");
  }
  get incoming() {
    const id = this.expectedReturn;
    return id && this.pending.has(id) && !this.closed.has(id)
      ? { providerId: id }
      : null;
  }
  get hasIncoming() {
    return (
      Boolean(this.returnCall) ||
      [...this.pending.keys()].some((id) => id !== this.expectedAgent)
    );
  }
  returnActive(id: string) {
    return (
      providerId(this.returnCall) === id && this.returnCall?.state === "active"
    );
  }
  async acceptIncoming(id: string) {
    if (id !== this.expectedReturn || this.closed.has(id))
      throw new Error("Incoming call identity changed.");
    const call = this.pending.get(id) || this.returnCall;
    if (!call || providerId(call) !== id)
      throw new Error("Waiting for the matching incoming audio.");
    this.returnCall = call;
    await this.answer(call);
  }
  private selected(id?: string) {
    if (id)
      return (
        [this.returnCall, this.agent].find((call) => providerId(call) === id) ||
        null
      );
    return this.returnCall || this.agent;
  }
  get activeIdentity() {
    return providerId(this.selected()) || null;
  }
  mute(id?: string) {
    this.selected(id)?.toggleAudioMute();
  }
  dtmf(value: string) {
    if (!/^[0-9*#]+$/.test(value)) throw new Error("Use keypad digits only.");
    this.selected()?.dtmf(value);
  }
  async input(id: string) {
    this.inputId = id;
    if (this.client) this.client.micId = id;
    for (const call of [this.agent, this.returnCall])
      if (call) await call.setAudioInDevice(id);
  }
  async output(id: string) {
    this.outputId = id;
    if (this.client) this.client.speaker = id;
    for (const call of [this.agent, this.returnCall])
      if (call) await call.setAudioOutDevice(id);
  }
  async devices() {
    return navigator.mediaDevices.enumerateDevices();
  }
  async emergencyHangup(id?: string) {
    const call =
      this.selected(id) ||
      (id === this.expectedReturn ? this.pending.get(id) : null);
    if (!call) return;
    if (!ended(call)) await call.hangup();
    const identity = providerId(call)!;
    const wasAgent = identity === this.expectedAgent;
    const wasReturn = identity === this.expectedReturn;
    this.closeIdentity(identity);
    if (wasReturn && this.agent?.remoteStream && this.audio)
      this.audio.srcObject = this.agent.remoteStream;
    if (wasAgent) this.report("agent-disconnected");
    else if (wasReturn) this.report("return-ended");
  }
  async releaseIdleAgent() {
    if (this.hasIncoming) return;
    const session = this.session;
    const id = this.expectedAgent;
    if (id) await this.emergencyHangup(id);
    if (this.session === session && !this.expectedAgent && !this.hasIncoming)
      this.disconnect();
  }
  disconnect() {
    ++this.session;
    this.cancelRegistration?.();
    this.client?.disconnect();
    this.client = null;
    this.audio = null;
    this.agent = this.returnCall = null;
    this.expectedAgent = this.expectedReturn = null;
    this.pending.clear();
    this.answers.clear();
    this.closed.clear();
    this.report("disconnected");
  }
}
