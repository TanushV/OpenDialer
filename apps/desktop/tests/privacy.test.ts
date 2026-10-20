import { expect, test } from "vitest";
import {
  boundKeypad,
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

test("disk cache keeps only allowlisted fields, so a new field fails closed", () => {
  const clean = persistentCache({
    id: "call",
    notes: { summary: "Derived", future_raw_text: "RAW-NEW" },
    helper_answer: "RAW-HELP-ANSWER",
    pages: { leads: { total: 3, next_cursor: null, raw_preview: "RAW-P" } },
    metrics: { rep_talk_ratio: 0.4 },
  });
  expect(JSON.stringify(clean)).not.toContain("RAW-");
  expect(clean.notes.summary).toBe("Derived");
  expect(clean.pages.leads.total).toBe(3);
  expect(clean.metrics.rep_talk_ratio).toBe(0.4);
});

test("saved keypad digits outlive only their replay window", () => {
  const at = 1_000_000;
  const value = {
    call_id: "call",
    idempotency_key: "key",
    digits: "1234",
    created_at: at,
  };
  expect(boundKeypad(value, at + 9_000)).toEqual(value);
  expect(boundKeypad(value, at + 11_000)).toEqual({ ...value, digits: "" });
  expect(boundKeypad(value, at + 301_000)).toBeNull();
  expect(boundKeypad({ ...value, created_at: at + 60_000 }, at)).toBeNull();
  expect(boundKeypad(null, at)).toBeNull();
});
