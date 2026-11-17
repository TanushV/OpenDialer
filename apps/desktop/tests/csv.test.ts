import { expect, it } from "vitest";
import { parseCSV, parseLeadCSV } from "../src/state/csv";

it("lead CSV accepts exported headings, blank optional tail and reports broken rows", () => {
  const result = parseLeadCSV(
    "Full Name,Mobile Phone,Notes\r\nAvery,+14155550184\r\nAlex,+14155550185,Good,extra",
  );
  expect(result.rows).toEqual([
    { "Full Name": "Avery", "Mobile Phone": "+14155550184", Notes: "" },
  ]);
  expect(result.errors[0]).toMatchObject({ row: 3 });
});
it("imports quoted fields, escaped quotes, blank rows and multiline values", () => {
  const rows = parseCSV(
    'name,phone,company\r\n"Reed, Ada",+14155550101,"A ""Good"" Co"\r\n"Multi\nLine",+12125550102,Example\n\n',
  );
  expect(rows).toEqual([
    { name: "Reed, Ada", phone: "+14155550101", company: 'A "Good" Co' },
    { name: "Multi\nLine", phone: "+12125550102", company: "Example" },
  ]);
});
it("requires phone header and rejects ragged rows", () => {
  expect(() => parseCSV("name\nAda")).toThrow();
  expect(() => parseCSV("name,phone\nAda")).toThrow();
});

it("parses bounded carrier columns without requiring lead phone data", () => {
  expect(
    parseCSV(
      "provider_record_id,cost_usd,duration_seconds\nreceipt-1,0.02,61",
      ["provider_record_id", "cost_usd", "duration_seconds"],
    ),
  ).toEqual([
    {
      provider_record_id: "receipt-1",
      cost_usd: "0.02",
      duration_seconds: "61",
    },
  ]);
  expect(() =>
    parseCSV("provider_record_id,cost_usd\nr,0.1", [
      "provider_record_id",
      "duration_seconds",
    ]),
  ).toThrow();
});
