import { useEffect, useState } from "react";
import { LONG_REQUEST_MS } from "../api/client";
import { date, useWorkspace } from "../context";
import { useLoad } from "../state/useLoad";
import type { Lead } from "../types";

interface ListName {
  name: string;
  paused: boolean;
  archived: boolean;
  script_mode?: "master" | "prepared";
}

export interface ListSummary extends ListName {
  total: number;
  ready: number;
  preparing: number;
  callable_now: number;
  held: number;
  completed: number;
  callbacks_due: number;
  max_attempts?: number | null;
  retry_days?: number[];
  /** When the Dialer suggests leaving a voicemail; advisory only (work package 6.12). */
  voicemail_rule?: VoicemailRule;
}

type VoicemailRule = "first_and_final" | "final" | "every" | "never";
const VOICEMAIL_RULES: [VoicemailRule, string][] = [
  ["first_and_final", "First and final attempts (default)"],
  ["final", "Final attempt only"],
  ["every", "Every attempt"],
  ["never", "Never"],
];

export interface LeadSelection {
  filter: string;
  list_name?: string;
  q: string;
  fingerprint: string;
  exclude: string[];
}
export interface ListsData {
  lists: ListSummary[];
  outside_callbacks: {
    id: string;
    name: string;
    phone: string;
    list_names: string[];
    due_at: string;
    blocked: boolean;
  }[];
}
export const listLabel = (name: string) => name || "Unnamed list";
export const leadLists = (lead: Lead) =>
  lead.list_names ?? [lead.list_name ?? ""];

export function useLists(names: string[] | null = null) {
  const { subscribe } = useWorkspace();
  const params = new URLSearchParams();
  if (names !== null) params.set("selected", "true");
  names?.forEach((name) => params.append("list_names", name));
  const state = useLoad<ListsData>(
    `/v1/lists${params.size ? `?${params}` : ""}`,
  );
  useEffect(
    () =>
      subscribe((events) => {
        if (
          events.some((event) =>
            ["lead.updated", "campaign.updated"].includes(event.type),
          )
        )
          void state.reload().catch(() => {});
      }),
    [subscribe, state.reload],
  );
  return state;
}

export function useListCatalog() {
  const { subscribe } = useWorkspace();
  const state = useLoad<{ lists: ListName[] }>("/v1/list-catalog");
  useEffect(
    () =>
      subscribe((events) => {
        if (events.some((event) => event.type === "campaign.updated"))
          void state.reload().catch(() => {});
      }),
    [state.reload, subscribe],
  );
  return state;
}

