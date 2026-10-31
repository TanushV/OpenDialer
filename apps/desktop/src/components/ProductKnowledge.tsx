import { useState } from "react";
import { useWorkspace } from "../context";
import type { Playbook, Workspace } from "../types";

function fields(book: Playbook | null) {
  return {
    id: book?.id ?? null,
    version: book?.version ?? 0,
    name: book?.name || "Business introduction",
    opener: book?.opener || "",
    intro: book?.screening_intro || "",
    knowledge: book?.product_description || "",
    claims: (book?.claims || []).join("\n"),
    limits: (book?.prohibited_claims || []).join("\n"),
    cards: book?.cards || [],
    reason: "",
  };
}

export function ProductKnowledge() {
  const { snapshot: w, api, run, refresh, notice, session } = useWorkspace();
  const book = w.active_playbook ?? null;
  const [draft, setDraft] = useState(() => fields(book));
  const [saving, setSaving] = useState(false);
  const changed =
    draft.id !== (book?.id ?? null) || draft.version !== (book?.version ?? 0);
  const admin = ["owner", "admin"].includes(session.role);
  async function reload() {
    setSaving(true);
    try {
      const current = await api<Workspace>("/v1/workspace");
      setDraft(fields(current.active_playbook ?? null));
      await refresh();
    } finally {
      setSaving(false);
    }
  }
  async function save() {
    setSaving(true);
    try {
      const lines = (text: string) =>
        text
          .split("\n")
          .map((s) => s.trim())
          .filter(Boolean);
      const saved = await api<Playbook>("/v1/admin/playbooks", "POST", {
        name: draft.name,
        opener: draft.opener,
        screening_intro: draft.intro,
        product_description: draft.knowledge,
        claims: lines(draft.claims),
        prohibited_claims: lines(draft.limits),
        cards: draft.cards,
        reason: "Edited on the Script page",
        // An administrator's explicit publication is the approval decision; it is audited.
        approved: true,
        expected_previous_id: draft.id,
        expected_previous_version: draft.version,
      });
      setDraft(fields(saved));
      await refresh();
      notice("Saved. The live helper uses it from the next suggestion.");
    } finally {
      setSaving(false);
    }
  }
  return (
    <section className="card padded" aria-label="Business knowledge">
      <p className="small muted">
        What you sell, prices and limits, in your own words. The live helper
        uses it. Facts you can state and things never to say come from your
        script's <code># Facts</code> and <code># Never say</code> sections.
      </p>
      {changed && (
        <p role="status">
          A newer version was saved. Reload it before saving yours.
        </p>
      )}
      <fieldset disabled={!admin || saving}>
        <label>
          What you sell
          <textarea
            aria-label="Product and pricing knowledge"
            value={draft.knowledge}
            maxLength={12000}
            rows={6}
            onChange={(e) => setDraft({ ...draft, knowledge: e.target.value })}
          />
        </label>
        <button
          className="primary"
          disabled={changed || saving}
          onClick={() => void run(save)}
        >
          Save
        </button>
      </fieldset>
      <button
        className="text-button"
        disabled={saving}
        onClick={() => void run(reload)}
      >
        Reload current knowledge
      </button>
      {!admin && (
        <p className="fine">
          An administrator publishes changes to business knowledge.
        </p>
      )}
    </section>
  );
}
