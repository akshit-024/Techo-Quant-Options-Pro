import type { MarketSnapshot, OptionLeg } from "../../domain/types";
import { formatNumber, formatPrice, signed } from "../../lib/format";
import { Badge } from "../../components/ui/Badge";
import { Icon } from "../../components/ui/Icon";
import { SectionHeader } from "../../components/ui/SectionHeader";

interface GreeksViewProps {
  snapshot: MarketSnapshot;
  selectedLegKey: string;
  onLegSelect: (key: string) => void;
}

function resolveLeg(snapshot: MarketSnapshot, key: string): { strike: number; leg: OptionLeg } {
  const [rawStrike, rawSide] = key.split(":");
  const row = snapshot.chain.find((item) => item.strike === Number(rawStrike)) ?? snapshot.chain[2];
  return { strike: row.strike, leg: rawSide === "PE" ? row.put : row.call };
}

export function GreeksView({ snapshot, selectedLegKey, onLegSelect }: GreeksViewProps) {
  const selected = resolveLeg(snapshot, selectedLegKey);
  const model = snapshot.selection.market === "MCX" ? "Black–76" : "Black–Scholes";
  const theoretical = selected.leg.greeks.theoreticalPrice;
  const theoreticalGap =
    theoretical === null ? null : selected.leg.ltp - theoretical;

  return (
    <div className="page-stack">
      <div className="page-intro">
        <div>
          <p className="eyebrow">GREEKS ENGINE</p>
          <h1>Risk sensitivity, leg by leg</h1>
          <p>Sensitivities are organized per leg using each side’s own IV and the intended market model.</p>
        </div>
        <div className="page-intro__meta">
          <span>Pricing model <strong>{model}</strong></span>
          <span>Rate <strong>6.00%</strong></span>
          <span>Time basis <strong>Actual / 365</strong></span>
        </div>
      </div>

      <section className="greeks-hero panel">
        <div className="greeks-contract">
          <span className="eyebrow">SELECTED CONTRACT</span>
          <div className="greeks-contract__name">
            <strong>{formatNumber(selected.strike, 0)}</strong>
            <span className={`side-pill side-pill--${selected.leg.side.toLowerCase()}`}>{selected.leg.side}</span>
          </div>
          <p>{snapshot.selection.symbol} · {snapshot.selection.expiry}</p>
          <div className="greeks-contract__quote">
            <div><span>Bid exit</span><strong>{formatPrice(selected.leg.bid)}</strong></div>
            <div><span>Ask entry</span><strong>{formatPrice(selected.leg.ask)}</strong></div>
            <div><span>Own IV</span><strong>{formatNumber(selected.leg.iv, 2)}%</strong></div>
          </div>
        </div>

        <div className="greeks-metrics">
          <div className="greek-metric greek-metric--delta"><span>Δ</span><div><small>Delta</small><strong>{signed(selected.leg.greeks.delta)}</strong><p>Directional sensitivity</p></div></div>
          <div className="greek-metric greek-metric--gamma"><span>Γ</span><div><small>Gamma</small><strong>{formatNumber(selected.leg.greeks.gamma, 6)}</strong><p>Delta acceleration</p></div></div>
          <div className="greek-metric greek-metric--theta"><span>Θ</span><div><small>Theta / day</small><strong>{signed(selected.leg.greeks.theta)}</strong><p>Calendar decay</p></div></div>
          <div className="greek-metric greek-metric--vega"><span>V</span><div><small>Vega</small><strong>{formatNumber(selected.leg.greeks.vega)}</strong><p>Per IV-point move</p></div></div>
        </div>

        <div className="theoretical-card">
          <Icon name="flask" size={20} />
          <span>Theoretical value</span>
          <strong>{theoretical === null ? "Unavailable" : formatPrice(theoretical)}</strong>
          {theoreticalGap === null ? (
            <p>Model value is unavailable for this provider snapshot.</p>
          ) : (
            <p className={theoreticalGap >= 0 ? "negative-number" : "positive-number"}>
              LTP {theoreticalGap >= 0 ? "above" : "below"} model by {formatPrice(Math.abs(theoreticalGap))}
            </p>
          )}
          <Badge tone="neutral">Reference only</Badge>
        </div>
      </section>

      <section className="panel greeks-table-panel">
        <SectionHeader
          eyebrow="Five-strike surface"
          title="Call and put sensitivities"
          description="Select any row to move that contract into the inspection panel."
          action={<Badge tone="purple">{model}</Badge>}
        />
        <div className="table-scroll">
          <table className="greeks-table">
            <thead><tr><th>Contract</th><th>LTP</th><th>IV</th><th>Delta</th><th>Gamma</th><th>Theta/day</th><th>Vega</th><th>Theoretical</th><th>Difference</th></tr></thead>
            <tbody>
              {snapshot.chain.flatMap((row) => [row.call, row.put].map((leg) => {
                const key = `${row.strike}:${leg.side}`;
                const difference =
                  leg.greeks.theoreticalPrice === null
                    ? null
                    : leg.ltp - leg.greeks.theoreticalPrice;
                return (
                  <tr className={selectedLegKey === key ? "is-selected" : ""} key={key}>
                    <th scope="row"><button className="contract-button" onClick={() => onLegSelect(key)} type="button"><strong>{formatNumber(row.strike, 0)}</strong><span className={`side-pill side-pill--${leg.side.toLowerCase()}`}>{leg.side}</span></button></th>
                    <td>{formatPrice(leg.ltp)}</td>
                    <td>{formatNumber(leg.iv, 2)}%</td>
                    <td>{signed(leg.greeks.delta)}</td>
                    <td>{formatNumber(leg.greeks.gamma, 6)}</td>
                    <td className="negative-number">{signed(leg.greeks.theta)}</td>
                    <td>{formatNumber(leg.greeks.vega)}</td>
                    <td>{leg.greeks.theoreticalPrice === null ? "—" : formatPrice(leg.greeks.theoreticalPrice)}</td>
                    <td className={difference === null ? undefined : difference >= 0 ? "negative-number" : "positive-number"}>{difference === null ? "—" : signed(difference)}</td>
                  </tr>
                );
              }))}
            </tbody>
          </table>
        </div>
      </section>

      <div className="formula-note">
        <Icon name="info" size={17} />
        <p><strong>Transparent model note.</strong> Greeks and theoretical values are analytical references, not executable prices. Entry estimates always use ask and exit estimates use bid.</p>
      </div>
    </div>
  );
}
