import { useEffect, useState } from "react";
import { useWorkspace } from "../context";
import { Select } from "../ui/kit";

export type ColumnMapping = Record<
  string,
  { column: string; delimiter: string | null }
>;

const ROLE_NAMES: Record<string, string> = {
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
  onChange,
}: {
  headers: string[];
  value: ColumnMapping | null;
  onChange: (map: ColumnMapping) => void;
}) {
  const { api } = useWorkspace();
  const [roles, setRoles] = useState<string[]>([]);
  const key = headers.join("\u0000");
  useEffect(() => {
    if (!headers.length) return;
    let live = true;
    void api<{ map: ColumnMapping; roles: string[] }>(
      "/v1/leads/column-map",
      "POST",
      { headers },
    ).then((guess) => {
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
    ["", "Not used"],
    ...headers.map((h): [string, string] => [h, h]),
  ];
  return (
    <div className="column-map" role="group" aria-label="Column map">
      <p className="small muted">
        Columns the research agent reads. Email, IDs and other bookkeeping
        columns never reach it.
      </p>
      {roles.map((role) => (
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
      ))}
    </div>
  );
}
