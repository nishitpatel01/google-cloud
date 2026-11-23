/** Pure formatting, citation parsing, SSE frame parsing, and CSV-safe export utilities. */

export type CitationKind = "ENTITY" | "EDGE" | "CHUNK";

export interface AnswerSegment {
  type: "text" | "citation";
  text: string;
  kind?: CitationKind;
  id?: string;
}

const CITE_REGEX = /\[(ENTITY|EDGE|CHUNK):([A-Za-z0-9][A-Za-z0-9._:-]{1,95})\]/g;
const FORMULA_PREFIX_REGEX = /^[\s]*[=+\-@\t\r]/;

/** Parse answer prose into safe React render segments (zero HTML injection). */
export function parseCitations(text: string): AnswerSegment[] {
  const segments: AnswerSegment[] = [];
  let cursor = 0;
  let match: RegExpExecArray | null;
  CITE_REGEX.lastIndex = 0;

  while ((match = CITE_REGEX.exec(text)) !== null) {
    if (match.index > cursor) {
      segments.push({ type: "text", text: text.slice(cursor, match.index) });
    }
    segments.push({
      type: "citation",
      text: match[0],
      kind: match[1] as CitationKind,
      id: match[2],
    });
    cursor = match.index + match[0].length;
  }
  if (cursor < text.length) {
    segments.push({ type: "text", text: text.slice(cursor) });
  }
  return segments;
}

/** Escape a CSV cell value and neutralize spreadsheet formula injection (=, +, -, @, TAB, CR). */
export function escapeCsvCell(value: unknown): string {
  const raw = value === null || value === undefined ? "" : String(value);
  const safe = FORMULA_PREFIX_REGEX.test(raw) ? `'${raw}` : raw;
  const escaped = safe.replace(/"/g, '""');
  return `"${escaped}"`;
}

export interface ExecutiveBriefRow {
  metric: string;
  value: string | number;
  notes: string;
}

/** Generate a formula-safe CSV Executive Brief ready for browser download. */
export function buildExecutiveBriefCsv(rows: ExecutiveBriefRow[]): string {
  const header = ["Metric", "Value", "Notes"].map(escapeCsvCell).join(",");
  const lines = rows.map((r) =>
    [escapeCsvCell(r.metric), escapeCsvCell(r.value), escapeCsvCell(r.notes)].join(",")
  );
  return [header, ...lines].join("\r\n") + "\r\n";
}

export function formatUsd(amount: number): string {
  if (!Number.isFinite(amount)) return "$0.00";
  if (amount > 0 && amount < 0.01) {
    return `$${amount.toFixed(4)}`;
  }
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: amount >= 1000 ? 0 : 2,
  }).format(amount);
}

export function formatMs(ms: number | undefined): string {
  if (ms === undefined || !Number.isFinite(ms)) return "—";
  if (ms >= 1000) return `${(ms / 1000).toFixed(2)}s`;
  return `${Math.round(ms)}ms`;
}

export function formatPercent(ratio: number | undefined | null): string {
  if (ratio === undefined || ratio === null || !Number.isFinite(ratio)) return "—";
  return `${Math.round(ratio * 100)}%`;
}

export interface ParsedSseEvent {
  event: string;
  data: unknown;
}

/** Incrementally parse buffered SSE text into complete frames + remaining tail. */
export function parseSseBuffer(buffer: string): {
  events: ParsedSseEvent[];
  remainder: string;
} {
  const normalized = buffer.replace(/\r\n/g, "\n");
  const blocks = normalized.split("\n\n");
  const remainder = blocks.pop() ?? "";
  const events: ParsedSseEvent[] = [];

  for (const block of blocks) {
    if (!block.trim()) continue;
    let eventName = "message";
    const dataLines: string[] = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) {
        eventName = line.slice(6).trim();
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice(5).trim());
      }
    }
    if (dataLines.length > 0) {
      const joined = dataLines.join("\n");
      try {
        events.push({ event: eventName, data: JSON.parse(joined) });
      } catch {
        events.push({ event: eventName, data: joined });
      }
    }
  }
  return { events, remainder };
}
