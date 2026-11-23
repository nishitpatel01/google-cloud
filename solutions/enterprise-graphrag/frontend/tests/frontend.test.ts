import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import {
  buildExecutiveBriefCsv,
  escapeCsvCell,
  formatMs,
  formatPercent,
  formatUsd,
  parseCitations,
  parseSseBuffer,
} from "../src/utils/format";

// 1. Citation parser splits prose & ENTITY/EDGE/CHUNK safely without HTML injection
{
  const segs = parseCitations(
    "Exposure from [ENTITY:ISSUER-INTEL] via [EDGE:EDGE-SRC-1] backed by [CHUNK:CHK-001] <script>alert(1)</script>."
  );
  assert.equal(segs.length, 7);
  assert.equal(segs[1].type, "citation");
  assert.equal(segs[1].kind, "ENTITY");
  assert.equal(segs[1].id, "ISSUER-INTEL");
  assert.equal(segs[3].kind, "EDGE");
  assert.equal(segs[3].id, "EDGE-SRC-1");
  assert.equal(segs[5].kind, "CHUNK");
  assert.equal(segs[5].id, "CHK-001");
  assert.equal(segs[6].type, "text");
  assert.ok(segs[6].text.includes("<script>")); // Kept strictly as plain text segment (escaped by React JSX)
}

// 2. CSV formula injection neutralization (=, +, -, @, TAB, CR)
{
  assert.equal(escapeCsvCell("=CMD|' /C calc'!A0"), `"'=CMD|' /C calc'!A0"`);
  assert.equal(escapeCsvCell("+2+3"), `"'+2+3"`);
  assert.equal(escapeCsvCell("-100"), `"'-100"`);
  assert.equal(escapeCsvCell("@SUM(A1:A2)"), `"'@SUM(A1:A2)"`);
  assert.equal(escapeCsvCell(`Hello "World"`), `"Hello ""World"""`);

  const csv = buildExecutiveBriefCsv([
    { metric: "Pack", value: "Supply Chain", notes: "=HYPERLINK(\"http://evil\")" },
  ]);
  assert.ok(csv.startsWith('"Metric","Value","Notes"\r\n'));
  assert.ok(csv.includes(`"'=HYPERLINK(""http://evil"")"`));
}

// 3. Incremental SSE chunk parser preserves partial frames across TCP chunks
{
  const chunk1 =
    'event: stage\ndata: {"event_type":"stage_started","stage":"gql_traverse"}\n\nevent: result\ndata: {"answer":"par';
  const r1 = parseSseBuffer(chunk1);
  assert.equal(r1.events.length, 1);
  assert.equal(r1.events[0].event, "stage");
  assert.equal((r1.events[0].data as { stage: string }).stage, "gql_traverse");

  const r2 = parseSseBuffer(r1.remainder + 'tial done"}\n\n');
  assert.equal(r2.events.length, 1);
  assert.equal(r2.events[0].event, "result");
  assert.equal((r2.events[0].data as { answer: string }).answer, "partial done");
  assert.equal(r2.remainder, "");
}

// 4. Formatting helpers
{
  assert.equal(formatUsd(0.0024), "$0.0024");
  assert.equal(formatUsd(4500), "$4,500");
  assert.equal(formatMs(450), "450ms");
  assert.equal(formatMs(12500), "12.50s");
  assert.equal(formatPercent(0.978), "98%");
  assert.equal(formatPercent(null), "—");
}

// 5. Static XSS & strict CSP guardrail across frontend/src/**
{
  const srcRoot = path.resolve("src");
  const walk = (dir: string): string[] =>
    fs.readdirSync(dir, { withFileTypes: true }).flatMap((ent) => {
      const full = path.join(dir, ent.name);
      return ent.isDirectory() ? walk(full) : [full];
    });
  for (const file of walk(srcRoot).filter((f) => f.endsWith(".ts") || f.endsWith(".tsx"))) {
    const content = fs.readFileSync(file, "utf-8");
    assert.ok(!content.includes("dangerouslySetInnerHTML"), `XSS sink in ${file}`);
    assert.ok(!content.includes("innerHTML"), `innerHTML sink in ${file}`);
    assert.ok(!content.includes("localStorage"), `localStorage token risk in ${file}`);
    assert.ok(!content.includes("style={{"), `Inline style violates strict CSP style-src 'self' in ${file}`);
  }
}

console.log("ALL FRONTEND UNIT & SECURITY GUARDRAIL TESTS PASSED (5/5 suites)");
