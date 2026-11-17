import { useEffect, useState } from "react";
import { useWorkspace } from "../context";
import { Select } from "../ui/kit";

export type ColumnMapping = Record<
  string,
  { column: string; delimiter: string | null }
>;

const ROLE_NAMES: Record<string, string> = {
  phone: "Dialing number",
  name: "Full name",
  first_name: "First name",
  last_name: "Last name",
  company: "Company name",
  domain: "Company website",
  state: "Prospect state",
  timezone: "Time zone",
  title: "Title",
  headline: "Headline",
  company_description: "Company description",
  industry: "Industry",
  size: "Company size",
  number_source: "Number source",
  live_status: "Live status (DEAD is never dialed)",
  signal_summaries: "Signal summaries",
  signal_dates: "Signal dates",
  signal_urls: "Signal links",
  linkedin_activity: "LinkedIn activity",
};
const MULTI = new Set(["signal_summaries", "signal_dates", "signal_urls"]);

/** Which column plays each role, guessed from the headers and correctable before import (section 9). */
export function ColumnMap({
  headers,
  value,
  sample,
  onChange,
}: {
  headers: string[];
  value: ColumnMapping | null;
  sample?: Record<string, string>;
  onChange: (map: ColumnMapping) => void;
}) {
  const { api, run } = useWorkspace();
  const [roles, setRoles] = useState<string[]>([]);
  const key = headers.join("\u0000");
  useEffect(() => {
    if (!headers.length) return;
    let live = true;
    void run(async () => {
      const guess = await api<{ map: ColumnMapping; roles: string[] }>(
        "/v1/leads/column-map",
        "POST",
        { headers },
      );
      if (!live) return;
      setRoles(guess.roles);
      onChange(guess.map);
    });
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  if (!headers.length || !value) return null;
  const options: [string, string][] = [
    ["", "No column mapped"],
    ...headers.map((h): [string, string] => [h, h]),
  ];
  const mapped = roles.filter((role) => value[role]);
  const unmapped = roles.filter((role) => !value[role]);
  const example = (role: string) =>
    sample?.[value[role]?.column ?? ""]?.trim() ?? "";
  const sampleName =
    example("name") ||
    [example("first_name"), example("last_name")].filter(Boolean).join(" ");
  const field = (role: string) => (
    <div key={role} className="column-map-row">
      <Select
        label={ROLE_NAMES[role] ?? role}
        options={options}
        value={value[role]?.column ?? ""}
        onChange={(e) => {
          const next = { ...value };
          if (e.target.value)
            next[role] = {
              column: e.target.value,
              delimiter:
                value[role]?.delimiter ??
                (MULTI.has(role)
                  ? role === "signal_summaries"
                    ? "|"
                    : ";"
                  : null),
            };
          else delete next[role];
          onChange(next);
        }}
      />
      {MULTI.has(role) && value[role] && (
        <label className="delimiter">
          Split on
          <input
            aria-label={`${ROLE_NAMES[role]} separator`}
            value={value[role].delimiter ?? ""}
            maxLength={3}
            onChange={(e) =>
              onChange({
                ...value,
                [role]: { ...value[role], delimiter: e.target.value },
              })
            }
          />
        </label>
      )}
    </div>
  );
  return (
    <div className="column-map" role="group" aria-label="Column map">
      <p className="small muted">
        These mappings tell OpenDialer how to dial and interpret the row.
        Research and scriptwriting can use mapped and unmapped columns.
      </p>
      {sample && (
        <p className="small">
          First row · {sampleName || "No name"} ·{" "}
          {example("phone") || "No dialing number mapped"}
          {example("company") && ` · ${example("company")}`}
          {example("live_status") && ` · ${example("live_status")}`}
        </p>
      )}
      {mapped.map(field)}
      {unmapped.length > 0 && (
        <details>
          <summary>Map other fields</summary>
          {unmapped.map(field)}
        </details>
      )}
    </div>
  );
}
