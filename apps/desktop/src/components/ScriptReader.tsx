import type { ReactNode } from "react";
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
}: {
  option: ScriptOption;
  written: string[];
}) {
  return (
    <div className="script-option">
      {option.title && <strong>{option.title}</strong>}
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

/** The script a rep reads: picked options first, other options folded away. */
export function ScriptReader({ script }: { script: Partial<Script> }) {
  const written = Object.values(script.written ?? {}).map((w) => w.text);
  const sections = script.sections ?? [];
  return (
    <div className="script-reader">
      {Object.keys(script.context ?? {}).length > 0 && (
        <details className="script-directions">
          <summary>Playbook directions · not spoken</summary>
          {Object.entries(script.context ?? {}).map(([key, value]) => (
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
      {sections.map((section, i) => {
        if (!section.options) {
          return section.text ? (
            <section key={i}>
              <h3>{section.title}</h3>
              <p>{section.text}</p>
            </section>
          ) : null;
        }
        const picked = section.options.filter((o) => o.picked);
        const others = section.options.filter((o) => !o.picked);
        if (section.key === "objections")
          return (
            <section key={section.key}>
              <h3>{section.title}</h3>
              {section.options.map((option) => (
                <details key={option.id} className="script-objection">
                  <summary>{option.title}</summary>
                  <ScriptText text={option.text} written={written} />
                  {option.notes && (
                    <aside className="script-cue">
                      <strong>Rep directions</strong>
                      <p>{option.notes}</p>
                    </aside>
                  )}
                </details>
              ))}
            </section>
          );
        return (
          <section key={section.key ?? i}>
            <h3>{section.title}</h3>
            {(picked.length ? picked : section.options).map((option) => (
              <Option key={option.id} option={option} written={written} />
            ))}
            {picked.length > 0 && others.length > 0 && (
              <details className="script-others">
                <summary>
                  {others.length} other option{others.length === 1 ? "" : "s"}
                </summary>
                {others.map((option) => (
                  <Option key={option.id} option={option} written={written} />
                ))}
              </details>
            )}
          </section>
        );
      })}
    </div>
  );
}
