import type { Script } from "../types";
import { Markdown } from "./ScriptMemory";

export function ScriptReader({ script }: { script: Partial<Script> }) {
  const sections = script.sections?.length
    ? script.sections
    : [
        {
          title: "Opening",
          text:
            script.opener ||
            "Introduce yourself and your business, then ask whether now is a good time.",
        },
        {
          title: "Discovery",
          text: (script.discovery_questions || []).join("\n\n"),
        },
        { title: "Gatekeeper", text: script.gatekeeper || "" },
        { title: "Screening", text: script.screening_intro || "" },
        { title: "Proof", text: script.proof || "" },
        { title: "Next step", text: script.closing_question || "" },
      ];
  return (
    <div className="script-reader">
      {sections
        .filter((s) => s.text)
        .map((s, i) => (
          <section key={i}>
            <h3>{s.title}</h3>
            <Markdown text={s.text} />
          </section>
        ))}
      {script.objection_cards?.map((card) => (
        <details key={card.id}>
          <summary>{card.tags.join(" · ") || "Objection"}</summary>
          <Markdown text={card.text} />
        </details>
      ))}
    </div>
  );
}
