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
      <p>{withWritten(option.text, written)}</p>
    </div>
  );
}

/** The script a rep reads: picked options first, other options folded away. */
export function ScriptReader({ script }: { script: Partial<Script> }) {
  const written = Object.values(script.written ?? {}).map((w) => w.text);
  const sections = script.sections ?? [];
  return (
    <div className="script-reader">
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
                  <p>{withWritten(option.text, written)}</p>
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
