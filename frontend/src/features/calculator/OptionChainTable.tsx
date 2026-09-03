import type { MarketSnapshot, OptionLeg, OptionStrike } from "../../domain/types";
import { formatCompact, formatNumber, formatPrice, signed } from "../../lib/format";
import { Badge } from "../../components/ui/Badge";
import { SectionHeader } from "../../components/ui/SectionHeader";

interface OptionChainTableProps {
  chain: readonly OptionStrike[];
  dataMode: MarketSnapshot["dataMode"];
  selectedLegKey: string;
  presentationMode: "QUICK" | "PRO";
  compact?: boolean;
  onLegSelect: (key: string) => void;
}

export function legKey(strike: number, side: OptionLeg["side"]): string {
  return `${strike}:${side}`;
}

function Validation({ leg }: { leg: OptionLeg }) {
  return leg.rejectionReasons.length === 0 ? (
    <Badge tone="positive">Valid</Badge>
  ) : (
    <span className="chain-rejection" title={leg.rejectionReasons.join(", ")}>
      {leg.rejectionReasons.join(" · ")}
    </span>
  );
}

function LegScore({ leg, strike, selected, onSelect }: {
  leg: OptionLeg;
  strike: number;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      aria-label={`Select ${strike} ${leg.side}`}
      aria-pressed={selected}
      className={`chain-score ${selected ? "is-selected" : ""}`}
      onClick={onSelect}
      type="button"
    >
      <strong>{formatNumber(leg.strikeScore, 1)}</strong>
      <span>{leg.rejectionReasons.length ? "Review" : "Select"}</span>
    </button>
  );
}

export function OptionChainTable({
  chain,
  dataMode,
  selectedLegKey,
  presentationMode,
  compact = false,
  onLegSelect,
}: OptionChainTableProps) {
  const pro = presentationMode === "PRO" && !compact;

  return (
    <section className="content-section option-chain-section" aria-labelledby="chain-title">
      <SectionHeader
        id="chain-title"
        eyebrow="Executable prices"
        title="Five-strike option chain"
        description="ATM−2 through ATM+2. Ask is the estimated entry; bid is the immediate-exit reference."
        action={<div className="side-legend"><span className="ce-dot" /> CE <span className="pe-dot" /> PE</div>}
      />
      <div className="chain-shell">
        <div className="table-scroll">
          <table className={`option-chain ${pro ? "option-chain--pro" : "option-chain--quick"}`}>
            <thead>
              <tr className="chain-side-header">
                <th className="ce-heading" colSpan={pro ? 10 : 5}>CALLS · CE</th>
                <th className="strike-heading">Strike</th>
                <th className="pe-heading" colSpan={pro ? 10 : 5}>PUTS · PE</th>
              </tr>
              <tr>
                <th>Score</th>
                {pro ? <><th>Check</th><th>Liq.</th><th>IV</th><th>Chg OI</th><th>OI</th><th>Volume</th></> : <th>Liq.</th>}
                <th>LTP</th>
                <th>Bid</th>
                <th className="ask-column">Ask entry</th>
                <th className="strike-heading">Level</th>
                <th>Bid exit</th>
                <th>Ask</th>
                <th>LTP</th>
                {pro ? <><th>Volume</th><th>OI</th><th>Chg OI</th><th>IV</th><th>Liq.</th><th>Check</th></> : <th>Liq.</th>}
                <th>Score</th>
              </tr>
            </thead>
            <tbody>
              {chain.map((row) => {
                const callSelected = selectedLegKey === legKey(row.strike, "CE");
                const putSelected = selectedLegKey === legKey(row.strike, "PE");
                return (
                  <tr className={row.moneyness === "ATM" ? "is-atm" : ""} key={row.strike}>
                    <td><LegScore leg={row.call} strike={row.strike} selected={callSelected} onSelect={() => onLegSelect(legKey(row.strike, "CE"))} /></td>
                    {pro ? <>
                      <td><Validation leg={row.call} /></td>
                      <td>{formatNumber(row.call.liquidityScore, 1)}</td>
                      <td>{formatNumber(row.call.iv, 1)}%</td>
                      <td className={row.call.changeOpenInterest === null ? "" : row.call.changeOpenInterest >= 0 ? "positive-number" : "negative-number"}>{row.call.changeOpenInterest === null ? "—" : signed(row.call.changeOpenInterest, "")}</td>
                      <td>{formatCompact(row.call.openInterest)}</td>
                      <td>{formatCompact(row.call.volume)}</td>
                    </> : <td>{formatNumber(row.call.liquidityScore, 1)}</td>}
                    <td>{formatPrice(row.call.ltp)}</td>
                    <td>{formatPrice(row.call.bid)}</td>
                    <td className="ask-column"><strong>{formatPrice(row.call.ask)}</strong></td>
                    <th className="strike-cell" scope="row"><strong>{formatNumber(row.strike, 0)}</strong><span>{row.moneyness}</span></th>
                    <td className="bid-column"><strong>{formatPrice(row.put.bid)}</strong></td>
                    <td>{formatPrice(row.put.ask)}</td>
                    <td>{formatPrice(row.put.ltp)}</td>
                    {pro ? <>
                      <td>{formatCompact(row.put.volume)}</td>
                      <td>{formatCompact(row.put.openInterest)}</td>
                      <td className={row.put.changeOpenInterest === null ? "" : row.put.changeOpenInterest >= 0 ? "positive-number" : "negative-number"}>{row.put.changeOpenInterest === null ? "—" : signed(row.put.changeOpenInterest, "")}</td>
                      <td>{formatNumber(row.put.iv, 1)}%</td>
                      <td>{formatNumber(row.put.liquidityScore, 1)}</td>
                      <td><Validation leg={row.put} /></td>
                    </> : <td>{formatNumber(row.put.liquidityScore, 1)}</td>}
                    <td><LegScore leg={row.put} strike={row.strike} selected={putSelected} onSelect={() => onLegSelect(legKey(row.strike, "PE"))} /></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <div className="chain-notes">
          <span><i className="note-swatch note-swatch--ask" /> Ask = executable entry reference</span>
          <span><i className="note-swatch note-swatch--bid" /> Bid = immediate-exit estimate</span>
          <span><i className="note-swatch note-swatch--atm" /> ATM row</span>
          <span className="chain-notes__disclaimer">
            {dataMode === "DEMO"
              ? "Demo quotes only · LTP is never treated as an executable price"
              : dataMode === "STALE"
                ? "Retained stale backend quotes · non-actionable · LTP is not an executable price"
                : "Validated live backend quotes · ask is the entry reference and bid is the exit reference"}
          </span>
        </div>
      </div>
    </section>
  );
}
