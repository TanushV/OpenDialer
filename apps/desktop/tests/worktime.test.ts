import { describe, expect, it } from "vitest";
import { heartbeatActivity } from "../src/state/worktime";

const now = Date.parse("2026-10-18T18:00:00Z");
const state = { presence: "ready", repId: "rep", calls: [] };

describe("transparent working-time heartbeat", () => {
  it("counts declared ready time and pauses forgotten availability", () => {
    expect(heartbeatActivity(state, 60_000, now)).toBe("ready");
    expect(heartbeatActivity(state, 15 * 60_000, now)).toBe("idle");
    expect(heartbeatActivity({ ...state, presence: "paused" }, 0, now)).toBeNull();
  });
  it("keeps a current call alive while the rep listens without interacting", () => {
    const calls = [{ rep_id: "rep", state: "HUMAN" }];
    expect(heartbeatActivity({ ...state, calls }, 20 * 60_000, now)).toBe("call");
  });
  it("never uses another rep or detached voicemail as observed work", () => {
    const calls = [
      { rep_id: "other", state: "HUMAN" },
      { rep_id: "rep", state: "VM_PLAYING", detached: true },
    ];
    expect(heartbeatActivity({ ...state, calls }, 20 * 60_000, now)).toBe("idle");
  });
  it("allows recent wrap-up while paused but not old unfinished dispositions", () => {
    const calls = [{ rep_id: "rep", state: "ENDED", ended_at: new Date(now - 60_000).toISOString() }];
    expect(heartbeatActivity({ ...state, presence: "paused", calls }, 0, now)).toBe("wrapup");
    expect(heartbeatActivity({ ...state, presence: "paused", calls }, 16 * 60_000, now)).toBeNull();
    expect(heartbeatActivity({ ...state, presence: "paused", calls }, 0, now + 16 * 60_000)).toBeNull();
  });
});
