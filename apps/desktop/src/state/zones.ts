/** Callback, meeting and task times are entered in the prospect's explicit IANA zone. */
export const deviceZone = () =>
  Intl.DateTimeFormat().resolvedOptions().timeZone;

function valid(zone: string | null | undefined): zone is string {
  if (!zone) return false;
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: zone });
    return true;
  } catch {
    return false;
  }
}

/** The first valid explicit zone; the device zone only when none is known. */
export function chooseZone(...candidates: (string | null | undefined)[]) {
  const zone = candidates.find(valid);
  return zone
    ? { zone, explicit: true }
    : { zone: deviceZone(), explicit: false };
}

export function zoneCaption(choice: { zone: string; explicit: boolean }) {
  const name = choice.zone.replaceAll("_", " ");
  return choice.explicit
    ? name
    : `your device zone, ${name}; prospect zone unknown`;
}

function wall(instant: number, zone: string) {
  const part = Object.fromEntries(
    new Intl.DateTimeFormat("en-US", {
      timeZone: zone,
      hourCycle: "h23",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    })
      .formatToParts(instant)
      .map((p) => [p.type, p.value]),
  );
  return `${part.year}-${part.month}-${part.day}T${part.hour}:${part.minute}`;
}

/** A datetime-local value showing the instant's wall-clock time in `zone`. */
export function zonedInput(value: string | null | undefined, zone: string) {
  if (!value) return "";
  const instant = Date.parse(value);
  return Number.isFinite(instant) ? wall(instant, zone) : "";
}

const minutes = (local: string) => Date.parse(local + ":00Z");

/** Converts a datetime-local wall-clock value in `zone` to a UTC instant. */
export function zonedInstant(local: string, zone: string) {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(local))
    throw new Error("Enter a complete date and time.");
  const target = minutes(local);
  let instant = target;
  // Two corrections settle every offset, including the hour after a DST change.
  for (let i = 0; i < 2; i++) instant += target - minutes(wall(instant, zone));
  if (wall(instant, zone) !== local)
    throw new Error(
      `${local.replace("T", " ")} does not exist in ${zone} because of a daylight-saving change. Choose another time.`,
    );
  return new Date(instant).toISOString();
}

/** The prospect's current local time, or null without a known zone. */
export function localTime(zone: string | null | undefined, now = Date.now()) {
  if (!valid(zone)) return null;
  return new Date(now).toLocaleTimeString([], {
    timeZone: zone,
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "short",
  });
}
