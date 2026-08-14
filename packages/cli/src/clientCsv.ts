/** CSV parse + schema-sample helpers for Client.ingest. */
/** Rows sent to schema inference. Profile fidelity = decision quality, so we
 *  send the whole file up to this cap, evenly strided across it (never the
 *  head — head-of-file bias is exactly what evidence-grounded inference fixes).
 *  Matches the Explorer's SCHEMA_SAMPLE_CAP. */
export const SCHEMA_SAMPLE_CAP = 5000;

export function stridedSample<T>(rows: T[], cap: number = SCHEMA_SAMPLE_CAP): T[] {
  if (rows.length <= cap) return rows;
  const out: T[] = [];
  for (let i = 0; i < cap; i++) out.push(rows[Math.floor((i * rows.length) / cap)]!);
  return out;
}
/**
 * Parse a CSV string into an array of row objects.
 *
 * Minimal RFC-4180-ish parser: handles quoted fields with commas, escaped
 * quotes (`""`), CRLF/LF line endings. Does not handle BOM stripping or
 * encoding detection — we assume UTF-8 text in.
 */
export function parseCsv(content: string): Record<string, string>[] {
  const rows: string[][] = [];
  let cur: string[] = [];
  let field = "";
  let inQuotes = false;

  for (let i = 0; i < content.length; i++) {
    const ch = content[i];
    if (inQuotes) {
      if (ch === '"') {
        if (content[i + 1] === '"') {
          field += '"';
          i++;
        } else {
          inQuotes = false;
        }
      } else {
        field += ch;
      }
    } else {
      if (ch === '"') {
        inQuotes = true;
      } else if (ch === ",") {
        cur.push(field);
        field = "";
      } else if (ch === "\n") {
        cur.push(field);
        rows.push(cur);
        cur = [];
        field = "";
      } else if (ch === "\r") {
        // swallow; handled by the following \n in CRLF, or treat lone \r as line end
        if (content[i + 1] !== "\n") {
          cur.push(field);
          rows.push(cur);
          cur = [];
          field = "";
        }
      } else {
        field += ch;
      }
    }
  }
  // flush trailing field/row
  if (field.length > 0 || cur.length > 0) {
    cur.push(field);
    rows.push(cur);
  }

  if (rows.length === 0) return [];
  const headers = rows[0]!.map((h) => h.trim());
  const out: Record<string, string>[] = [];
  for (let r = 1; r < rows.length; r++) {
    const row = rows[r]!;
    // skip blank trailing lines
    if (row.length === 1 && row[0] === "") continue;
    const obj: Record<string, string> = {};
    for (let c = 0; c < headers.length; c++) {
      obj[headers[c]!] = row[c] ?? "";
    }
    out.push(obj);
  }
  return out;
}
