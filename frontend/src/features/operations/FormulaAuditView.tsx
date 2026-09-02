import { useMemo } from "react";
import { Badge } from "../../components/ui/Badge";
import { Icon } from "../../components/ui/Icon";
import { SectionHeader } from "../../components/ui/SectionHeader";
import type { MarketSnapshot } from "../../domain/types";
import {
  buildFormulaAudit,
  type AuditStatus,
  type FormulaAuditCheck,
} from "../../data/operationsDemo";
import { DemoBoundary, OperationsHeading } from "./OperationsChrome";
import "./operations.css";

export interface FormulaAuditViewProps {
  snapshot: MarketSnapshot;
  now?: Date;
}

function auditTone(status: AuditStatus): "positive" | "warning" | "danger" {
  if (status === "PASS") return "positive";
  if (status === "WARN") return "warning";
  return "danger";
}

function overallStatus(checks: readonly FormulaAuditCheck[]): AuditStatus {
  if (checks.some((check) => check.status === "FAIL")) return "FAIL";
  if (checks.some((check) => check.status === "WARN")) return "WARN";
  return "PASS";
}

export function FormulaAuditView({ snapshot, now }: FormulaAuditViewProps) {
  const checks = useMemo(
    () => buildFormulaAudit(snapshot, now ?? new Date()),
    [snapshot, now],
  );
  const status = overallStatus(checks);
  const counts = checks.reduce<Record<AuditStatus, number>>(
    (totals, check) => ({ ...totals, [check.status]: totals[check.status] + 1 }),
    { PASS: 0, WARN: 0, FAIL: 0 },
  );

  return (
    <div className="page-stack ops-page">
      <OperationsHeading
        eyebrow="Runtime consistency"
        title="Formula audit"
        description="Checks are recomputed from the active MarketSnapshot on every render; no result is hard-coded to pass."
        meta={<Badge dot tone={auditTone(status)}>Overall {status}</Badge>}
      />

      <DemoBoundary tone={status === "FAIL" ? "CAUTION" : "INFO"}>
        This client-side consistency audit helps explain the current UI. It does not replace canonical backend validation, source attestation, or execution risk checks.
      </DemoBoundary>

      <section className={`ops-audit-summary ops-audit-summary--${status.toLowerCase()}`}>
        <div className="ops-audit-summary__status">
          <span><Icon name={status === "PASS" ? "shield" : status === "WARN" ? "info" : "x"} size={25} /></span>
          <div><p>Runtime result</p><strong>{status}</strong><small>{checks.length} checks evaluated from {snapshot.selection.market} / {snapshot.selection.symbol}</small></div>
        </div>
        <div className="ops-audit-summary__counts">
          <div><span className="ops-audit-dot ops-audit-dot--pass" /> <strong>{counts.PASS}</strong><small>Pass</small></div>
          <div><span className="ops-audit-dot ops-audit-dot--warn" /> <strong>{counts.WARN}</strong><small>Warn</small></div>
          <div><span className="ops-audit-dot ops-audit-dot--fail" /> <strong>{counts.FAIL}</strong><small>Fail</small></div>
        </div>
        <dl className="ops-audit-summary__context">
          <div><dt>Captured</dt><dd>{snapshot.capturedAt}</dd></div>
          <div><dt>Mode</dt><dd>{snapshot.dataMode}</dd></div>
          <div><dt>Decision</dt><dd>{snapshot.analytics.decision}</dd></div>
        </dl>
      </section>

      <section className="panel ops-audit-panel">
        <SectionHeader
          eyebrow="Computed evidence"
          title="Snapshot checks"
          description="Identity, topology, executable prices, Greeks, scoring, ranking, provenance, and freshness are evaluated from the supplied object."
          action={<Badge tone="neutral"><Icon name="calculator" size={11} /> Runtime derived</Badge>}
        />
        <div className="ops-audit-list">
          {checks.map((check, index) => (
            <article className={`ops-audit-check ops-audit-check--${check.status.toLowerCase()}`} key={check.id}>
              <div className="ops-audit-check__number">{String(index + 1).padStart(2, "0")}</div>
              <div className="ops-audit-check__copy">
                <span>{check.category}</span>
                <h3>{check.label}</h3>
                <p>{check.evidence}</p>
              </div>
              <Badge dot tone={auditTone(check.status)}>{check.status}</Badge>
            </article>
          ))}
        </div>
      </section>

      <div className="ops-readonly-callout">
        <Icon name="lock" size={15} />
        Audit results are explanatory and read only. A failed client check cannot be overridden here.
      </div>
    </div>
  );
}
