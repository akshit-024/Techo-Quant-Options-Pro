import { useMemo, useState } from "react";
import { Badge } from "../../components/ui/Badge";
import { SectionHeader } from "../../components/ui/SectionHeader";
import {
  DEMO_JOURNAL_TRADES,
  type JournalFilter,
  type JournalOutcome,
  type JournalTrade,
} from "../../data/operationsDemo";
import {
  DemoBoundary,
  formatOperationsDate,
  formatOperationsMoney,
  OperationsHeading,
} from "./OperationsChrome";
import "./operations.css";

export interface TradeJournalViewProps {
  trades?: readonly JournalTrade[];
  initialFilter?: JournalFilter;
}

const FILTERS: readonly JournalFilter[] = ["ALL", "WIN", "LOSS", "FLAT", "OPEN"];

function outcomeTone(outcome: JournalOutcome): "positive" | "danger" | "warning" | "info" {
  if (outcome === "WIN") return "positive";
  if (outcome === "LOSS") return "danger";
  if (outcome === "OPEN") return "info";
  return "warning";
}

export function TradeJournalView({
  trades = DEMO_JOURNAL_TRADES,
  initialFilter = "ALL",
}: TradeJournalViewProps) {
  const [filter, setFilter] = useState<JournalFilter>(initialFilter);
  const summary = useMemo(() => {
    const closed = trades.filter((trade) => trade.outcome !== "OPEN");
    const wins = closed.filter((trade) => trade.outcome === "WIN").length;
    const realizedPnl = closed.reduce((total, trade) => total + (trade.realizedPnl ?? 0), 0);
    const measuredR = closed.filter((trade) => trade.rMultiple !== null);
    const averageR = measuredR.length === 0
      ? 0
      : measuredR.reduce((total, trade) => total + (trade.rMultiple ?? 0), 0) / measuredR.length;
    return {
      closed: closed.length,
      open: trades.length - closed.length,
      winRate: closed.length === 0 ? 0 : (wins / closed.length) * 100,
      realizedPnl,
      averageR,
    };
  }, [trades]);
  const visibleTrades = filter === "ALL"
    ? trades
    : trades.filter((trade) => trade.outcome === filter);

  return (
    <div className="page-stack ops-page">
      <OperationsHeading
        eyebrow="Review and learning"
        title="Trade journal"
        description="Outcome evidence, risk multiples, and exit reasons shown together for post-trade review."
        meta={<span className="ops-heading__timestamp">Read-only demo ledger</span>}
      />

      <DemoBoundary>
        These entries do not come from the execution SQLite ledger. Connect the trusted backend before treating this view as a book of record.
      </DemoBoundary>

      <section className="ops-stat-grid" aria-label="Journal summary">
        <article className="ops-stat-card"><span>Closed trades</span><strong>{summary.closed}</strong><small>{summary.open} demo position open</small></article>
        <article className="ops-stat-card ops-stat-card--positive"><span>Win rate</span><strong>{summary.winRate.toFixed(1)}%</strong><small>Closed demo trades</small></article>
        <article className={summary.realizedPnl >= 0 ? "ops-stat-card ops-stat-card--positive" : "ops-stat-card ops-stat-card--danger"}><span>Realized P&amp;L</span><strong>{formatOperationsMoney(summary.realizedPnl)}</strong><small>Before taxes and fees</small></article>
        <article className="ops-stat-card"><span>Average outcome</span><strong>{summary.averageR >= 0 ? "+" : ""}{summary.averageR.toFixed(2)} R</strong><small>Measured closed trades</small></article>
      </section>

      <section className="panel ops-table-panel">
        <SectionHeader
          eyebrow="Demo journal"
          title="Trade-by-trade evidence"
          description="Filters change only the visible demo rows; they never mutate a position or journal record."
          action={
            <div className="ops-filter" aria-label="Filter journal by outcome" role="group">
              {FILTERS.map((value) => (
                <button
                  aria-pressed={filter === value}
                  className={filter === value ? "is-active" : undefined}
                  key={value}
                  onClick={() => setFilter(value)}
                  type="button"
                >
                  {value}
                </button>
              ))}
            </div>
          }
        />
        <div className="ops-table-wrap">
          <table className="ops-table ops-table--journal">
            <caption className="ops-sr-only">Filtered demo trade journal</caption>
            <thead>
              <tr>
                <th scope="col">Trade</th>
                <th scope="col">Contract</th>
                <th scope="col">Entry / exit</th>
                <th scope="col">Quantity</th>
                <th scope="col">Prices</th>
                <th scope="col">P&amp;L</th>
                <th scope="col">R</th>
                <th scope="col">Outcome</th>
                <th scope="col">Exit reason</th>
              </tr>
            </thead>
            <tbody>
              {visibleTrades.map((trade) => (
                <tr key={trade.id}>
                  <td><strong className="ops-mono">{trade.id}</strong><small>{trade.market}</small></td>
                  <td><strong>{trade.contract}</strong><small>{trade.side}</small></td>
                  <td><strong>{formatOperationsDate(trade.enteredAt)}</strong><small>{trade.exitedAt ? formatOperationsDate(trade.exitedAt) : "Position open"}</small></td>
                  <td className="ops-mono">{trade.quantity.toLocaleString("en-IN")}</td>
                  <td className="ops-mono"><strong>{formatOperationsMoney(trade.entryPrice)}</strong><small>{trade.exitPrice === null ? "—" : formatOperationsMoney(trade.exitPrice)}</small></td>
                  <td className={trade.realizedPnl === null ? "ops-muted" : trade.realizedPnl >= 0 ? "positive-number" : "negative-number"}>{trade.realizedPnl === null ? "Open" : formatOperationsMoney(trade.realizedPnl)}</td>
                  <td className="ops-mono">{trade.rMultiple === null ? "—" : `${trade.rMultiple > 0 ? "+" : ""}${trade.rMultiple.toFixed(2)} R`}</td>
                  <td><Badge tone={outcomeTone(trade.outcome)} dot>{trade.outcome}</Badge></td>
                  <td>{trade.exitReason}</td>
                </tr>
              ))}
              {visibleTrades.length === 0 ? (
                <tr><td className="ops-empty" colSpan={9}>No demo trades match this filter.</td></tr>
              ) : null}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
