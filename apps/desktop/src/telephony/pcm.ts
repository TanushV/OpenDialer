/** Mono PCM16 at 16 kHz; retain only the current 100 ms packet. */
export class PCM {
  private phase = 0;
  private sum = 0;
  private count = 0;
  private index = 0;
  private packet = new Int16Array(1600);
  constructor(
    private rate: number,
    private emit: (packet: Int16Array) => void,
  ) {}
  push(samples: Float32Array) {
    for (const value of samples) {
      this.sum += value;
      this.count++;
      this.phase += 16000;
      if (this.phase < this.rate) continue;
      this.phase -= this.rate;
      const sample = Math.max(-1, Math.min(1, this.sum / this.count));
      this.packet[this.index++] = sample * (sample < 0 ? 32768 : 32767);
      this.sum = this.count = 0;
      if (this.index === 1600) {
        this.emit(this.packet);
        this.packet = new Int16Array(1600);
        this.index = 0;
      }
    }
  }
}
