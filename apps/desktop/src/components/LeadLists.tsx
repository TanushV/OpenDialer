import { useEffect, useState } from "react";
import { isAdmin, date, useWorkspace } from "../context";
import { useLoad } from "../state/useLoad";
import type { Lead } from "../types";

export interface ListSummary {
  name: string;
  paused: boolean;
  archived: boolean;
  total: number;
  ready: number;
  preparing: number;
  callable_now: number;
  held: number;
  completed: number;
  callbacks_due: number;
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

export function ListsPage() {
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
    notice(
      action === "archive"
        ? "List removed from active views and dialing. Restore it using Show removed lists. Contacts in other active lists remain."
        : action === "restore"
          ? "List restored. Contact history and opt-outs are retained."
          : "List updated. Contact history and opt-outs are retained.",
    );
  }
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>Lists</h1>
          <p>
            Remove a whole list here, or choose which lists to call in Dialer.
            Removed lists can be restored with their history.
          </p>
        </div>
      </div>
      <section className="card table-card" aria-label="Lead lists">
        <div className="table-toolbar">
          <h3>Imported lists</h3>
          <button onClick={() => void run(lists.reload)}>Refresh lists</button>
        </div>
        <label className="check">
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
                <th>READY / PREPARING</th>
                <th>CALLABLE NOW</th>
                <th>HELD</th>
                <th>COMPLETED</th>
                <th>DUE CALLBACKS</th>
                {isAdmin(w.user.role) && <th>ACTIONS</th>}
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
                    {list.ready} / {list.preparing}
                  </td>
                  <td>{list.callable_now}</td>
                  <td>{list.held}</td>
                  <td>{list.completed}</td>
                  <td>{list.callbacks_due}</td>
                  {isAdmin(w.user.role) && (
                    <td>
                      <div className="button-row">
                        {!list.archived && (
                          <button
                            onClick={() =>
                              void run(() =>
                                edit(
                                  list.name,
                                  list.paused ? "resume" : "pause",
                                ),
                              )
                            }
                          >
                            {list.paused ? "Resume" : "Pause"}
                          </button>
                        )}
                        <button
                          onClick={() =>
                            void run(() =>
                              edit(
                                list.name,
                                list.archived ? "restore" : "archive",
                              ),
                            )
                          }
                        >
                          {list.archived ? "Restore list" : "Remove list"}
                        </button>
                      </div>
                      <details>
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
                          disabled={!names[list.name]?.trim()}
                          onClick={() =>
                            void run(() => edit(list.name, "rename"))
                          }
                        >
                          Save name
                        </button>
                      </details>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {visibleLists?.length === 0 && (
          <p>
            No active lists. Import a CSV in Leads or show removed lists to
            restore one.
          </p>
        )}
        <p className="fine">
          Callable now checks the current calling rules, excluding the rep's
          Ready/audio state. Counts can overlap when one contact belongs to
          several lists. Paused lists stop ordinary calls; their callbacks
          remain available for explicit review. Removed lists stop outbound from
          that membership. Removing a list keeps history and opt-outs; contacts
          shared with another active list remain available there.
        </p>
      </section>
    </>
  );
}

export function BulkLeadActions({
  selected,
  memberships,
  filteredList,
  updated,
}: {
  selected: string[];
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
      <span>{selected.length} selected</span>
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
        disabled={
          !selected.length ||
          (action === "remove" && source === null) ||
          (action === "location" && !timezone.trim() && !state.trim())
        }
        onClick={() =>
          void run(async () => {
            const result = await api<{ updated: number; unchanged: number }>(
              "/v1/leads/bulk",
              "POST",
              {
                lead_ids: selected,
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
      <p className="fine">
        Select the checkboxes beside the leads first. Remove from list changes
        only that membership; Remove leads hides contacts from active views and
        calling. To remove an entire list, use Lists. History and opt-outs are
        retained.
      </p>
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
  return (
    <section className="card dial-lists" aria-label="Dial from">
      <div className="table-toolbar">
        <strong>Dial from</strong>
        <button onClick={() => void run(lists.reload)}>
          Refresh calling lists
        </button>
      </div>
      <label className="check">
        <input
          type="checkbox"
          aria-label="Dial from all active lists"
          checked={names === null}
          disabled={disabled}
          onChange={(e) => void run(() => change(e.target.checked ? null : []))}
        />
        All active lists
      </label>
      {names !== null && (
        <div className="button-row">
          {lists.data?.lists
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
                {list.archived ? " · archived" : list.paused ? " · paused" : ""}
              </label>
            ))}
        </div>
      )}
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
    </section>
  );
}
