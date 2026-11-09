import { useState } from "react";
import { useWorkspace } from "../context";
import { Button, Field } from "../ui/kit";

interface SyncResult {
  added: number;
  updated: number;
  removed: number;
  opted_out: number;
  recently_contacted: number;
  other_owner: number;
}

/**
 * HubSpot picks who is called (section 9B): choose a list, set what its number checks may spend, then sync. Calling
 * never waits on HubSpot; Start calling syncs again, and the list syncs every 30 minutes while you call.
 */
export function HubSpotLists({ onSynced }: { onSynced: () => Promise<void> }) {
  const { api, run, notice } = useWorkspace();
  const [lists, setLists] = useState<{ id: string; name: string }[] | null>(
    null,
  );
  const [listId, setListId] = useState("");
  const [limit, setLimit] = useState("10");
  const [result, setResult] = useState<SyncResult | null>(null);
  const chosen = lists?.find((l) => l.id === listId);
  return (
    <section className="card import-card" aria-label="HubSpot list">
      <div>
        <h2>Sync a HubSpot list</h2>
        <p className="muted">
          Only contacts owned by the HubSpot user this computer calls for are
          synced. Numbers are checked (about 6¢ a contact) and each number gets
          a do-not-call check before it can be dialed.
        </p>
      </div>
      <div>
        {lists === null ? (
          <Button
            onClick={() =>
              void run(async () => {
                const found = await api<{
                  lists: { id: string; name: string }[];
                }>("/v1/hubspot/lists");
                setLists(found.lists);
                setListId(found.lists[0]?.id ?? "");
              })
            }
          >
            Choose a HubSpot list
          </Button>
        ) : (
          <>
            <Field label="HubSpot list">
              <select
                value={listId}
                onChange={(e) => setListId(e.target.value)}
              >
                {lists.map((l) => (
                  <option key={l.id} value={l.id}>
                    {l.name}
                  </option>
                ))}
              </select>
            </Field>
            <Field
              label="Spend limit for number checks (USD)"
              hint={`About ${Math.floor(Number(limit) / 0.06) || 0} contacts at 6¢ each`}
            >
              <input
                inputMode="decimal"
                value={limit}
                onChange={(e) => setLimit(e.target.value)}
              />
            </Field>
            <div className="button-row">
              <Button
                variant="primary"
                disabled={!chosen || !(Number(limit) >= 0)}
                onClick={() =>
                  void run(async () => {
                    await api("/v1/lists/spend-limit", "PUT", {
                      list_name: chosen!.name,
                      usd: Number(limit),
                    });
                    const synced = await api<SyncResult>(
                      "/v1/hubspot/sync",
                      "POST",
                      { list_id: chosen!.id, list_name: chosen!.name },
                    );
                    setResult(synced);
                    await onSynced();
                    notice(`${chosen!.name} synced.`);
                  })
                }
              >
                Sync
              </Button>
            </div>
          </>
        )}
        {result && (
          <p className="small" role="status">
            {result.added} added · {result.updated} updated · {result.removed}{" "}
            removed · {result.opted_out} opted out · {result.recently_contacted}{" "}
            reached recently by another dialer · {result.other_owner} owned by
            other reps
          </p>
        )}
      </div>
    </section>
  );
}
