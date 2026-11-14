import { useEffect, useRef, useState } from "react";

/** The default `# Triggers` section (section 9). Research may only claim these, inside each window; edit to taste. */
export const TRIGGERS = `# Triggers
> Why to call now. The agent may only claim these, inside each window.
- hiring_sellers: Hiring AEs, SDRs or BDRs (90 days, A)
- new_sales_leader: New sales leader (180 days, A)
- new_funding: New funding (180 days, B)
- expansion: Expansion or new market (180 days, B)
- leaner_team: Layoffs or a leaner team (180 days, B)`;
import { Check, Sparkles } from "lucide-react";
import { useWorkspace } from "../context";
import type { ScriptError, ScriptTemplate } from "../types";
import { OpenFile } from "./OpenFile";
import { readNewest, useNewestAnswer } from "../state/newest";

type Base = { version: number; template: ScriptTemplate | null };

export const STARTER = `# Opening
Hi {first_name}, this is {rep} with {seller}. {{ one sentence on why we're calling {company} now, based on research }}

# Screening
{rep} from {seller}, calling about your current process.

# Gatekeeper
Who handles this for {company}?

# Discovery
- How are you handling this today?
- What would you change about it if you could?
- Who else weighs in on a decision like this?

# Offers
## Short review
A 15-minute review of how you handle this today, with two ideas you can use either way.

# Objections
## Not interested
Fair enough. Is that because it's handled, or because now isn't the time?

## Send me an email
Happy to. What should it cover so it's worth opening?

# Close
- Would a 15-minute call later this week be useful?
`;

const PICK: Record<string, string> = {
  opening: "AI picks 1 per lead",
  gatekeeper: "AI picks 1 per lead",
  close: "AI picks 1 per lead",
  pitch: "AI picks 1 per lead",
  discovery: "AI picks up to 3 per lead",
  offers: "AI picks a lead offer and a backup",
};

/** Seller wording with the AI-written parts shown as labelled chips. */
function Wording({
  text,
  blocks,
}: {
  text: string;
  blocks: Record<string, string>;
}) {
  return (
    <>
      {text.split(/(\[\[ai-\d+\]\])/).map((part, i) => {
        const id = part.match(/^\[\[(ai-\d+)\]\]$/)?.[1];
        return id ? (
          <span className="ai-chip" key={i} title="The AI writes this per lead">
            <Sparkles size={12} /> {blocks[id]}
          </span>
        ) : (
          <span key={i}>{part}</span>
        );
      })}
    </>
  );
}

export function ScriptOutline({ template }: { template: ScriptTemplate }) {
  const blocks = Object.fromEntries(
    template.blocks.map((b) => [b.id, b.instruction]),
  );
  return (
    <div className="script-outline">
      {template.sections.map((section) =>
        section.spoken === false ? (
          <details key={section.key}>
            <summary>{section.title} · reference</summary>
            <p>{section.text}</p>
          </details>
        ) : (
          <section key={section.key}>
            <header>
              <h3>{section.title}</h3>
              <span className="fine">
                {section.options.length > 1
                  ? `${section.options.length} options · ${PICK[section.key] ?? "All named options available"}`
                  : section.key === "screening"
                    ? "Fixed, said as written"
                    : "One version"}
              </span>
            </header>
            <ul>
              {section.options.map((option) => (
                <li key={option.id}>
                  {option.title && <strong>{option.title}: </strong>}
                  <Wording text={option.text} blocks={blocks} />
                  {option.notes && (
                    <aside className="script-cue">
                      <strong>Rep directions</strong>
                      <p>{option.notes}</p>
                    </aside>
                  )}
                </li>
              ))}
            </ul>
          </section>
        ),
      )}
      {Object.entries(template.context ?? {})
        .filter(
          ([key]) =>
            !template.sections.some(
              (section) => section.key === key || section.title === key,
            ),
        )
        .map(([key, value]) => (
          <details key={key}>
            <summary>{key.replaceAll("_", " ")} · not spoken</summary>
            <p>{value}</p>
          </details>
        ))}
      {!template.sections.some(
        (section) => section.key === "facts" || section.key === "never_say",
      ) &&
        (template.facts.length > 0 || template.never_say.length > 0) && (
          <section>
            <header>
              <h3>Facts and limits</h3>
            </header>
            <p className="fine">
              {template.facts.length} fact
              {template.facts.length === 1 ? "" : "s"} the AI may use ·{" "}
              {template.never_say.length} thing
              {template.never_say.length === 1 ? "" : "s"} it never says
            </p>
          </section>
        )}
    </div>
  );
}

