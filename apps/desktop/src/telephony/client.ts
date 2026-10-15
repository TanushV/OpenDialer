import type { TelnyxRTC } from '@telnyx/webrtc';
import type { Call } from '@telnyx/webrtc/lib/src/Modules/Verto/webrtc/Call';

export class Telephony {
  private client: TelnyxRTC | null = null;
  private call: Call | null = null;
  private expectedAgent = false;
  private inputId = ''; private outputId = '';
  status = 'disconnected';
  onStatus: (status: string) => void = () => {};
  async connect(token: string, audio: HTMLAudioElement) {
    if (!navigator.mediaDevices?.getUserMedia) throw new Error('This webview does not expose microphone/WebRTC support.');
    const permission = await navigator.mediaDevices.getUserMedia({ audio: true });
    permission.getTracks().forEach(track => track.stop());
    const { TelnyxRTC } = await import('@telnyx/webrtc');
    this.client = new TelnyxRTC({ login_token: token, debug: false, enableCallReports: false, enableCallRecording: false,
      keepConnectionAliveOnSocketClose: true, maxReconnectAttempts: 8 });
    this.client.remoteElement = audio;
    if (this.inputId) this.client.micId = this.inputId;
    if (this.outputId) this.client.speaker = this.outputId;
    this.client.on('telnyx.notification', event => {
      if (event.type === 'callUpdate' && event.call) {
        if (event.call.state === 'ringing' && this.expectedAgent) { this.call = event.call; event.call.answer({ remoteElement: audio, video: false }); }
        if (event.call.state === 'active') { this.call = event.call; this.onStatus('active'); }
        if (['hangup', 'destroy'].includes(event.call.state)) this.onStatus('agent-disconnected');
      }
    });
    this.client.on('telnyx.socket.close', () => this.onStatus('reconnecting'));
    await new Promise<void>((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error('Telephony registration timed out')), 15000);
      this.client!.on('telnyx.ready', () => { clearTimeout(timeout); this.status = 'ready'; this.onStatus('registered'); resolve(); });
      this.client!.on('telnyx.error', () => { clearTimeout(timeout); reject(new Error('Telephony registration failed')); });
      this.client!.connect();
    });
  }
  expectAgentLeg() { this.expectedAgent = true; }
  mute() { this.call?.toggleAudioMute(); }
  dtmf(value: string) { if (!/^[0-9*#]+$/.test(value)) throw new Error('Use keypad digits only.'); this.call?.dtmf(value); }
  async input(id: string) { this.inputId = id; if (this.client) this.client.micId = id; if (this.call) await this.call.setAudioInDevice(id); }
  async output(id: string) { this.outputId = id; if (this.client) this.client.speaker = id; if (this.call) await this.call.setAudioOutDevice(id); }
  async devices() { return await navigator.mediaDevices.enumerateDevices(); }
  async emergencyHangup() { await this.call?.hangup(); }
  disconnect() { this.client?.disconnect(); this.client = null; this.call = null; }
}
