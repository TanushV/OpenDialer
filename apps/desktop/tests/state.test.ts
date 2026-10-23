import { describe, expect, it } from "vitest";
import { isActiveCall, isTerminal } from "../src/types";
import { OptOutJournal } from "../src/state/optouts";

class MemoryStorage {
  values = new Map<string, string>();
  getItem(k: string) {
    return this.values.get(k) ?? null;
  }
  setItem(k: string, v: string) {
    this.values.set(k, v);
  }
  removeItem(k: string) {
    this.values.delete(k);
  }
}

describe("durable offline opt-outs", () => {
  it("persists pending events through a restart and blocks calls until each acknowledgement", async () => {
    const storage = new MemoryStorage();
    const first = new OptOutJournal(storage, "tenant-a");
    first.add("+14155550101", "opt_out");
    const restarted = new OptOutJournal(storage, "tenant-a");
    expect(restarted.blocksCalling).toBe(true);
    await restarted.synchronize(async () => {
      throw new Error("offline");
    });
    expect(restarted.blocksCalling).toBe(true);
    await restarted.synchronize(async (event) => ({ id: event.id }));
    expect(restarted.blocksCalling).toBe(false);
  });
  it("does not expose another workspace journal", () => {
    const storage = new MemoryStorage();
    new OptOutJournal(storage, "tenant-a").add("+14155550101", "opt_out");
    expect(new OptOutJournal(storage, "tenant-b").blocksCalling).toBe(false);
  });
});

describe("one call-state definition", () => {
  it("treats carrier-finished and missing calls as terminal", () => {
    for (const state of ["ENDED", "FINALIZED", "BUSY", "NO_ANSWER"])
      expect(isTerminal({ state })).toBe(true);
    expect(isTerminal(null)).toBe(true);
    expect(isTerminal({ state: "RINGING" })).toBe(false);
  });
  it("does not count a detached voicemail or wrap-up as an active call", () => {
    expect(isActiveCall({ state: "HUMAN" })).toBe(true);
    expect(isActiveCall({ state: "VOICEMAIL_PLAYING", detached: true })).toBe(
      false,
    );
    expect(isActiveCall({ state: "NO_ANSWER" })).toBe(false);
    expect(isActiveCall(null)).toBe(false);
  });
});
