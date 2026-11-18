function readCSV(text: string) {
  const rows: { cells: string[]; line: number }[] = [];
  let row: string[] = [];
  let value = "";
  let quoted = false;
  let line = 1;
  let startLine = 1;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (c === '"') {
      if (quoted && text[i + 1] === '"') {
        value += '"';
        i++;
      } else quoted = !quoted;
    } else if (!quoted && c === ",") {
      row.push(value);
      value = "";
    } else if (!quoted && (c === "\n" || c === "\r")) {
      if (c === "\r" && text[i + 1] === "\n") i++;
      row.push(value);
      if (row.some((x) => x.trim())) rows.push({ cells: row, line: startLine });
      row = [];
      value = "";
      startLine = ++line;
    } else {
      if (c === "\n" || (c === "\r" && text[i + 1] !== "\n")) line++;
      value += c;
    }
  }
  if (quoted)
    throw new Error(`CSV row ${startLine} has an unclosed quoted field.`);
  row.push(value);
  if (row.some((x) => x.trim())) rows.push({ cells: row, line: startLine });
  return rows;
}

export function parseCSV(
  text: string,
  requiredHeaders: string[] = ["phone"],
): Record<string, string>[] {
  const rows = readCSV(text);
  const header =
    rows.shift()?.cells.map((x) =>
      x
        .replace(/^\uFEFF/, "")
        .trim()
        .toLowerCase(),
    ) ?? [];
  if (
    requiredHeaders.some((name) => !header.includes(name)) ||
    new Set(header).size !== header.length
  )
    throw new Error(
      `CSV needs unique headers including ${requiredHeaders.join(", ")}.`,
    );
  return rows.map(({ cells, line }) => {
    if (cells.length !== header.length)
      throw new Error(`CSV row ${line} has the wrong number of columns.`);
    return Object.fromEntries(header.map((h, j) => [h, cells[j].trim()]));
  });
}

export const leadCSVHeaders =
  "name,phone,email,company,domain,title,linkedin,headline,company description,industry,company size,state,timezone,number source,phone status,account signals,signal dates,signal links,script";

export function parseLeadCSV(text: string) {
  const input = readCSV(text);
  const headers =
    input
      .shift()
      ?.cells.map((header) => header.replace(/^\uFEFF/, "").trim()) ?? [];
  const keys = headers.map((header) =>
    header.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, ""),
  );
  if (
    !headers.length ||
    keys.some((key) => !key) ||
    new Set(keys).size !== keys.length
  )
    throw new Error("Give each CSV column a unique, nonempty header.");
  const errors: { row: number; reason: string }[] = [];
  const rows: Record<string, string>[] = [];
  const rowNumbers: number[] = [];
  for (const { cells, line } of input) {
    if (
      cells.length > headers.length &&
      cells.slice(headers.length).some((cell) => cell.trim())
    ) {
      errors.push({
        row: line,
        reason:
          "More values than header columns. Quote fields containing commas.",
      });
      continue;
    }
    rows.push(
      Object.fromEntries(
        headers.map((header, index) => [header, cells[index] ?? ""]),
      ),
    );
    rowNumbers.push(line);
  }
  return { headers, rows, rowNumbers, errors };
}

export function leadImportBatches(
  { rows, rowNumbers }: ReturnType<typeof parseLeadCSV>,
  fields: object,
) {
  if (rows.length > 10_000)
    throw new Error("Import up to 10,000 leads per CSV.");

  // The local authority applies a 5 MB limit per JSON request.
  const limit = 5_000_000;
  const encoder = new TextEncoder();
  const envelope = encoder.encode(
    JSON.stringify({ ...fields, rows: [] }),
  ).length;
  const batches: (typeof rows)[] = [];
  let batch: typeof rows = [];
  let bytes = envelope;
  for (const [index, row] of rows.entries()) {
    const size = encoder.encode(JSON.stringify(row)).length;
    if (envelope + size > limit)
      throw new Error(
        `CSV row ${rowNumbers[index]} exceeds the 5 MB import limit.`,
      );
    if (bytes + Number(batch.length > 0) + size > limit) {
      batches.push(batch);
      batch = [];
      bytes = envelope;
    }
    bytes += Number(batch.length > 0) + size;
    batch.push(row);
  }
  if (batch.length) batches.push(batch);
  return batches;
}
