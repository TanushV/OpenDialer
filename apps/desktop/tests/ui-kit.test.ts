import { readFileSync } from "node:fs";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { Button, Check, Panel, Status, Table } from "../src/ui/kit";

describe("the UI kit (R5.1, rule 9)", () => {
  it("styles only its own classes, never bare elements", () => {
    const css = readFileSync(
      new URL("../src/ui/kit.css", import.meta.url),
      "utf8",
    )
      .replace(/\/\*[\s\S]*?\*\//g, "")
      .replace(/\{[^}]*\}/g, "{}");
    const selectors = css
      .split("{}")
      .flatMap((block) => block.split(","))
      .map((s) => s.trim())
      .filter(Boolean);
    for (const selector of selectors)
      expect(selector).toMatch(/^(:root|\.[a-z-]+)/);
  });
  it("renders a button with its key, a one-line checkbox, a status, a panel and a table", () => {
    const html = renderToStaticMarkup(
      createElement("div", null, [
        createElement(
          Button,
          { key: 1, variant: "primary", kbd: "Space" },
          "Start calling",
        ),
        createElement(Check, {
          key: 2,
          label: "All lists",
          checked: true,
          onChange: () => {},
        }),
        createElement(
          Status,
          { key: 3, tone: "good", label: "Telnyx" },
          "Checked",
        ),
        createElement(Panel, { key: 4, title: "Numbers" }, "x"),
        createElement(Table, {
          key: 5,
          columns: ["Phone"],
          rows: [["+14155550100"]],
        }),
      ]),
    );
    expect(html).toContain('class="ui-button primary"');
    expect(html).toContain('<kbd class="ui-kbd">Space</kbd>');
    expect(html).toContain('class="ui-check"');
    expect(html).toContain('role="status" aria-label="Telnyx"');
    expect(html).toContain('<h2 class="ui-panel-title">Numbers</h2>');
    expect(html).toContain('<td class="ui-cell">+14155550100</td>');
  });
});
