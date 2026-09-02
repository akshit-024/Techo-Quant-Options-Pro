import type { MarketSnapshot, OptionLeg } from "../../domain/types";
import { formatNumber, formatPrice } from "../../lib/format";
import { Badge } from "../../components/ui/Badge";
import { Icon } from "../../components/ui/Icon";
import { SectionHeader } from "../../components/ui/SectionHeader";
import { RankingTable } from "./RankingTable";

interface RankingViewProps {
  snapshot: MarketSnapshot;
  selectedLegKey: string;
  onLegSelect: (key: string) => void;
}

function selectedLeg(snapshot: MarketSnapshot, key: string): { strike: number; leg: OptionLeg } {
  const [rawStrike, side] = key.split(":");
  const strike = Number(rawStrike);
  const row = snapshot.chain.find((item) => item.strike === strike) ?? snapshot.chain[2];
  return { strike: row.strike, leg: side === "PE" ? row.put : row.call };
}

export function RankingView({ snapshot, selectedLegKey, onLegSelect }: RankingViewProps) {
  const selection = selectedLeg(snapshot, selectedLegKey);
  const best = snapshot.ranking[0];
  const second = snapshot.ranking[1];

  return (
    <div className="page-stack">
      <div className="page-intro">
        <div>
          <p className="eyebrow">RANKING ENGINE</p>
          <h1>Executable strike selection</h1>
          <p>Independent CE and PE liquidity, explicit rejection reasons and price-aware ranking.</p>
        </div>
        <div className="page-intro__meta">
          <span>Universe <strong>10 legs</strong></span>
          <span>Eligible <strong>{snapshot.ranking.filter((item) => item.rejectionReasons.length === 0).length}</strong></span>
          <span>Score floor <strong>75 tradable</strong></span>
        </div>
      </div>

      <div className="top-picks-grid">
        {[best, second].map((entry, index) => (
          <button
            className={`top-pick-card top-pick-card--${index + 1}`}
            key={`${entry.strike}:${entry.side}`}
            onClick={() => onLegSelect(`${entry.strike}:${entry.side}`)}
            type="button"
          >
            <span className="top-pick-card__rank">{index === 0 ? "BEST STRIKE" : "SECOND BEST"}</span>
            <div><strong>{formatNumber(entry.strike, 0)}</strong><span className={`side-pill side-pill--${entry.side.toLowerCase()}`}>{entry.side}</span></div>
            <p>{formatNumber(entry.score, 1)} score · {formatNumber(entry.liquidityScore, 1)} liquidity</p>
            <span className="top-pick-card__price">Ask entry <strong>{formatPrice(entry.askEntry)}</strong></span>
          </button>
        ))}
        <article className="selected-contract-card">
          <div className="selected-contract-card__heading">
            <span>INSPECTING</span>
            <Badge tone={selection.leg.rejectionReasons.length ? "warning" : "positive"}>
              {selection.leg.rejectionReasons.length ? "Review" : "Eligible"}
            </Badge>
          </div>
          <div className="selected-contract-card__contract">
            <strong>{formatNumber(selection.strike, 0)} {selection.leg.side}</strong>
            <span>{selection.leg.securityId}</span>
          </div>
          <div className="selected-contract-card__prices">
            <span>Entry ask <strong>{formatPrice(selection.leg.ask)}</strong></span>
            <span>Exit bid <strong>{formatPrice(selection.leg.bid)}</strong></span>
          </div>
        </article>
      </div>

      <section className="panel ranking-panel">
        <SectionHeader
          eyebrow="All candidates"
          title={`${snapshot.selection.symbol} strike ranking`}
          description="Rejected contracts remain visible for audit, but rank behind every eligible contract."
          action={<Badge tone="neutral"><Icon name="info" size={13} /> Click a contract to inspect</Badge>}
        />
        <RankingTable entries={snapshot.ranking} onSelect={onLegSelect} selectedLegKey={selectedLegKey} />
      </section>

      <div className="score-legend" aria-label="Score band legend">
        <span><i className="legend-strong" /> 85–100 Strong</span>
        <span><i className="legend-tradable" /> 75–84 Tradable</span>
        <span><i className="legend-watch" /> 65–74 Watchlist</span>
        <span><i className="legend-blocked" /> Below 65 No trade</span>
      </div>
    </div>
  );
}
