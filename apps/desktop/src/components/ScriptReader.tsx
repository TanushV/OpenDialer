import { useLayoutEffect, useRef, type ReactNode } from "react";
import { Sparkles } from "lucide-react";
import type { Script, ScriptOption } from "../types";

/** Marks the lines the AI wrote for this lead, so a rep can tell them from the seller's wording. */
function withWritten(text: string, written: string[]): ReactNode {
  const pieces = written.filter((w) => w && text.includes(w));
  if (!pieces.length) return text;
  const pattern = new RegExp(
    `(${pieces.map((p) => p.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`,
  );
  return text.split(pattern).map((part, i) =>
    pieces.includes(part) ? (
      <mark className="ai-text" key={i} title="Written for this lead">
        {part}
      </mark>
    ) : (
      part
    ),
  );
}

const cells = (line: string) =>
  line
    .trim()
    .replace(/^\||\|$/g, "")
    .split("|")
    .map((cell) => cell.trim());
const isRow = (line: string) => /^\s*\|.*\|\s*$/.test(line);
const isRule = (line: string) =>
  /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/.test(line);

/** Script text with Markdown tables drawn as tables (R5.11); a wide table scrolls inside its box. */
function ScriptText({ text, written }: { text: string; written: string[] }) {
  const lines = text.split("\n");
  const blocks: ReactNode[] = [];
  let prose: string[] = [];
  const flush = () => {
    const value = prose.join("\n").trim();
    if (value)
      blocks.push(<p key={blocks.length}>{withWritten(value, written)}</p>);
    prose = [];
  };
  for (let i = 0; i < lines.length; i++) {
    if (isRow(lines[i]) && i + 1 < lines.length && isRule(lines[i + 1])) {
      flush();
      const head = cells(lines[i]);
      const body: string[][] = [];
      for (i += 2; i < lines.length && isRow(lines[i]); i++)
        body.push(cells(lines[i]));
      i--;
      blocks.push(
        <div className="script-table" key={blocks.length}>
          <table>
            <thead>
              <tr>
                {head.map((cell, c) => (
                  <th key={c}>{cell}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {body.map((row, r) => (
                <tr key={r}>
                  {row.map((cell, c) => (
                    <td key={c}>{withWritten(cell, written)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
    } else prose.push(lines[i]);
  }
  flush();
  return <>{blocks}</>;
}

function Option({
  option,
  written,
  part,
  current,
}: {
  option: ScriptOption;
  written: string[];
  part: string;
  current: boolean;
}) {
  return (
    <div
      className="script-option"
      data-script-part={part}
      aria-current={current ? "step" : undefined}
    >
      <ScriptText text={option.text} written={written} />
      {option.notes && (
        <aside className="script-cue">
          <strong>Rep directions</strong>
          <p>{option.notes}</p>
        </aside>
      )}
    </div>
  );
}

/** Keep the current spoken option in view within the script pane only. */
export function ScriptReader({
  script,
  position,
}: {
  script: Partial<Script>;
  position?: { key: string; sequence: number } | null;
}) {
  const written = Object.values(script.written ?? {}).map((w) => w.text);
  const sections = script.sections ?? [];
  const directions = Object.entries(script.context ?? {}).filter(
    ([key]) =>
      !sections.some((section) => section.key === key || section.title === key),
  );
  const reader = useRef<HTMLDivElement>(null);
  const part = position?.key;
  const sequence = position?.sequence;
  useLayoutEffect(() => {
    if (!part || sequence === undefined) return;
    const pane = reader.current?.closest<HTMLElement>(".script-card");
    const target = reader.current?.querySelector<HTMLElement>(
      '[aria-current="step"]',
    );
    if (!pane || !target) return;
    const disclosure = target.closest("details");
    if (disclosure) disclosure.open = true;
    const visible = pane.getBoundingClientRect();
    const line = target.getBoundingClientRect();
    if (line.top < visible.top + 16 || line.bottom > visible.bottom - 38)
      pane.scrollTop += line.top - visible.top - 20;
  }, [part, sequence]);
  return (
    <div ref={reader} className="script-reader">
      {directions.length > 0 && (
        <details className="script-directions">
          <summary>Playbook directions · not spoken</summary>
          {directions.map(([key, value]) => (
            <section key={key}>
              <h3>{key.replaceAll("_", " ")}</h3>
              <p>{value}</p>
            </section>
          ))}
        </details>
      )}
      {written.length > 0 && (
        <p className="fine script-legend">
          <Sparkles size={13} /> Highlighted lines were written for this lead
          from research.
        </p>
      )}
      {sections
        .filter((section) => section.key === "follow_up" && !!section.purpose)
        .map((section) => (
          <details key={section.key} className="script-directions">
            <summary>{section.title}</summary>
            <p>{section.text}</p>
          </details>
        ))}
      {sections
        .filter((section) => section.key !== "follow_up" || !section.purpose)
        .map((section, i) => {
          if (section.spoken === false)
            return (
              <details className="script-directions" key={section.key ?? i}>
                <summary>{section.title} · reference</summary>
                <ScriptText text={section.text ?? ""} written={[]} />
              </details>
            );
          if (!section.options) {
            return section.text ? (
              <section key={i}>
                <h3>{section.title}</h3>
                <p>{section.text}</p>
              </section>
            ) : null;
          }
          const hasSelection = section.options.some((option) => option.picked);
          return (
            <section key={section.key ?? i}>
              <h3>{section.title}</h3>
              {section.options.map((option, index) => {
                const key = `${section.key}:${option.id}`;
                const content = (
                  <Option
                    option={option}
                    written={written}
                    part={key}
                    current={part === key}
                  />
                );
                return section.key === "objections" ||
                  (hasSelection && !option.picked) ? (
                  <details
                    key={option.id}
                    className={
                      section.key === "objections"
                        ? "script-objection"
                        : "script-others"
                    }
                  >
                    <summary>
                      {option.title || `Alternative ${index + 1}`}
                    </summary>
                    {content}
                  </details>
                ) : (
                  <div key={option.id}>
                    {option.title && <strong>{option.title}</strong>}
                    {content}
                  </div>
                );
              })}
            </section>
          );
        })}
    </div>
  );
}
