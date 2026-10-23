// Raw speech, quotes and helper text expire after five minutes; they never enter the offline cache.
const RAW = new Set([
  "transcript",
  "transcript_revisions",
  "quote",
  "quotes",
  "coach",
  "coach_events",
  "say_or_ask",
  "alternatives",
  "answer",
  "helper_answer",
  "recent_dialogue",
  "last_remote",
  "recording_url",
]);
export function persistentCache<T>(value: T): T {
  const clean = (v: unknown): unknown => {
    if (Array.isArray(v)) return v.map(clean);
    if (!v || typeof v !== "object") return v;
    return Object.fromEntries(
      Object.entries(v)
        .filter(([key]) => !RAW.has(key) || key.startsWith("transcript"))
        .map(([key, item]) => [
          key,
          key.startsWith("transcript")
            ? []
            : key === "raw_evidence_status"
              ? "expired"
              : clean(item),
        ]),
    );
  };
  return clean(value) as T;
}

/** A saved keypad press may replay only briefly; its digits never outlive that. */
export const KEYPAD_REPLAY_MS = 10_000;
export function boundKeypad<T extends { created_at: number; digits: string }>(
  value: T | null,
  now = Date.now(),
): T | null {
  if (!value) return null;
  const age = now - value.created_at;
  if (!Number.isFinite(age) || age < 0 || age >= 300_000) return null;
  return age < KEYPAD_REPLAY_MS ? value : { ...value, digits: "" };
}

type Temporary = {
  raw_remaining_ms?: number;
  remaining_ms?: number;
  _expires?: number;
  _monoExpires?: number;
};
type Speech = {
  transcript?: Temporary[];
  coach?: Temporary | null;
  raw_evidence_status?: string;
};
/** First-seen deadlines by segment revision: a refresh may shorten a countdown, never restart it. */
const deadlines = new Map<string, { wall: number; mono: number }>();

export function receiveSpeech<T extends Speech>(
  value: T,
  now = Date.now(),
  mono = performance.now(),
): T {
  for (const [key, seen] of deadlines)
    if (seen.wall <= now || seen.mono <= mono) deadlines.delete(key);
  const stamp = <
    V extends Temporary & { segment_id?: string; revision?: number },
  >(
    v: V,
    remaining: number | undefined,
  ): V => {
    const ttl = Math.min(
      300000,
      Math.max(0, Number.isFinite(remaining) ? remaining! : 0),
    );
    let wall = now + ttl;
    let monoEnd = mono + ttl;
    if (v.segment_id) {
      const key = `${v.segment_id}:${v.revision ?? ""}`;
      const seen = deadlines.get(key);
      if (seen) {
        wall = Math.min(wall, seen.wall);
        monoEnd = Math.min(monoEnd, seen.mono);
      }
      deadlines.set(key, { wall, mono: monoEnd });
    }
    return { ...v, _expires: wall, _monoExpires: monoEnd };
  };
  return {
    ...value,
    ...(value.transcript
      ? {
          transcript: value.transcript.map((s) => stamp(s, s.raw_remaining_ms)),
        }
      : {}),
    ...(value.coach
      ? { coach: stamp(value.coach, value.coach.remaining_ms) }
      : {}),
  };
}
export function expireSpeech<T extends Speech>(
  value: T,
  now = Date.now(),
  mono = performance.now(),
): T {
  const live = (v: Temporary) =>
    (v._expires ?? 0) > now && (v._monoExpires ?? 0) > mono;
  const transcript = value.transcript?.filter(live);
  const coach = value.coach && live(value.coach) ? value.coach : null;
  if (
    transcript?.length === value.transcript?.length &&
    coach === (value.coach ?? null)
  )
    return value;
  return {
    ...value,
    ...(transcript ? { transcript } : {}),
    coach,
    ...(value.transcript?.length && !transcript?.length
      ? { raw_evidence_status: "expired" }
      : {}),
  };
}
export function speechDeadline(
  value: Speech,
  now = Date.now(),
  mono = performance.now(),
): number | null {
  const ends = [
    ...(value.transcript ?? []),
    ...(value.coach ? [value.coach] : []),
  ].map((v) => Math.min((v._expires ?? 0) - now, (v._monoExpires ?? 0) - mono));
  return ends.length ? Math.max(0, Math.min(...ends)) : null;
}
export interface ExpiryClock {
  now: () => number;
  mono: () => number;
  setTimer: (run: () => void, delay: number) => unknown;
  clearTimer: (timer: unknown) => void;
}
const browserClock: ExpiryClock = {
  now: () => Date.now(),
  mono: () => performance.now(),
  setTimer: (run, delay) => setTimeout(run, delay),
  clearTimer: (timer) => clearTimeout(timer as ReturnType<typeof setTimeout>),
};
/**
 * Purge `value`'s temporary speech when it is due; returns the cancel function.
 *
 * The deadline is the earlier of a wall and a monotonic clock, which drift apart (a slewed
 * wall clock, a coarsened performance.now), so a timer can fire while both still read
 * "live". Purging then changes nothing and React keeps the same value, so check first and
 * schedule again instead of relying on a re-render.
 */
export function watchSpeechExpiry(
  value: Speech,
  purge: () => void,
  clock: ExpiryClock = browserClock,
): () => void {
  let timer: unknown;
  const schedule = (minimum: number) => {
    const delay = speechDeadline(value, clock.now(), clock.mono());
    if (delay !== null) timer = clock.setTimer(check, Math.max(delay, minimum));
  };
  const check = () => {
    if (expireSpeech(value, clock.now(), clock.mono()) !== value) purge();
    else schedule(1);
  };
  schedule(0);
  return () => clock.clearTimer(timer);
}
