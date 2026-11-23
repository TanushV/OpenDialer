import { useLoad } from "../state/useLoad";

interface Rates {
  day: string;
  connected: number;
  short: number;
  short_share: number;
  short_limit: number;
  outbound: number;
  abandoned: number;
  abandoned_share: number;
  abandoned_limit: number;
}

const percent = (share: number) => `${Math.round(share * 100)}%`;

/**
 * Work package 6.17: today's traffic as Telnyx counts it (UTC day). Information only; nothing here stops calling.
 * Short calls are connected calls of 6 seconds or less; abandoned calls end on our side before answer.
 */
export function CarrierRates() {
  const { data } = useLoad<Rates>("/v1/review/carrier-rates");
  if (!data) return null;
  const shortHigh = data.connected > 0 && data.short_share > data.short_limit;
  const abandonedHigh =
    data.outbound > 0 && data.abandoned_share > data.abandoned_limit;
  return (
    <section className="card padded" aria-label="Carrier traffic today">
      <h3>Carrier traffic today (UTC)</h3>
      <p className={shortHigh ? "alert" : "fine"} data-testid="short-share">
        Short calls: {data.short} of {data.connected} connected calls lasted 6
        seconds or less ({percent(data.short_share)}). Telnyx may charge or
        block above {percent(data.short_limit)}.
      </p>
      <p
        className={abandonedHigh ? "alert" : "fine"}
        data-testid="abandoned-share"
      >
        Ended while ringing: {data.abandoned} of {data.outbound} dials (
        {percent(data.abandoned_share)}). Telnyx charges $0.005 each above{" "}
        {percent(data.abandoned_limit)}.
      </p>
    </section>
  );
}
