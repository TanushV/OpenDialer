export interface StaffedWindow {
  start_at: string;
  end_at: string;
  reps: number;
}

const localInput = (value: string) => {
  const instant = new Date(value);
  return new Date(instant.getTime() - instant.getTimezoneOffset() * 60_000)
    .toISOString()
    .slice(0, 16);
};

export function Staffing({
  windows,
  delay,
  onChange,
  onDelay,
}: {
  windows: StaffedWindow[];
  delay: number;
  onChange: (value: StaffedWindow[]) => void;
  onDelay: (value: number) => void;
}) {
  const change = (index: number, values: Partial<StaffedWindow>) =>
    onChange(
      windows.map((window, i) =>
        i === index ? { ...window, ...values } : window,
      ),
    );
  return (
    <fieldset className="card padded">
      <legend>Staffed calling periods</legend>
      <p className="small muted">
        Declare when reps can call, excluding breaks. Times use this device’s
        time zone ({Intl.DateTimeFormat().resolvedOptions().timeZone}). Total
        available rep minutes still caps the plan. With no periods, capacity is
        only an aggregate estimate.
      </p>
      {windows.map((window, i) => (
        <div className="optimization-fields" key={i}>
          <label>
            Start
            <input
              aria-label={`Staffed start ${i + 1}`}
              type="datetime-local"
              value={localInput(window.start_at)}
              onChange={(e) => {
                if (e.target.value)
                  change(i, {
                    start_at: new Date(e.target.value).toISOString(),
                  });
              }}
            />
          </label>
          <label>
            End
            <input
              aria-label={`Staffed end ${i + 1}`}
              type="datetime-local"
              value={localInput(window.end_at)}
              onChange={(e) => {
                if (e.target.value)
                  change(i, { end_at: new Date(e.target.value).toISOString() });
              }}
            />
          </label>
          <label>
            Available reps
            <input
              aria-label={`Available reps ${i + 1}`}
              type="number"
              min={1}
              max={100}
              value={window.reps}
              onChange={(e) => change(i, { reps: Number(e.target.value) })}
            />
          </label>
          <button
            className="text-button"
            aria-label={`Remove staffed period ${i + 1}`}
            onClick={() => onChange(windows.filter((_, index) => index !== i))}
          >
            Remove
          </button>
        </div>
      ))}
      <button
        className="secondary"
        disabled={windows.length >= 100}
        onClick={() => {
          const start = new Date();
          start.setSeconds(0, 0);
          onChange([
            ...windows,
            {
              start_at: start.toISOString(),
              end_at: new Date(start.getTime() + 3600000).toISOString(),
              reps: 1,
            },
          ]);
        }}
      >
        Add staffed period
      </button>
      <label>
        New number setup delay (hours)
        <input
          type="number"
          min={0}
          max={168}
          value={delay}
          onChange={(e) => onDelay(Number(e.target.value))}
        />
      </label>
      <p className="small muted">
        Setup delay is a planning assumption. Waiting inventory remains
        unavailable until verified. New rental purchases remain blocked.
      </p>
    </fieldset>
  );
}
