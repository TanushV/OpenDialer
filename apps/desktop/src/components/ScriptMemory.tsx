import {
  Fragment,
  useCallback,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { date, label, useWorkspace } from "../context";

type Section =
  | "opening"
  | "screening"
  | "gatekeeper"
  | "pitch"
  | "discovery"
  | "offers"
  | "objections"
  | "close";
interface Proposal {
  id: string;
  section: Section;
  index: number | null;
  text: string;
  rationale: string;
  source: "team" | "memory";
  status: string;
  created_at: string;
  comparison_id: string | null;
}
interface ScriptTest {
  id: string;
  status: string;
  section: string;
  text: string;
  ends_at: string;
  stopped_at?: string;
  required_per_arm: number | null;
  arms: Record<
    "control" | "candidate",
    { matured_leads: number; matured_conversions: number } | undefined
  >;
}
interface Version {
  version: number;
  source: string | null;
  adopted_at: string | null;
  template: Record<string, unknown> | null;
}
interface Lab {
  base: Version;
  history: Version[];
  settings: { auto_propose: boolean; promotion: "automatic" | "approval" };
  proposals: Proposal[];
  tests: ScriptTest[];
  editable: Section[];
  locked: Record<string, string>;
  report: { samples: number; traced_leads?: number };
  memory: {
    version: number;
    markdown: string;
    team_notes: string;
    written_by: string;
    written_at: string;
  } | null;
  memory_versions: {
    version: number;
    written_by: string;
    written_at: string;
  }[];
}

const SECTION_NAMES: Record<Section, string> = {
  opening: "Opening",
  screening: "Screening",
  gatekeeper: "Gatekeeper",
  pitch: "Pitch",
  discovery: "Discovery",
  offers: "Offers",
  objections: "Objections",
  close: "Close",
};

function inline(text: string): ReactNode[] {
  return text
    .split(/(\*\*[^*]+\*\*)/g)
    .map((part, i) =>
      part.startsWith("**") && part.endsWith("**") ? (
        <strong key={i}>{part.slice(2, -2)}</strong>
      ) : (
        <Fragment key={i}>{part}</Fragment>
      ),
    );
}

/** A small, safe renderer for the memory's Markdown: headings, bullets, emphasis. */
export function Markdown({ text }: { text: string }) {
  const blocks: ReactNode[] = [];
  let items: ReactNode[] = [];
  const flush = () => {
    if (items.length) blocks.push(<ul key={blocks.length}>{items}</ul>);
    items = [];
  };
  for (const raw of text.split("\n")) {
    const line = raw.trimEnd();
    if (!line.trim() || line.startsWith("<!--")) {
      flush();
      continue;
    }
    if (line.startsWith("## ")) {
      flush();
      blocks.push(<h4 key={blocks.length}>{line.slice(3)}</h4>);
    } else if (line.startsWith("# ")) {
      flush();
      blocks.push(<h3 key={blocks.length}>{line.slice(2)}</h3>);
    } else if (line.startsWith("- ")) {
      items.push(<li key={items.length}>{inline(line.slice(2))}</li>);
    } else if (line.startsWith("  ") && items.length) {
      items.push(
        <li key={items.length} className="fine">
          {inline(line.trim())}
        </li>,
      );
    } else {
      flush();
      const italic = line.startsWith("_") && line.endsWith("_");
      blocks.push(
        <p key={blocks.length} className={italic ? "muted small" : undefined}>
          {italic ? <em>{line.slice(1, -1)}</em> : inline(line)}
        </p>,
      );
    }
  }
  flush();
  return <div className="markdown">{blocks}</div>;
}

export function ScriptMemory() {
  const { api, run, subscribe, notice } = useWorkspace();
  const [lab, setLab] = useState<Lab | null>(null);
  const [notes, setNotes] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const load = useCallback(async () => {
    setLab(await api<Lab>("/v1/optimization/script"));
  }, [api]);
  useEffect(() => {
    void run(load);
    return subscribe((events) => {
      if (events.some((event) => event.type === "optimization.updated"))
        void run(load);
    });
  }, [load, run, subscribe]);
  if (!lab)
    return (
      <section className="card padded" aria-label="Script memory">
        <h2>Script memory</h2>
        <p>Loading the script memory…</p>
      </section>
    );
  const running = lab.tests.find((t) => !t.stopped_at);
  async function act(work: () => Promise<unknown>, message: string) {
    await run(async () => {
      await work();
      notice(message);
      await load();
    });
  }
  function download() {
    if (!lab?.memory) return;
    const url = URL.createObjectURL(
      new Blob([lab.memory.markdown], { type: "text/markdown" }),
    );
    const link = document.createElement("a");
    link.href = url;
    link.download = `script-memory-v${lab.memory.version}.md`;
    link.click();
    URL.revokeObjectURL(url);
  }
  return (
    <section className="card padded" aria-label="Script memory">
      <h2>Script memory</h2>
      <p className="muted small">
        How prospects in this campaign react to each part of the base script,
        rewritten after each learning refresh. Findings are correlations;
        changes are tested against the base before they replace it. Who is
        calling, the screening line and the proof slot stay locked.
      </p>
      <p>
        Base script v{lab.base.version} ·{" "}
        {lab.base.source === "seller_template"
          ? "the seller's template"
          : label((lab.base.source ?? "not saved yet").split(":")[0])}
        {lab.base.adopted_at ? ` · adopted ${date(lab.base.adopted_at)}` : ""}
      </p>
      <div
        className="optimization-fields"
        aria-label="Script learning settings"
      >
        <label className="check-row">
          <input
            type="checkbox"
            checked={lab.settings.auto_propose}
            onChange={(e) =>
              void act(
                () =>
                  api("/v1/admin/script/settings", "PUT", {
                    ...lab.settings,
                    auto_propose: e.target.checked,
                  }),
                "Script learning settings saved",
              )
            }
          />
          Propose and test changes from the memory
        </label>
        <label>
          When a change wins its test
          <select
            aria-label="When a change wins its test"
            value={lab.settings.promotion}
            onChange={(e) =>
              void act(
                () =>
                  api("/v1/admin/script/settings", "PUT", {
                    ...lab.settings,
                    promotion: e.target.value,
                  }),
                "Script learning settings saved",
              )
            }
          >
            <option value="automatic">Replace the base automatically</option>
            <option value="approval">Wait for my approval</option>
          </select>
        </label>
      </div>
      {lab.memory ? (
        <>
          <div className="memory-document" aria-label="Script memory document">
            <Markdown text={lab.memory.markdown} />
          </div>
          <p className="fine">
            Version {lab.memory.version} · {lab.memory.written_by} ·{" "}
            {date(lab.memory.written_at)}{" "}
            <button className="text-button" onClick={download}>
              Download .md
            </button>
          </p>
        </>
      ) : (
        <p>No memory yet. It is written after the first learning refresh.</p>
      )}
      <label>
        Team notes (never overwritten by learning)
        <textarea
          aria-label="Team notes"
          rows={4}
          value={notes ?? lab.memory?.team_notes ?? ""}
          onChange={(e) => setNotes(e.target.value)}
        />
        <button
          className="secondary"
          disabled={notes === null || notes === (lab.memory?.team_notes ?? "")}
          onClick={() =>
            void act(async () => {
              await api("/v1/admin/script/memory", "PUT", {
                expected_version: lab.memory?.version ?? 0,
                team_notes: notes,
              });
              setNotes(null);
            }, "Team notes saved")
          }
        >
          Save team notes
        </button>
      </label>
      <h3>Script tests</h3>
      {!lab.tests.length && <p className="muted small">No script test yet.</p>}
      <ul>
        {lab.tests.map((test) => {
          const a = test.arms.control;
          const b = test.arms.candidate;
          return (
            <li key={test.id}>
              <strong>{label(test.status)}</strong>:{" "}
              {SECTION_NAMES[test.section as Section] ?? test.section} “
              {test.text}” · base{" "}
              {a ? `${a.matured_conversions}/${a.matured_leads}` : "0/0"},
              candidate{" "}
              {b ? `${b.matured_conversions}/${b.matured_leads}` : "0/0"}{" "}
              matured
              {test.required_per_arm
                ? ` · about ${test.required_per_arm} per arm needed`
                : ""}
              {!test.stopped_at && ` · runs until ${date(test.ends_at)}`}
            </li>
          );
        })}
      </ul>
      <label>
        Decision reason
        <input
          aria-label="Script decision reason"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
        />
      </label>
      <h3>Script versions</h3>
      <ul>
        {lab.history.map((version) => (
          <li key={version.version}>
            v{version.version} ·{" "}
            {version.source === "seller_template"
              ? "seller template"
              : label((version.source ?? "not saved yet").split(":")[0])}
            {version.adopted_at ? ` · ${date(version.adopted_at)}` : ""}
            {version.version !== lab.base.version && (
              <button
                className="text-button"
                disabled={Boolean(running) || reason.trim().length < 3}
                onClick={() =>
                  void act(
                    () =>
                      api("/v1/admin/script/rollback", "POST", {
                        version: version.version,
                        reason,
                      }),
                    `Base script restored from v${version.version}`,
                  )
                }
              >
                Restore this version
              </button>
            )}
          </li>
        ))}
      </ul>
      <p className="fine">
        {lab.report.samples} conversations with closed outcomes analyzed
        {lab.report.traced_leads !== undefined
          ? ` · ${lab.report.traced_leads} leads with delivered script sections`
          : ""}
        . A pattern needs at least 10 leads on each side and a later period to
        confirm it.
      </p>
    </section>
  );
}
