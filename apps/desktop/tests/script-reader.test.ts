import { expect, test } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { ScriptReader } from "../src/components/ScriptReader";

test("playbook instructions remain available separately from spoken options", () => {
  const html = renderToStaticMarkup(
    createElement(ScriptReader, {
      script: {
        context: {
          goal: "Earn twenty minutes",
          instructions: "Stop at a firm refusal",
        },
        sections: [
          {
            key: "opening",
            title: "Opening",
            options: [
              {
                id: "opening-1",
                title: null,
                text: "Hi Dana",
                notes: "Pause now",
                picked: true,
                rank: 0,
              },
            ],
          },
        ],
      },
    }),
  );
  expect(html).toContain("Stop at a firm refusal");
  expect(html).toContain("Playbook directions");
  expect(html).toContain("Pause now");
  expect(html).not.toContain("<p>Hi Dana\nPause now</p>");
});

test("a Markdown table in a script option renders as a table that scrolls instead of overflowing", () => {
  const html = renderToStaticMarkup(
    createElement(ScriptReader, {
      script: {
        sections: [
          {
            key: "objections",
            title: "Objections",
            options: [
              {
                id: "pricing",
                title: "Pricing",
                text: "Use the row that fits.\n\n| They say | You say |\n| --- | --- |\n| Too expensive | What would make it worth it? |\n| No budget | When does budget open? |",
                picked: true,
                rank: 0,
              },
            ],
          },
        ],
      },
    }),
  );
  expect(html).toContain('<div class="script-table">');
  expect(html).toContain("<th>They say</th>");
  expect(html).toContain("<td>When does budget open?</td>");
  expect(html).not.toContain("| --- |");
  expect(html).toContain("Use the row that fits.");
});
