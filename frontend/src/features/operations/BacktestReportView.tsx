import { Badge } from "../../components/ui/Badge";
import { SectionHeader } from "../../components/ui/SectionHeader";
import {
  DEMO_BACKTEST_REPORT,
  type BacktestMetric,
  type BacktestReport,
  type EquityPoint,
} from "../../data/operationsDemo";
import {
  DemoBoundary,
  formatOperationsDate,
  formatOperationsMoney,
  OperationsHeading,
} from "./OperationsChrome";
import "./operations.css";

export interface BacktestReportViewProps {
  report?: BacktestReport;
}

interface EquityGeometry {
  line: string;
  area: string;
  min: number;
  max: number;
  start: number;
  finish: number;
}

function buildEquityGeometry(points: readonly EquityPoint[]): EquityGeometry | null {
  if (points.length === 0) return null;
  const width = 800;
  const height = 220;
  const insetX = 18;
  const insetY = 20;
  const equities = points.map((point) => point.equity);
  const min = Math.min(...equities);
  const max = Math.max(...equities);
  const span = Math.max(max - min, 1);
  const coordinates = points.map((point, index) => {
    const x = points.length === 1
      ? width / 2
      : insetX + (index / (points.length - 1)) * (width - insetX * 2);
    const y = insetY + ((max - point.equity) / span) * (height - insetY * 2);
    return [x, y] as const;
  });
  const line = coordinates.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const area = `M ${coordinates[0][0].toFixed(1)} ${height} L ${coordinates
    .map(([x, y]) => `${x.toFixed(1)} ${y.toFixed(1)}`)
    .join(" L ")} L ${coordinates[coordinates.length - 1][0].toFixed(1)} ${height} Z`;
  return { line, area, min, max, start: equities[0], finish: equities[equities.length - 1] };
}

function metricClass(tone: BacktestMetric["tone"]): string {
  return `ops-backtest-metric ops-backtest-metric--${tone.toLowerCase()}`;
}

export function BacktestReportView({ report = DEMO_BACKTEST_REPORT }: BacktestReportViewProps) {
  const geometry = buildEquityGeometry(report.equity);

  return (
    <div className="page-stack ops-page">
      <OperationsHeading
        eyebrow="Historical simulation"
        title="Backtest report"
        description="A transparent performance layout for metrics, equity progression, assumptions, and sample trades."
        meta={<span className="ops-heading__timestamp">Generated {formatOperationsDate(report.generatedAt)}</span>}
      />

      <DemoBoundary tone="CAUTION">
        This is a deterministic demonstration report, not actual performance. It does not predict future returns and is not investment advice.
      </DemoBoundary>

      <section className="panel ops-backtest-hero">
        <div>
          <p className="eyebrow">{report.sample}</p>
          <h2>{report.title}</h2>
          <p>{report.market} · {report.period}</p>
        </div>
        <dl>
          <div><dt>Timeframe</dt><dd>{report.timeframe}</dd></div>
          <div><dt>Cost model</dt><dd>Demo slippage and spread</dd></div>
          <div><dt>Data source</dt><dd><Badge tone="warning">Demo only</Badge></dd></div>
        </dl>
      </section>

      <section className="ops-backtest-metrics" aria-label="Backtest metrics">
        {report.metrics.map((metric) => (
          <article className={metricClass(metric.tone)} key={metric.id}>
            <span>{metric.label}</span>
            <strong>{metric.value}</strong>
            <small>{metric.detail}</small>
          </article>
        ))}
      </section>

      <section className="panel ops-equity-panel">
        <SectionHeader
          eyebrow="Capital path"
          title="Demo equity curve"
          description="Portfolio value across the supplied observation points; hover-free labels keep the export readable."
          action={geometry ? <Badge tone={geometry.finish >= geometry.start ? "positive" : "danger"}>{formatOperationsMoney(geometry.finish - geometry.start)}</Badge> : null}
        />
        {geometry ? (
          <div className="ops-equity-chart">
            <div className="ops-equity-chart__scale" aria-hidden="true">
              <span>{formatOperationsMoney(geometry.max)}</span>
              <span>{formatOperationsMoney((geometry.max + geometry.min) / 2)}</span>
              <span>{formatOperationsMoney(geometry.min)}</span>
            </div>
            <svg aria-label="Demo equity curve" preserveAspectRatio="none" role="img" viewBox="0 0 800 220">
              <line x1="0" x2="800" y1="20" y2="20" />
              <line x1="0" x2="800" y1="110" y2="110" />
              <line x1="0" x2="800" y1="200" y2="200" />
              <path className="ops-equity-chart__area" d={geometry.area} />
              <polyline className="ops-equity-chart__line" points={geometry.line} />
            </svg>
            <div className="ops-equity-chart__labels" aria-hidden="true">
              {report.equity.map((point) => <span key={point.observation}>{point.label}</span>)}
            </div>
          </div>
        ) : <p className="ops-empty">No equity observations were supplied.</p>}
      </section>

      <section className="panel ops-table-panel">
        <SectionHeader
          eyebrow="Sample outcomes"
          title="Representative backtest trades"
          description="Bid/ask-aware sample exits from the demonstration dataset."
          action={<Badge tone="neutral">{report.trades.length} rows</Badge>}
        />
        <div className="ops-table-wrap">
          <table className="ops-table">
            <caption className="ops-sr-only">Representative demo backtest trades</caption>
            <thead><tr><th scope="col">Trade</th><th scope="col">Contract</th><th scope="col">Entry</th><th scope="col">Exit</th><th scope="col">P&amp;L</th><th scope="col">R multiple</th><th scope="col">Reason</th></tr></thead>
            <tbody>
              {report.trades.map((trade) => (
                <tr key={trade.id}>
                  <td><strong className="ops-mono">{trade.id}</strong><small>{trade.market}</small></td>
                  <td><strong>{trade.contract}</strong><small>{trade.side}</small></td>
                  <td><strong>{formatOperationsMoney(trade.entryPrice)}</strong><small>{formatOperationsDate(trade.enteredAt)}</small></td>
                  <td><strong>{trade.exitPrice === null ? "—" : formatOperationsMoney(trade.exitPrice)}</strong><small>{trade.exitedAt ? formatOperationsDate(trade.exitedAt) : "Open"}</small></td>
                  <td className={(trade.realizedPnl ?? 0) >= 0 ? "positive-number" : "negative-number"}>{trade.realizedPnl === null ? "—" : formatOperationsMoney(trade.realizedPnl)}</td>
                  <td className="ops-mono">{trade.rMultiple === null ? "—" : `${trade.rMultiple > 0 ? "+" : ""}${trade.rMultiple.toFixed(2)} R`}</td>
                  <td>{trade.exitReason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
