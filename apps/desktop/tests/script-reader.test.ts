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
