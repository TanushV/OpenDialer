import { expect, test } from "vitest";
import {
  persistentCache,
  receiveSpeech,
  expireSpeech,
} from "../src/state/privacy";

test("disk cache strips raw conversation and helper content while preserving allowed data", () => {
  const payload = {
    id: "call",
    human_notes: "Keep my note",
    transcript: [{ text: "RAW-VOICE" }],
    call: { coach: { say_or_ask: "RAW-HELP" }, recording_url: "RAW-URL" },
    notes: { summary: "Paraphrased summary", evidence: ["segment-id"] },
    coach_events: [{ reason: "RAW-REASON" }],
    raw_evidence_status: "available_temporarily",
  };
  const clean = persistentCache(payload);
  expect(JSON.stringify(clean)).not.toContain("RAW-");
  expect(clean.human_notes).toBe(payload.human_notes);
  expect(clean.notes.summary).toBe(payload.notes.summary);
  expect(clean.transcript).toEqual([]);
});

test("browser expiry uses original remaining life and monotonic time even if wall clock rolls back", () => {
  const state = receiveSpeech(
    {
      transcript: [
        { segment_id: "s", text: "private", raw_remaining_ms: 1000 },
      ],
      coach: null,
    },
    2000,
    100,
  );
  expect(expireSpeech(state, 2200, 300).transcript).toHaveLength(1);
  expect(expireSpeech(state, 500, 1101).transcript).toEqual([]);
});

test("missing or excessive raw lifetime is never an indefinite UI cache", () => {
  const state = receiveSpeech(
    {
      transcript: [
        { text: "no deadline" },
        { text: "too long", raw_remaining_ms: 900000 },
      ],
    },
    0,
    0,
  );
  expect(expireSpeech(state, 0, 0).transcript).toHaveLength(1);
  expect(expireSpeech(state, 300001, 300001).transcript).toEqual([]);
});
