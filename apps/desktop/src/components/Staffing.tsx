import { useMemo } from "react";
import { useWorkspace } from "../context";
import { useHistory } from "../state/useHistory";
import { limits, type Definitions } from "../state/definitions";
import { zonedInput, zonedInstant } from "../state/zones";
import { HistoryMore } from "./HistoryMore";

export interface StaffedWindow {
  start_at: string;
  end_at: string;
  reps: number;
  rep_ids?: string[];
  pool_id?: string;
}

/** The weekly rep schedule saved with the campaign; one IANA zone for every period. */
export interface SimpleStaffing {
  staffing_timezone: string;
  staffing_start_hour: number;
  staffing_end_hour: number;
  staffing_reps: number;
  staffing_weekdays: number[];
}

const WEEKDAYS = [
  "Monday",
  "Tuesday",
  "Wednesday",
  "Thursday",
  "Friday",
  "Saturday",
  "Sunday",
];

/** An IANA zone picker; a saved zone the engine does not list stays selectable. */
export function ZonePicker({
  title,
  value,
  onChange,
  disabled,
}: {
  title: string;
  value: string;
  onChange: (zone: string) => void;
  disabled?: boolean;
}) {
  const zones = useMemo(() => {
    const listed = ["UTC", ...Intl.supportedValuesOf("timeZone")];
    return listed.includes(value) ? listed : [value, ...listed];
  }, [value]);
  return (
    <label>
      {title}
      <select
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
      >
        {zones.map((zone) => (
          <option key={zone} value={zone}>
            {zone.replaceAll("_", " ")}
          </option>
        ))}
      </select>
    </label>
  );
}

export function WeeklySchedule({
  value,
  bounds,
  onChange,
}: {
  value: SimpleStaffing;
  bounds: Definitions | null;
  onChange: (value: Partial<SimpleStaffing>) => void;
}) {
  const b = bounds?.optimization;
  return (
    <>
      <div className="optimization-fields">
        <ZonePicker
          title="Staffing time zone"
          value={value.staffing_timezone}
          onChange={(zone) => onChange({ staffing_timezone: zone })}
        />
        <label>
          Staffing starts (hour)
          <input
            type="number"
            {...limits(b?.staffing_start_hour)}
            value={value.staffing_start_hour}
            onChange={(e) =>
              onChange({ staffing_start_hour: Number(e.target.value) })
            }
          />
        </label>
        <label>
          Staffing ends (hour)
          <input
            type="number"
            {...limits(b?.staffing_end_hour)}
            value={value.staffing_end_hour}
            onChange={(e) =>
              onChange({ staffing_end_hour: Number(e.target.value) })
            }
          />
        </label>
        <label>
          Simple staffed reps
          <input
            type="number"
            {...limits(b?.staffing_reps)}
            value={value.staffing_reps}
            onChange={(e) =>
              onChange({ staffing_reps: Number(e.target.value) })
            }
          />
        </label>
      </div>
      <fieldset className="weekday-row">
        <legend>Staffed weekdays</legend>
        {WEEKDAYS.map((day, i) => (
          <label className="check-row" key={day}>
            <input
              type="checkbox"
              checked={value.staffing_weekdays.includes(i)}
              onChange={(e) =>
                onChange({
                  staffing_weekdays: e.target.checked
                    ? [...value.staffing_weekdays, i].sort()
                    : value.staffing_weekdays.filter((v) => v !== i),
                })
              }
            />
            {day}
          </label>
        ))}
      </fieldset>
    </>
  );
}

/** Dated exceptions to the weekly schedule, entered in the same staffing zone. */
export function DatedStaffing({
  windows,
  zone,
  bounds,
  onChange,
}: {
  windows: StaffedWindow[];
  zone: string;
  bounds: Definitions | null;
  onChange: (value: StaffedWindow[]) => void;
}) {
  const { snapshot: w, notice } = useWorkspace();
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
  const instant = (local: string) => {
    try {
      return zonedInstant(local, zone);
    } catch (error) {
      notice(error instanceof Error ? error.message : String(error));
      return null;
    }
  };
  const maximum = bounds?.optimization?.staffed_windows?.max_items;
  return (
    <fieldset className="card padded">
      <legend>Dated staffing periods</legend>
      <p className="small muted">
        Periods replace the weekly schedule for planning and use the staffing
        time zone ({zone.replaceAll("_", " ")}). Exclude breaks. Name reps to
        account for promised callbacks; anonymous headcount supports unowned
        work only. Overlapping named periods count each rep once. Existing calls
        and reservations reduce availability.
      </p>
      {windows.map((window, i) => (
        <div className="optimization-fields" key={i}>
          <label>
            Roster pool (optional)
            <input
              aria-label={`Roster pool ${i + 1}`}
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
              value={zonedInput(window.start_at, zone)}
              onChange={(e) => {
                const value = e.target.value && instant(e.target.value);
                if (value) change(i, { start_at: value });
              }}
            />
          </label>
          <label>
            End
            <input
              aria-label={`Staffed end ${i + 1}`}
              type="datetime-local"
              value={zonedInput(window.end_at, zone)}
              onChange={(e) => {
                const value = e.target.value && instant(e.target.value);
                if (value) change(i, { end_at: value });
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
        disabled={maximum !== undefined && windows.length >= maximum}
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
      {maximum !== undefined && windows.length >= maximum && (
        <p className="fine">
          At most {maximum} dated periods; remove one to add another.
        </p>
      )}
      <p className="fine">
        Use the same roster pool only when headcount includes the named reps.
        Named members are subtracted from that pool, so overlapping availability
        is counted once.
      </p>
    </fieldset>
  );
}
