import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { formatErRebuild } from "../src/erRebuildRender.js";

const here = dirname(fileURLToPath(import.meta.url));

describe("infona er rebuild printout", () => {
  it("cliOnto.ts formats via formatErRebuild", () => {
    const src = readFileSync(join(here, "../src/cliOnto.ts"), "utf8");
    expect(src).toContain("formatErRebuild");
    expect(src).toContain("erRebuildRender");
  });

  it("prints winner, reason, provenance, and a leftover conflict", () => {
    const out = formatErRebuild(
      {
        types: [
          {
            type: "Supplier",
            entities_before: 6,
            entities_after: 3,
            fragments_absorbed: 3,
            clusters_merged: 2,
          },
        ],
        fragments_absorbed_total: 3,
        merges: [
          {
            winner: "https://graph.infona.ai/entities/Supplier/ERP-1001",
            losers: [
              "https://graph.infona.ai/entities/Supplier/CRM-4402",
              "https://graph.infona.ai/entities/Supplier/DIR-8891",
            ],
            reason: "signal-richest",
            score: 1,
            provenance: {
              source: "erp",
              observed_at: "2026-03-01T12:00:00+00:00",
              authority: "source_of_truth",
            },
          },
        ],
        conflicts: [
          {
            field: "headquarters",
            entity: "https://graph.infona.ai/entities/Supplier/ERP-1001",
            winner: {
              value: "Austin",
              source: "erp",
              authority: "source_of_truth",
              observed_at: "2026-03-01T12:00:00+00:00",
            },
            loser: {
              value: "San Francisco",
              source: "directory",
              authority: "supplementary",
              observed_at: "2024-06-01T00:00:00+00:00",
            },
            reason: "authority",
          },
        ],
        unresolved: [
          {
            field: "credit_rating",
            entity: "https://graph.infona.ai/entities/Supplier/ERP-1001",
            values: [
              {
                value: "A",
                source: "erp",
                authority: "source_of_truth",
                observed_at: "2026-03-01T12:00:00+00:00",
              },
              {
                value: "BBB",
                source: "crm",
                authority: "source_of_truth",
                observed_at: "2026-03-01T12:00:00+00:00",
              },
            ],
            flagged: "equal-trust sources — not silently guessed",
          },
        ],
      },
      "suppliers",
    );
    expect(out).toContain("winner:");
    expect(out).toContain("reason:     signal-richest");
    expect(out).toContain("provenance: erp @ 2026-03-01T12:00:00+00:00 (source_of_truth)");
    expect(out).toContain("unresolved  credit_rating");
    expect(out).toContain("flagged: equal-trust sources — not silently guessed");
    expect(out).toContain("Done. 3 fragments absorbed.");
  });

  it("still prints counts when the server omits merge extras", () => {
    const out = formatErRebuild(
      {
        types: [
          {
            type: "Supplier",
            entities_before: 2,
            entities_after: 2,
            fragments_absorbed: 0,
            clusters_merged: 0,
          },
        ],
        fragments_absorbed_total: 0,
      },
      "suppliers",
    );
    expect(out).toBe(
      [
        "Rebuilding entity resolution for suppliers…",
        "  Supplier         2 → 2  (−0 fragments across 0 clusters)",
        "",
        "Done. 0 fragments absorbed.",
        "",
      ].join("\n"),
    );
  });
});
