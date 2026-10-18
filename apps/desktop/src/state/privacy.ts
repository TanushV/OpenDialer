// Only derived, nonverbatim state is eligible for a durable desktop cache.
export function persistentCache<T>(value: T): T {
  const clean = (v: unknown): unknown => {
    if (Array.isArray(v)) return v.map(clean);
    if (!v || typeof v !== "object") return v;
    const result: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(v)) {
      if (
        [
          "coach",
          "coach_events",
          "recent_dialogue",
          "last_remote",
          "recording_url",
          "recording_urls",
          "quote",
          "verbatim_time_expression",
          "say_or_ask",
          "private_question",
          "raw_content",
          "_expires",
          "_monoExpires",
        ].includes(key)
      )
        continue;
      result[key] =
        key === "transcript" || key === "transcript_revisions"
          ? []
          : key === "raw_evidence_status"
            ? "expired"
            : clean(item);
    }
    return result;
  };
  return clean(value) as T;
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
export function receiveSpeech<T extends Speech>(
  value: T,
  now = Date.now(),
  mono = performance.now(),
): T {
  const stamp = <V extends Temporary>(
    v: V,
    remaining: number | undefined,
  ): V => {
    const ttl = Math.min(
      300000,
      Math.max(0, Number.isFinite(remaining) ? remaining! : 0),
    );
    return { ...v, _expires: now + ttl, _monoExpires: mono + ttl };
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
export function speechDeadline(value: Speech): number | null {
  const ends = [
    ...(value.transcript ?? []),
    ...(value.coach ? [value.coach] : []),
  ].map((v) =>
    Math.min(
      (v._expires ?? 0) - Date.now(),
      (v._monoExpires ?? 0) - performance.now(),
    ),
  );
  return ends.length ? Math.max(0, Math.min(...ends)) : null;
}