/** The lists table. On the Leads page it sits above the leads (R10.6); `onChanged` reloads the leads below. */
export function ListsPage({
  embedded = false,
  onChanged,
}: {
  embedded?: boolean;
  onChanged?: () => Promise<void>;
}) {
  const { api, run, refresh, snapshot: w, notice } = useWorkspace();
  const lists = useLists();
  const [names, setNames] = useState<Record<string, string>>({});
  const [showRemoved, setShowRemoved] = useState(false);
  const visibleLists = lists.data?.lists.filter(
    (list) => showRemoved || !list.archived,
  );
  async function edit(name: string, action: string) {
    await api("/v1/lists", "PATCH", {
      campaign_id: w.campaign.id,
      list_name: name,
      action,
      ...(action === "rename" ? { name: names[name] } : {}),
    });
    await lists.reload();
    await refresh();
    await onChanged?.();
    notice(
      action === "delete"
        ? "List removed. You can import a new list under this name."
        : action === "restore"
          ? "List restored. Contact history and opt-outs are retained."
          : "List updated. Contact history and opt-outs are retained.",
    );
  }
  return (
    <>
      {!embedded && (
        <div className="page-heading">
          <div>
            <h1>Lists</h1>
            <p>
              Remove a list here, or choose which lists to call in Dialer.
              Removing a list frees its name for a new import.
            </p>
          </div>
        </div>
      )}
      <section className="card table-card" aria-label="Lead lists">
        <div className="table-toolbar">
          <h3>Your lists</h3>
          <button
            className="secondary small"
            onClick={() => void run(lists.reload)}
          >
            Refresh lists
          </button>
        </div>
        <label className="list-visibility">
          <input
            type="checkbox"
            aria-label="Show removed lists"
            checked={showRemoved}
            onChange={(e) => setShowRemoved(e.target.checked)}
          />
          Show removed lists
        </label>
        {lists.error && <p role="alert">{lists.error}</p>}
        {!lists.data && !lists.error && <p>Loading lists…</p>}
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>LIST</th>
                <th>TOTAL</th>
                <th>SCRIPTS</th>
                <th>CALLABLE NOW</th>
                <th>HELD</th>
                <th>COMPLETED</th>
                <th>DUE CALLBACKS</th>
                <th>ACTIONS</th>
              </tr>
            </thead>
            <tbody>
              {visibleLists?.map((list) => (
                <tr key={list.name}>
                  <td>
                    <strong>{listLabel(list.name)}</strong>
                    <small>
                      {list.archived
                        ? "Removed"
                        : list.paused
                          ? "Paused"
                          : "Active"}
                    </small>
                  </td>
                  <td>{list.total}</td>
                  <td>
                    {list.script_mode === "master"
                      ? "Dial only · master script"
                      : `${list.ready} ready / ${list.preparing} preparing`}
                  </td>
                  <td>{list.callable_now}</td>
                  <td>{list.held}</td>
                  <td>{list.completed}</td>
                  <td>{list.callbacks_due}</td>
                  <td className="lead-list-actions">
                    <div className="button-row">
                      {!list.archived && (
                        <button
                          className="secondary small"
                          onClick={() =>
                            void run(() =>
                              edit(list.name, list.paused ? "resume" : "pause"),
                            )
                          }
                        >
                          {list.paused ? "Resume" : "Pause"}
                        </button>
                      )}
                      <button
                        className="secondary small"
                        onClick={() =>
                          void run(() =>
                            edit(
                              list.name,
                              list.archived ? "restore" : "delete",
                            ),
                          )
                        }
                      >
                        {list.archived ? "Restore list" : "Remove list"}
                      </button>
                      {list.archived && (
                        <button
                          className="secondary small"
                          onClick={() =>
                            void run(() => edit(list.name, "delete"))
                          }
                        >
                          Delete removed list
                        </button>
                      )}
                    </div>
                    {!list.archived && (
                      <ListDialingSettings
                        key={JSON.stringify([
                          list.script_mode,
                          list.max_attempts,
                          list.retry_days,
                          list.voicemail_rule,
                        ])}
                        list={list}
                        saved={async () => {
                          await lists.reload();
                          await refresh();
                          await onChanged?.();
                        }}
                      />
                    )}
                    <details className="list-rename">
                      <summary>Rename list</summary>
                      <input
                        aria-label={`New name for ${listLabel(list.name)}`}
                        value={names[list.name] ?? list.name}
                        onChange={(e) =>
                          setNames({ ...names, [list.name]: e.target.value })
                        }
                        maxLength={120}
                      />
                      <button
                        className="secondary small"
                        disabled={!names[list.name]?.trim()}
                        onClick={() =>
                          void run(() => edit(list.name, "rename"))
                        }
                      >
                        Save name
                      </button>
                    </details>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {visibleLists?.length === 0 && (
          <p className="lead-lists-note">
            No active lists. Import a CSV in Leads or show removed lists to
            restore one.
          </p>
        )}
        <p className="fine lead-lists-note">
          Callable now checks the current calling rules, excluding the rep's
          Ready/audio state. Counts can overlap when one contact belongs to
          several lists. Paused lists stop ordinary calls; their callbacks
          remain available for explicit review. Removing a list keeps saved call
          history and opt-outs. Contacts in another active list stay there.
        </p>
      </section>
    </>
  );
}

function ListDialingSettings({
  list,
  saved,
}: {
  list: ListSummary;
  saved: () => Promise<void>;
}) {
  const { api, run, snapshot, notice } = useWorkspace();
  const [mode, setMode] = useState(list.script_mode ?? "prepared");
  const [maximum, setMaximum] = useState(String(list.max_attempts ?? ""));
  const [waits, setWaits] = useState((list.retry_days ?? []).join(", "));
  const [voicemail, setVoicemail] = useState<VoicemailRule>(
    list.voicemail_rule ?? "first_and_final",
  );
  const delays = waits.trim()
    ? waits.split(",").map((day) => Number(day.trim()))
    : [];
  const valid =
    (!maximum ||
      (Number.isInteger(Number(maximum)) &&
        Number(maximum) >= 1 &&
        Number(maximum) <= 20)) &&
    (!delays.length ||
      (delays.length === Number(maximum) - 1 &&
        delays.every(
          (day) => Number.isInteger(day) && day >= 1 && day <= 365,
        )));
  return (
    <details className="list-dialing list-rename">
      <summary>Dialing settings</summary>
      <label>
        Script mode
        <select
          aria-label="Script mode"
          value={mode}
          onChange={(event) =>
            setMode(event.target.value === "master" ? "master" : "prepared")
          }
        >
          <option value="prepared">Require prepared scripts</option>
          <option value="master">Dial only · use master script</option>
        </select>
      </label>
      <label>
        Attempts per lead
        <input
          aria-label="List attempts per lead"
          type="number"
          min={1}
          max={20}
          placeholder="Workspace default"
          value={maximum}
          onChange={(event) => setMaximum(event.target.value)}
        />
      </label>
      <label>
        Days between attempts
        <input
          aria-label="Days between attempts"
          value={waits}
          placeholder="Workspace default"
          onChange={(event) => setWaits(event.target.value)}
        />
      </label>
      <p className="fine">
        For 3 attempts, enter 2, 3 to call on day 0, day 2 and day 5. Leave
        blank to use the default timing.
      </p>
      <label>
        Leave voicemail on
        <select
          aria-label="Leave voicemail on"
          value={voicemail}
          onChange={(event) =>
            setVoicemail(event.target.value as VoicemailRule)
          }
        >
          {VOICEMAIL_RULES.map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
      </label>
      <p className="fine">
        The Dialer suggests when to leave a message. Requested callbacks and
        people you have spoken with get one too. Nothing is enforced.
      </p>
      <button
        className="secondary small"
        disabled={!valid}
        onClick={() =>
          void run(async () => {
            await api("/v1/lists", "PATCH", {
              campaign_id: snapshot.campaign.id,
              list_name: list.name,
              action: "dialing",
              script_mode: mode,
              max_attempts: maximum ? Number(maximum) : null,
              retry_days: delays,
              voicemail_rule: voicemail,
            });
            await saved();
            notice("List dialing settings saved.");
          })
        }
      >
        Save dialing settings
      </button>
    </details>
  );
}

export function BulkLeadActions({
  selected,
  selection,
  count,
  memberships,
  filteredList,
  updated,
}: {
  selected: string[];
  selection?: LeadSelection;
  count: number;
  memberships: string[];
  filteredList: string | null;
  updated: () => Promise<void>;
}) {
  const { api, run, notice } = useWorkspace();
  const [action, setAction] = useState("pause");
  const [list, setList] = useState("");
  const [removeFrom, setRemoveFrom] = useState<string | null>(null);
  const [timezone, setTimezone] = useState("");
  const [state, setState] = useState("");
  const moving = ["move", "add"].includes(action);
  const source =
    removeFrom !== null && memberships.includes(removeFrom)
      ? removeFrom
      : filteredList !== null && memberships.includes(filteredList)
        ? filteredList
        : memberships.length === 1
          ? memberships[0]
          : null;
  return (
    <section aria-label="Selected lead actions" className="bulk-leads">
      <span>{count} selected</span>
      <label>
        Lead action
        <select
          aria-label="Lead action"
          value={action}
          onChange={(e) => setAction(e.target.value)}
        >
          <option value="pause">Pause calling</option>
          <option value="resume">Resume calling</option>
          <option value="archive">Remove leads</option>
          <option value="restore">Restore removed leads</option>
          <option value="move">Move to list</option>
          <option value="add">Add to another list</option>
          <option value="remove">Remove from list</option>
          <option value="location">Correct time zone / state</option>
        </select>
      </label>
      {action === "remove" && (
        <label>
          List to remove from
          <select
            aria-label="List to remove from"
            value={source ?? "__choose_list__"}
            onChange={(e) => setRemoveFrom(e.target.value)}
          >
            <option value="__choose_list__" disabled>
              Choose a list the selected leads belong to
            </option>
            {memberships.map((name) => (
              <option key={name} value={name}>
                {listLabel(name)}
              </option>
            ))}
          </select>
        </label>
      )}
      {moving && (
        <label>
          Destination list
          <input
            aria-label="Destination list"
            value={list}
            maxLength={120}
            onChange={(e) => setList(e.target.value)}
            placeholder="Blank means Unnamed list"
          />
        </label>
      )}
      {action === "location" && (
        <>
          <label>
            Time zone
            <input
              aria-label="Lead time zone"
              value={timezone}
              onChange={(e) => setTimezone(e.target.value)}
              placeholder="America/New_York"
            />
          </label>
          <label>
            State
            <input
              aria-label="Lead state"
              value={state}
              onChange={(e) => setState(e.target.value)}
              placeholder="NY"
            />
          </label>
        </>
      )}
      <button
        className="primary"
        disabled={
          !count ||
          (action === "remove" && source === null) ||
          (action === "location" && !timezone.trim() && !state.trim())
        }
        onClick={() =>
          void run(async () => {
            const result = await api<{ updated: number; unchanged: number }>(
              "/v1/leads/bulk",
              "POST",
              {
                ...(selection ? { selection } : { lead_ids: selected }),
                action,
                ...(moving ? { list_name: list.trim() } : {}),
                ...(action === "remove" ? { list_name: source } : {}),
                ...(action === "location"
                  ? {
                      timezone: timezone.trim() || null,
                      state: state.trim() || null,
                    }
                  : {}),
              },
              { timeoutMs: LONG_REQUEST_MS },
            );
            await updated();
            notice(
              action === "remove"
                ? `${result.updated} removed from ${listLabel(source!)}. ${result.unchanged ? `${result.unchanged} were not in that list. ` : ""}Other memberships and history are retained.`
                : action === "archive"
                  ? `${result.updated} leads removed from active views and dialing. Restore them in Removed leads.`
                  : `${result.updated} leads updated. History and opt-outs are retained.`,
            );
          })
        }
      >
        Apply to selected leads
      </button>
      <p className="fine">History and opt-outs are always kept.</p>
    </section>
  );
}

export function DialLists({
  names,
  change,
  disabled,
  callback,
}: {
  names: string[] | null;
  change: (names: string[] | null) => Promise<void>;
  disabled: boolean;
  callback: (id: string) => Promise<unknown>;
}) {
  const { run } = useWorkspace();
  const lists = useLists(names);
  const catalog = useListCatalog();
  return (
    <section className="card dial-lists" aria-label="Dial from">
      <div className="table-toolbar">
        <strong>Dial from</strong>
        <button
          className="secondary small"
          onClick={() =>
            void run(async () => {
              await catalog.reload();
              await lists.reload();
            })
          }
        >
          Refresh calling lists
        </button>
      </div>
      <div className="dial-lists-body">
        <label className="check">
          <input
            type="checkbox"
            aria-label="Dial from all active lists"
            checked={names === null}
            disabled={disabled}
            onChange={(e) =>
              void run(() => change(e.target.checked ? null : []))
            }
          />
          All active lists
        </label>
        {names !== null && (
          <div className="dial-list-choices">
            {catalog.data?.lists
              .filter((list) => !list.archived)
              .map((list) => (
                <label className="check" key={list.name}>
                  <input
                    type="checkbox"
                    aria-label={`Dial list ${listLabel(list.name)}`}
                    checked={names.includes(list.name)}
                    disabled={disabled || list.archived || list.paused}
                    onChange={(e) =>
                      void run(() =>
                        change(
                          e.target.checked
                            ? [...names, list.name]
                            : names.filter((name) => name !== list.name),
                        ),
                      )
                    }
                  />
                  {listLabel(list.name)}
                  {list.archived
                    ? " · archived"
                    : list.paused
                      ? " · paused"
                      : ""}
                </label>
              ))}
          </div>
        )}
        {!catalog.data && !catalog.error && names !== null && (
          <p>Loading calling lists…</p>
        )}
        {catalog.error && <p role="alert">{catalog.error}</p>}
        {names?.length === 0 && (
          <p role="status">Choose at least one list to dial.</p>
        )}
        {lists.error && <p role="alert">{lists.error}</p>}
        {!!lists.data?.outside_callbacks.length && (
          <details>
            <summary>
              Due callbacks outside this calling pool (
              {lists.data.outside_callbacks.length})
            </summary>
            <p>
              These are separate choices. Selecting one keeps your ordinary list
              selection unchanged.
            </p>
            {lists.data.outside_callbacks.map((lead) => (
              <div className="button-row" key={lead.id}>
                <span>
                  {lead.name} · {date(lead.due_at)}
                  {lead.blocked ? " · paused or archived" : ""}
                </span>
                <button
                  className="secondary small"
                  disabled={disabled || lead.blocked}
                  onClick={() =>
                    void run(async () => {
                      await callback(lead.id);
                    })
                  }
                >
                  Review callback for {lead.name}
                </button>
              </div>
            ))}
          </details>
        )}
        <p className="fine">
          No new outbound calls at or after 5 p.m. in the lead's local time.
          Timing, holds, opt-outs and caller-ID selection still apply.
        </p>
      </div>
    </section>
  );
}
