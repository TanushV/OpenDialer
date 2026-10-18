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
        reason: draft.reason,
        approved: true,
        expected_previous_id: draft.id,
        expected_previous_version: draft.version,
      });
      setDraft(fields(saved));
      await refresh();
      notice(
        "New playbook version published. Active call scripts stay stable.",
      );
    } finally {
      setSaving(false);
    }
  }
  return (
    <section className="card padded" aria-label="Business knowledge">
      <span className="eyebrow">APPROVED BUSINESS KNOWLEDGE</span>
      <h2>{draft.name}</h2>
      <p className="small muted">
        Describe what you sell, supported capabilities, pricing and limits. The
        helper uses this context to compose its own guidance.
      </p>
      {changed && (
        <p role="status">
          A newer version is available. Reload and review it before publishing.
        </p>
      )}
      <fieldset disabled={!admin || saving}>
        <label>
          Product and pricing knowledge
          <textarea
            aria-label="Product and pricing knowledge"
            value={draft.knowledge}
            maxLength={12000}
            rows={6}
            onChange={(e) => setDraft({ ...draft, knowledge: e.target.value })}
          />
        </label>
        <label>
          Approved factual claims
          <textarea
            aria-label="Approved factual claims"
            value={draft.claims}
            rows={4}
            onChange={(e) => setDraft({ ...draft, claims: e.target.value })}
          />
        </label>
        <p className="fine">
          One reviewed fact per line, up to 32. Include any conditions on prices
          or claims.
        </p>
        <label>
          Limits and prohibited claims
          <textarea
            aria-label="Limits and prohibited claims"
            value={draft.limits}
            rows={3}
            onChange={(e) => setDraft({ ...draft, limits: e.target.value })}
          />
        </label>
        <label>
          Opener
          <textarea
            aria-label="Opener"
            value={draft.opener}
            rows={3}
            maxLength={600}
            onChange={(e) => setDraft({ ...draft, opener: e.target.value })}
          />
        </label>
        <label>
          Screening introduction
          <textarea
            aria-label="Screening introduction"
            value={draft.intro}
            rows={3}
            maxLength={400}
            onChange={(e) => setDraft({ ...draft, intro: e.target.value })}
          />
        </label>
        <label>
          Publication reason
          <input
            aria-label="Publication reason"
            value={draft.reason}
            maxLength={1200}
            onChange={(e) => setDraft({ ...draft, reason: e.target.value })}
          />
        </label>
        <button
          className="primary"
          disabled={!draft.reason.trim() || changed || saving}
          onClick={() => void run(save)}
        >
          Publish reviewed version
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
