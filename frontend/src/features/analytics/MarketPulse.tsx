import type { MarketSnapshot } from "../../domain/types";
import { formatNumber, formatPrice, signed } from "../../lib/format";
import { Badge } from "../../components/ui/Badge";
import { Icon } from "../../components/ui/Icon";
import { Sparkline } from "./Sparkline";

interface MarketPulseProps {
  snapshot: MarketSnapshot;
}

export function MarketPulse({ snapshot }: MarketPulseProps) {
  const { analytics } = snapshot;
  const spot = Number(snapshot.inputs.find((item) => item.id === "spot")?.effectiveValue ?? 0);
  const trendTone = analytics.trend === "BULLISH" ? "positive" : analytics.trend === "BEARISH" ? "danger" : "warning";

  return (
    <div className="market-pulse-grid">
      <article className="panel market-chart-card">
        <div className="panel__header">
          <div>
            <p className="eyebrow">Underlying pulse</p>
            <h3>{snapshot.selection.symbol}</h3>
          </div>
          <Badge tone={trendTone}>{analytics.trend}</Badge>
        </div>
        <div className="market-price-line">
          <strong>{formatPrice(spot)}</strong>
          <span className={analytics.trend === "BEARISH" ? "negative-number" : "positive-number"}>
            {signed((analytics.spotSeries.at(-1) ?? spot) - (analytics.spotSeries[0] ?? spot))} pts
          </span>
        </div>
        <Sparkline label={`${snapshot.selection.symbol} available price observations`} values={analytics.spotSeries} tone={analytics.trend === "BEARISH" ? "violet" : "cyan"} />
        <div className="chart-axis"><span>09:15</span><span>11:42</span></div>
        <div className="range-band">
          <div><span>Expected low</span><strong>{formatPrice(analytics.expectedLow)}</strong></div>
          <div className="range-band__move"><span>Expected move</span><strong>± {formatNumber(analytics.expectedMove)}</strong></div>
          <div><span>Expected high</span><strong>{formatPrice(analytics.expectedHigh)}</strong></div>
        </div>
      </article>

      <article className="panel evidence-card">
        <div className="panel__header">
          <div>
            <p className="eyebrow">Evidence stack</p>
            <h3>Market structure</h3>
          </div>
          <Icon name="pulse" />
        </div>
        <div className="evidence-list">
          <div className="evidence-row">
            <span>Trend strength</span>
            <div className="evidence-meter"><i style={{ width: `${analytics.trendStrength}%` }} /></div>
            <strong>{analytics.trendStrength}</strong>
          </div>
          <div className="evidence-row">
            <span>OI PCR</span>
            <div className="evidence-meter evidence-meter--purple"><i style={{ width: `${Math.min(100, analytics.pcr * 65)}%` }} /></div>
            <strong>{formatNumber(analytics.pcr)}</strong>
          </div>
          <div className="evidence-row">
            <span>Change-OI PCR</span>
            <div className="evidence-meter evidence-meter--amber"><i style={{ width: `${Math.min(100, analytics.changeOiPcr * 58)}%` }} /></div>
            <strong>{formatNumber(analytics.changeOiPcr)}</strong>
          </div>
        </div>
        <div className="level-grid">
          <div><span>Support</span><strong>{formatNumber(analytics.support, 0)}</strong><small>Highest put OI</small></div>
          <div><span>Resistance</span><strong>{formatNumber(analytics.resistance, 0)}</strong><small>Highest call OI</small></div>
          <div><span>ATM average IV</span><strong>{formatNumber(analytics.atmIv, 1)}%</strong><small>CE and PE own IV</small></div>
          <div><span>Synthetic future</span><strong>{formatNumber(analytics.syntheticFutures)}</strong><small>Supporting evidence only</small></div>
        </div>
      </article>
    </div>
  );
}
