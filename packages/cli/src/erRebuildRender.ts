/** Format `infona er rebuild` — winner, why, provenance, leftover conflict. */

export type ErClaim = {
  value?: unknown;
  source?: unknown;
  authority?: unknown;
  observed_at?: unknown;
};

export type ErMerge = {
  winner?: unknown;
  losers?: unknown;
  reason?: unknown;
  score?: unknown;
  triggered_at?: unknown;
  provenance?: {
    source?: unknown;
    observed_at?: unknown;
    authority?: unknown;
  };
};

export type ErConflict = {
  field?: unknown;
  entity?: unknown;
  winner?: ErClaim;
  loser?: ErClaim | null;
  reason?: unknown;
  values?: ErClaim[];
  flagged?: unknown;
};

export type ErRebuildReport = {
  types?: Array<Record<string, unknown>>;
  fragments_absorbed_total?: unknown;
  merges?: ErMerge[];
  conflicts?: ErConflict[];
  unresolved?: ErConflict[];
};

function asList<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

function flatten<T>(
  report: ErRebuildReport,
  key: "merges" | "conflicts" | "unresolved",
): T[] {
  const top = asList<T>(report[key]);
  if (top.length) return top;
  const out: T[] = [];
  for (const t of asList<Record<string, unknown>>(report.types)) {
    out.push(...asList<T>(t[key]));
  }
  return out;
}

function claimLine(claim: ErClaim | null | undefined): string {
  if (!claim) return "";
  return `${claim.value ?? ""}  (${claim.source ?? ""}, ${claim.authority ?? ""}, ${claim.observed_at ?? ""})`;
}

/** Full stranger-facing rebuild dump (counts + merges + leftover conflict). */
export function formatErRebuild(report: ErRebuildReport, kg: string): string {
  const lines: string[] = [`Rebuilding entity resolution for ${kg}…`];
  const types = asList<Record<string, unknown>>(report.types);
  for (const t of types) {
    const name = String(t.type ?? "?").padEnd(16, " ");
    lines.push(
      `  ${name} ${t.entities_before} → ${t.entities_after}` +
        `  (−${t.fragments_absorbed} fragments across ${t.clusters_merged} clusters)`,
    );
  }

  for (const merge of flatten<ErMerge>(report, "merges")) {
    lines.push("");
    lines.push(`  merge  ${merge.winner ?? "?"}`);
    const losers = asList<string>(merge.losers);
    if (losers.length) {
      lines.push(`         losers:     ${losers.join(", ")}`);
    }
    lines.push(`         reason:     ${merge.reason ?? ""}`);
    if (merge.score !== undefined && merge.score !== null) {
      const n = Number(merge.score);
      lines.push(`         score:      ${Number.isFinite(n) ? n.toFixed(2) : merge.score}`);
    }
    const prov = merge.provenance ?? {};
    const src = String(prov.source ?? "");
    const when = String(prov.observed_at ?? merge.triggered_at ?? "");
    const auth = String(prov.authority ?? "");
    let tail = `${src} @ ${when}`.replace(/^ @ | @ $/g, "").trim();
    if (auth) tail = tail ? `${tail} (${auth})` : `(${auth})`;
    if (tail) lines.push(`         provenance: ${tail}`);
  }

  for (const conflict of flatten<ErConflict>(report, "conflicts")) {
    lines.push("");
    lines.push(`  conflict  ${conflict.field ?? "?"}`);
    lines.push(`         entity:     ${conflict.entity ?? ""}`);
    lines.push(`         winner:     ${claimLine(conflict.winner)}`);
    if (conflict.loser) {
      lines.push(`         loser:      ${claimLine(conflict.loser)}`);
    }
    lines.push(`         reason:     ${conflict.reason ?? ""}`);
  }

  for (const item of flatten<ErConflict>(report, "unresolved")) {
    lines.push("");
    lines.push(`  unresolved  ${item.field ?? "?"}`);
    lines.push(`         entity:     ${item.entity ?? ""}`);
    for (const claim of asList<ErClaim>(item.values)) {
      const src = String(claim.source ?? "?");
      lines.push(
        `         ${src}: ${claim.value ?? ""} @ ${claim.observed_at ?? ""} (${claim.authority ?? ""})`,
      );
    }
    const flagged =
      item.flagged || "equal-trust sources — not silently guessed";
    lines.push(`         flagged: ${flagged}`);
  }

  let total = report.fragments_absorbed_total;
  if (total === undefined || total === null) {
    total = types.reduce(
      (sum, t) => sum + Number(t.fragments_absorbed ?? 0),
      0,
    );
  }
  lines.push("");
  lines.push(`Done. ${total} fragments absorbed.`);
  return `${lines.join("\n")}\n`;
}
