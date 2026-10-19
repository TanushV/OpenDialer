import { useWorkspace } from "../context";
import { useHistory } from "../state/useHistory";
import { HistoryMore } from "./HistoryMore";

export interface StaffedWindow {
  start_at: string;
  end_at: string;
  reps: number;
  rep_ids?: string[];
  pool_id?: string;
}

export interface SimpleStaffing {
  staffing_timezone: string;
  staffing_start_hour: number;
  staffing_end_hour: number;
  staffing_reps: number;
  staffing_weekdays: number[];
  account_channel_limit: number | null;
  minimum_expansion_age_hours: number;
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
  simple,
  onSimple,
}: {
  windows: StaffedWindow[];
  delay: number;
  onChange: (value: StaffedWindow[]) => void;
  onDelay: (value: number) => void;
  simple: SimpleStaffing;
  onSimple: (value: Partial<SimpleStaffing>) => void;
}) {
  const { snapshot: w } = useWorkspace();
  const owners = useHistory(
    "task_owners",
    w.task_owners || [{ id: w.user.id, name: w.user.name }],
  );
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
        available rep minutes still caps the plan. With no dated periods, the
        saved simple schedule below supplies the same planning grid. Name reps
        to account for promised callbacks; anonymous headcount supports unowned
        work only. Overlapping named periods count each rep once. Whole attempts
        share one time grid; partial period edges are omitted. Existing calls
        and reservations reduce availability.
      </p>
      {Array.isArray(simple.staffing_weekdays) ? (
        <details open={!windows.length}>
          <summary>Simple schedule (used without dated periods)</summary>
          <div className="optimization-fields">
            <label>
              Staffing time zone
              <input
                value={simple.staffing_timezone}
                onChange={(e) =>
                  onSimple({ staffing_timezone: e.target.value })
                }
              />
            </label>
            <label>
              Staffing starts (hour)
              <input
                type="number"
                min={0}
                max={23}
                value={simple.staffing_start_hour}
                onChange={(e) =>
                  onSimple({ staffing_start_hour: Number(e.target.value) })
                }
              />
            </label>
            <label>
              Staffing ends (hour)
              <input
                type="number"
                min={1}
                max={24}
                value={simple.staffing_end_hour}
                onChange={(e) =>
                  onSimple({ staffing_end_hour: Number(e.target.value) })
                }
              />
            </label>
            <label>
              Simple staffed reps
              <input
                type="number"
                min={1}
                max={100}
                value={simple.staffing_reps}
                onChange={(e) =>
                  onSimple({ staffing_reps: Number(e.target.value) })
                }
              />
            </label>
          </div>
          <fieldset>
            <legend>Staffed weekdays</legend>
            {[
              "Monday",
              "Tuesday",
              "Wednesday",
              "Thursday",
              "Friday",
              "Saturday",
              "Sunday",
            ].map((day, i) => (
              <label className="check-row" key={day}>
                <input
                  type="checkbox"
                  checked={simple.staffing_weekdays.includes(i)}
                  onChange={(e) =>
                    onSimple({
                      staffing_weekdays: e.target.checked
                        ? [...simple.staffing_weekdays, i].sort()
                        : simple.staffing_weekdays.filter(
                            (value) => value !== i,
                          ),
                    })
                  }
                />
                {day}
              </label>
            ))}
          </fieldset>
        </details>
      ) : (
        <p role="status">
          Saved simple schedule unavailable. Reload settings before changing
          availability.
        </p>
      )}
      {windows.map((window, i) => (
        <div className="optimization-fields" key={i}>
          <label>
            Roster pool (optional)
            <input
              aria-label={`Roster pool ${i + 1}`}
              maxLength={64}
              value={window.pool_id || ""}
              onChange={(e) =>
                change(i, { pool_id: e.target.value || undefined })
              }
            />
          </label>
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
          <label className="check-row">
            <input
              type="checkbox"
              aria-label={`Use named reps ${i + 1}`}
              checked={window.rep_ids !== undefined}
              onChange={(e) =>
                change(i, {
                  rep_ids: e.target.checked ? [w.user.id] : undefined,
                  reps: 1,
                })
              }
            />
            Name available reps
          </label>
          {window.rep_ids !== undefined ? (
            <label>
              Named reps
              <select
                multiple
                aria-label={`Named reps ${i + 1}`}
                value={window.rep_ids}
                onChange={(e) => {
                  const selected = Array.from(
                    e.target.selectedOptions,
                    (option) => option.value,
                  );
                  change(i, { rep_ids: selected, reps: selected.length });
                }}
              >
                {owners.items.map((owner) => (
                  <option key={owner.id} value={owner.id}>
                    {owner.name}
                  </option>
                ))}
                {window.rep_ids
                  .filter(
                    (id) => !owners.items.some((owner) => owner.id === id),
                  )
                  .map((id) => (
                    <option key={id} value={id}>
                      Unavailable or not yet loaded rep
                    </option>
                  ))}
              </select>
            </label>
          ) : (
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
          )}
          <button
            className="text-button"
            aria-label={`Remove staffed period ${i + 1}`}
            onClick={() => onChange(windows.filter((_, index) => index !== i))}
          >
            Remove
          </button>
        </div>
      ))}
      {windows.some((window) => window.rep_ids !== undefined) && (
        <HistoryMore history={owners} label="Load more available reps" />
      )}
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
      <p className="fine">
        Use the same roster pool only when headcount includes the named reps.
        Named members are subtracted from that pool, so overlapping availability
        is counted once.
      </p>
      <label>
        Account concurrent-call limit (blank if unknown)
        <input
          type="number"
          min={1}
          max={1000}
          value={simple.account_channel_limit ?? ""}
          onChange={(e) =>
            onSimple({
              account_channel_limit: e.target.value
                ? Number(e.target.value)
                : null,
            })
          }
        />
      </label>
      <label>
        Shortfall review interval (hours)
        <input
          type="number"
          min={1}
          max={168}
          value={simple.minimum_expansion_age_hours}
          onChange={(e) =>
            onSimple({ minimum_expansion_age_hours: Number(e.target.value) })
          }
        />
      </label>
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
