export function parseCSV(
  text: string,
  requiredHeaders: string[] = ["phone"],
): Record<string, string>[] {
  const rows: string[][] = [];
  let row: string[] = [];
  let value = "";
  let quoted = false;
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
      if (row.some((x) => x.trim())) rows.push(row);
      row = [];
      value = "";
    } else value += c;
  }
  if (quoted) throw new Error("CSV has an unclosed quoted field.");
  row.push(value);
  if (row.some((x) => x.trim())) rows.push(row);
  const header =
    rows.shift()?.map((x) =>
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
  return rows.map((r, i) => {
    if (r.length !== header.length)
      throw new Error(`CSV row ${i + 2} has the wrong number of columns.`);
    return Object.fromEntries(header.map((h, j) => [h, r[j].trim()]));
  });
}
