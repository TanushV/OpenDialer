import { PCM } from "./pcm";
declare const sampleRate: number;
declare class AudioWorkletProcessor {
  port: MessagePort;
}
declare function registerProcessor(
  name: string,
  processor: typeof AudioWorkletProcessor,
): void;
class SpeechPCM extends AudioWorkletProcessor {
  private pcm = new PCM(sampleRate, (packet) =>
    this.port.postMessage(packet.buffer, [packet.buffer]),
  );
  process(inputs: Float32Array[][]) {
    const channels = inputs[0];
    if (channels?.length) {
      const mono = channels[0].slice();
      for (let i = 0; i < mono.length; i++) {
        for (let c = 1; c < channels.length; c++) mono[i] += channels[c][i];
        mono[i] /= channels.length;
      }
      this.pcm.push(mono);
    }
    return true;
  }
}
registerProcessor("speech-pcm", SpeechPCM);