/** One place for the script: paste Markdown, see it parsed as you type, save. */
export function ScriptEditor() {
  const { api, run, notice, subscribe } = useWorkspace();
  const [base, setBase] = useState<Base | null>(null);
  const [loadError, setLoadError] = useState("");
  const [text, setText] = useState("");
  const [parsed, setParsed] = useState<ScriptTemplate | null>(null);
  const [errors, setErrors] = useState<ScriptError[]>([]);
  const [checking, setChecking] = useState(false);
  const [saving, setSaving] = useState(false);
  const [savedFeedback, setSavedFeedback] = useState("");
  const edited = useRef(false);
  const newest = useNewestAnswer();
  // The version the open edits started from: saving never silently replaces a newer one.
  const [startedFrom, setStartedFrom] = useState<number | null>(null);
  const load = async () => {
    try {
      await readNewest(
        newest,
        () => api<{ base: Base }>("/v1/optimization/script"),
        ({ base: result }) => {
          setBase(result);
          setLoadError("");
          if (!edited.current) {
            setText(result.template?.markdown ?? STARTER);
            setStartedFrom(result.version);
          }
        },
      );
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : String(error));
    }
  };
  useEffect(() => {
    void load();
    return subscribe((events) => {
      if (events.some((e) => e.type === "optimization.updated")) void load();
    });
  }, [api, subscribe]);
  useEffect(() => {
    if (!base || !text.trim()) return;
    let live = true;
    setChecking(true);
    const timer = window.setTimeout(() => {
      void api<{ template: ScriptTemplate | null; errors: ScriptError[] }>(
        "/v1/admin/script/import",
        "POST",
        { markdown: text, expected_version: base.version, preview: true },
      )
        .then((result) => {
          if (!live) return;
          setParsed(result.template);
          setErrors(result.errors);
        })
        .catch((error: unknown) => {
          if (live)
            setErrors([
              {
                line: 0,
                message: error instanceof Error ? error.message : String(error),
              },
            ]);
        })
        .finally(() => live && setChecking(false));
    }, 350);
    return () => {
      live = false;
      window.clearTimeout(timer);
    };
  }, [text, base?.version]);
  const saved = base?.template?.markdown ?? "";
  const unchanged = text === saved;
  const overtaken =
    edited.current &&
    startedFrom !== null &&
    base !== null &&
    base.version !== startedFrom;
  const save = () =>
    run(async () => {
      if (!base) return;
      setSaving(true);
      try {
        const result = await api<{
          refresh_required: boolean;
        }>("/v1/admin/script/import", "POST", {
          markdown: text,
          expected_version: startedFrom ?? base.version,
          preview: false,
        });
        edited.current = false;
        await load();
        const message = result.refresh_required
          ? "Script saved. To refresh personalized scripts, choose Generate scripts on a list in Leads."
          : "Script saved. Formatting-only edit; prepared scripts kept.";
        setSavedFeedback(message);
        notice(message);
      } finally {
        setSaving(false);
      }
    });
  if (!base)
    return (
      <section className="card padded" aria-label="Your script">
        <p role={loadError ? "alert" : "status"}>
          {loadError
            ? `Your script could not be loaded: ${loadError}`
            : "Loading your script…"}
        </p>
        {loadError && (
          <button className="secondary" onClick={() => void load()}>
            Try again
          </button>
        )}
      </section>
    );
  return (
    <section className="card padded script-editor" aria-label="Your script">
      <div className="section-heading">
        <div>
          <h2>Your script</h2>
          <p className="muted small">
            {base.template
              ? `Version ${base.version}. Edit below and save.`
              : "Start from the example or paste your own Markdown."}
          </p>
          {base.template && (
            <span
              className={`pill ${unchanged ? "green" : "amber"}`}
              role="status"
              aria-label="Script save state"
            >
              {unchanged
                ? `Saved · version ${base.version}`
                : "Unsaved changes: calls use version " +
                  base.version +
                  " until you save"}
            </span>
          )}
        </div>
        <p className="fine">
          Open your base calling script as a Markdown <code>.md</code> file.
        </p>
        <div className="button-row">
          <OpenFile
            label="Open calling script (.md)"
            accept=".md,.markdown,.txt,text/plain,text/markdown"
            onOpen={(file) => {
              void run(async () => {
                if (file.size > 40000)
                  throw new Error("Keep the script under 40 KB.");
                edited.current = true;
                setText(await file.text());
              });
            }}
          />
          {!/^#\s+triggers\s*$/im.test(text) && (
            <button
              className="secondary"
              onClick={() => {
                edited.current = true;
                setText(`${text.trimEnd()}\n\n${TRIGGERS}`);
              }}
            >
              Add trigger list
            </button>
          )}
          {!unchanged && base.template && (
            <button
              className="secondary"
              onClick={() => {
                edited.current = false;
                setText(base?.template?.markdown ?? STARTER);
              }}
            >
              Discard changes
            </button>
          )}
          <button
            className="primary"
            disabled={
              unchanged || checking || saving || overtaken || errors.length > 0
            }
            onClick={() => void save()}
          >
            <Check size={15} />
            {saving ? "Saving…" : "Save script"}
          </button>
        </div>
      </div>
      <p className="fine">
        Saving rewrites each lead's prepared script from this one. A call
        already under way keeps its script.
      </p>
      {savedFeedback && <p role="status">{savedFeedback}</p>}
      {overtaken && (
        <div className="alert" role="status">
          A newer version was saved while you were editing.
          <button
            onClick={() => {
              edited.current = false;
              setText(base?.template?.markdown ?? STARTER);
              setStartedFrom(base?.version ?? null);
            }}
          >
            Load the newer version
          </button>
        </div>
      )}
      <details className="format-guide">
        <summary>How to write it</summary>
        <ul>
          <li>
            Start with <code># Opening</code> and <code># Close</code>. Add{" "}
            <code># Pitch</code>, <code># Discovery</code>, <code># Goal</code>,{" "}
            <code># Instructions</code> and <code># Preparation</code> as
            useful. <code># Triggers</code> lists why-now reasons research may
            claim, each as <code>- Label (90 days, A)</code>. Screening has an
            identity default; <code># Gatekeeper</code>, <code># Offers</code>,{" "}
            <code># Objections</code>, <code># Facts</code> and{" "}
            <code># Never say</code> are optional.
          </li>
          <li>
            Use <code>##</code> for named options and objections. Prefix rep
            directions with <code>&gt;</code>; they never become spoken wording.
            Discovery bullets and existing opening/close bullets remain choices.
            Other bullets stay checklists. Familiar headings such as Opener and
            Problem and outcome work too.
          </li>
          <li>
            <code>{"{{ instruction }}"}</code> is text the AI writes for each
            lead from research, for example{" "}
            <code>{"{{ why we're calling them now }}"}</code>.
          </li>
          <li>
            Fields: <code>{"{first_name}"}</code>, <code>{"{name}"}</code>,{" "}
            <code>{"{company}"}</code>, <code>{"{rep}"}</code>,{" "}
            <code>{"{seller}"}</code>. Every opening says who is calling, and
            Screening names your business.
          </li>
        </ul>
      </details>
      <div className="script-columns">
        <label className="script-source">
          <span className="sr-only">Script Markdown</span>
          <textarea
            aria-label="Script Markdown"
            spellCheck={false}
            value={text}
            onChange={(e) => {
              edited.current = true;
              setText(e.target.value);
            }}
          />
        </label>
        <div className="script-parsed" aria-live="polite">
          {errors.length > 0 ? (
            <div className="script-errors" role="alert">
              <strong>
                {errors.length} thing{errors.length === 1 ? "" : "s"} to fix
              </strong>
              <ul>
                {errors.map((e, i) => (
                  <li key={i}>
                    {e.line ? (
                      <span className="line">Line {e.line}</span>
                    ) : null}
                    {e.message}
                  </li>
                ))}
              </ul>
            </div>
          ) : parsed ? (
            <>
              <p className="fine status-line">
                <Check size={13} />{" "}
                {checking
                  ? "Checking…"
                  : unchanged
                    ? "Saved"
                    : "Looks good. Save when ready."}
              </p>
              <ScriptOutline template={parsed} />
            </>
          ) : (
            <p className="muted">Checking…</p>
          )}
        </div>
      </div>
    </section>
  );
}
