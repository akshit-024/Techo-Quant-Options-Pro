import type { RankingEntry } from "../../domain/types";
import { formatNumber, formatPrice } from "../../lib/format";
import { Badge } from "../../components/ui/Badge";

interface RankingTableProps {
  entries: readonly RankingEntry[];
  selectedLegKey: string;
  limit?: number;
  onSelect: (key: string) => void;
}

function bandTone(entry: RankingEntry): "positive" | "info" | "warning" | "danger" {
  if (entry.band === "STRONG") return "positive";
  if (entry.band === "TRADABLE") return "info";
  if (entry.band === "WATCHLIST") return "warning";
  return "danger";
}

export function RankingTable({ entries, selectedLegKey, limit, onSelect }: RankingTableProps) {
  const visibleEntries = limit === undefined ? entries : entries.slice(0, limit);

  return (
    <div className="ranking-table-wrap">
      <table className="ranking-table">
        <thead>
          <tr>
            <th>Rank</th>
            <th>Contract</th>
            <th>Score</th>
            <th>Band</th>
            <th>Ask entry</th>
            <th>Bid exit</th>
            <th>Liquidity</th>
            <th>Spread</th>
            <th>Validation</th>
          </tr>
        </thead>
        <tbody>
          {visibleEntries.map((entry) => {
            const key = `${entry.strike}:${entry.side}`;
            const selected = selectedLegKey === key;
            return (
              <tr className={selected ? "is-selected" : ""} key={key}>
                <td>
                  <span className={`rank-number rank-number--${Math.min(entry.rank, 3)}`}>{entry.rank}</span>
                  {entry.rank === 1 ? <small className="rank-label">BEST</small> : null}
                  {entry.rank === 2 ? <small className="rank-label">SECOND</small> : null}
                </td>
                <th scope="row">
                  <button className="contract-button" onClick={() => onSelect(key)} type="button">
                    <strong>{formatNumber(entry.strike, 0)}</strong>
                    <span className={`side-pill side-pill--${entry.side.toLowerCase()}`}>{entry.side}</span>
                  </button>
                </th>
                <td><strong className="ranking-score">{formatNumber(entry.score, 1)}</strong></td>
                <td><Badge tone={bandTone(entry)}>{entry.band}</Badge></td>
                <td className="ask-price"><strong>{formatPrice(entry.askEntry)}</strong></td>
                <td>{formatPrice(entry.bidExit)}</td>
                <td>{formatNumber(entry.liquidityScore, 1)}</td>
                <td>{formatNumber(entry.spreadPercent, 2)}%</td>
                <td>
                  {entry.rejectionReasons.length === 0 ? (
                    <span className="validation-ok"><i /> Eligible</span>
                  ) : (
                    <span className="validation-reject" title={entry.rejectionReasons.join(", ")}>
                      {entry.rejectionReasons.join(" · ")}
                    </span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
