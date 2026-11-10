import { label } from "../context";
import { useLoad } from "../state/useLoad";

interface Report {
  answered: number;
  placed: number;
  window_days: number;
  stages: { stage: string; calls: number; outcomes: Record<string, number> }[];
}

/** D31, R11.6: where answered calls end in the script, by outcome; the one learning view kept after StatML. */
export function DropOff() {
  const { data } = useLoad<Report>("/v1/review/drop-off");
  if (!data) return null;
  return (
    <section className="card padded" aria-label="Where calls end">
      <h2>Where calls end</h2>
      <p className="fine">
        Answered calls in the last {data.window_days} days: {data.answered}.{" "}
        {data.placed === data.answered
          ? "Each shows the script part it ended in."
          : `${data.placed} show the script part they ended in; the rest had no transcript.`}
      </p>
      {data.placed ? (
        <table>
          <thead>
            <tr>
              <th>Script part</th>
              <th>Calls ended here</th>
              <th>Outcomes</th>
            </tr>
          </thead>
          <tbody>
            {data.stages.map((row) => (
              <tr key={row.stage}>
                <td>{row.stage}</td>
                <td>{row.calls}</td>
                <td>
                  {Object.entries(row.outcomes)
                    .sort((a, b) => b[1] - a[1])
                    .map(([outcome, n]) => `${n} ${label(outcome)}`)
                    .join(" · ") || "None"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="muted">Shows once answered calls have a transcript.</p>
      )}
    </section>
  );
}
