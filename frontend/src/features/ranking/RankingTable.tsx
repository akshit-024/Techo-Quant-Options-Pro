import type { RankingEntry } from "../../domain/types";
import { formatNumber, formatPrice } from "../../lib/format";
import { Badge } from "../../components/ui/Badge";

interface RankingTableProps {
  entries: readonly RankingEntry[];
  symbol: string;
  dataMode: "LIVE" | "DEMO" | "STALE";
  selectedLegKey: string;
  limit?: number;
  onSelect: (key: string) => void;
}

function bandTone(
  entry: RankingEntry,
): "positive" | "info" | "warning" | "danger" {
  if (entry.band === "STRONG") return "positive";
  if (entry.band === "TRADABLE") return "info";
  if (entry.band === "WATCHLIST") return "warning";
  return "danger";
}

export function RankingTable({
  entries,
  symbol,
  dataMode,
  selectedLegKey,
  limit,
  onSelect,
}: RankingTableProps) {
  /*
   * STALE backend data is never rendered as an active ranking.
   *
   * LIVE:
   *   Render validated backend ranking entries.
   *
   * DEMO:
   *   Render deterministic demo entries only when the user
   *   explicitly selected DEMO mode in App.tsx.
   *
   * App.tsx guarantees LIVE mode can never silently fall
   * back to a demo snapshot.
   */
  if (dataMode === "STALE") {
    return (
      <div className="ranking-table-wrap">
        <div className="live-data-unavailable">
          <strong>
            {symbol} ranking unavailable
          </strong>

          <p>
            The latest backend market snapshot is stale.
            Waiting for a fresh validated Dhan snapshot.
          </p>
        </div>
      </div>
    );
  }

  const visibleEntries =
    limit === undefined
      ? entries
      : entries.slice(0, limit);

  /*
   * Fail closed if the selected snapshot contains no
   * usable ranking entries.
   */
  if (visibleEntries.length === 0) {
    return (
      <div className="ranking-table-wrap">
        <div className="live-data-unavailable">
          <strong>
            {dataMode === "LIVE"
              ? "No eligible contracts in this view"
              : "Demo ranking unavailable"}
          </strong>

          <p>
            {dataMode === "LIVE"
              ? "All current option legs are blocked by one or more validation gates. Open Strike ranking to audit every rejection."
              : "The demo snapshot does not contain valid ranking entries."}
          </p>
        </div>
      </div>
    );
  }

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
            const key =
              `${entry.strike}:${entry.side}`;

            const selected =
              selectedLegKey === key;

            return (
              <tr
                className={
                  selected
                    ? "is-selected"
                    : ""
                }
                key={key}
              >
                {/* Rank */}
                <td>
                  <span
                    className={`rank-number rank-number--${Math.min(
                      entry.rank,
                      3,
                    )}`}
                  >
                    {entry.rank}
                  </span>

                  {entry.rank === 1 ? (
                    <small className="rank-label">
                      BEST
                    </small>
                  ) : null}

                  {entry.rank === 2 ? (
                    <small className="rank-label">
                      SECOND
                    </small>
                  ) : null}
                </td>

                {/* Contract */}
                <th scope="row">
                  <button
                    className="contract-button"
                    onClick={() =>
                      onSelect(key)
                    }
                    type="button"
                  >
                    <div className="contract-name">
                      <strong>
                        {entry.contractName}
                      </strong>
                    </div>

                    <div className="contract-meta">
                      <span>
                        Strike{" "}
                        {formatNumber(
                          entry.strike,
                          0,
                        )}
                      </span>

                      <span
                        className={`side-pill side-pill--${entry.side.toLowerCase()}`}
                      >
                        {entry.side}
                      </span>
                    </div>
                  </button>
                </th>

                {/* Score */}
                <td>
                  <strong className="ranking-score">
                    {formatNumber(
                      entry.score,
                      1,
                    )}
                  </strong>
                </td>

                {/* Band */}
                <td>
                  <Badge
                    tone={bandTone(entry)}
                  >
                    {entry.band}
                  </Badge>
                </td>

                {/* Ask */}
                <td className="ask-price">
                  <strong>
                    {formatPrice(
                      entry.askEntry,
                    )}
                  </strong>
                </td>

                {/* Bid */}
                <td>
                  {formatPrice(
                    entry.bidExit,
                  )}
                </td>

                {/* Liquidity */}
                <td>
                  {formatNumber(
                    entry.liquidityScore,
                    1,
                  )}
                </td>

                {/* Spread */}
                <td>
                  {formatNumber(
                    entry.spreadPercent,
                    2,
                  )}
                  %
                </td>

                {/* Validation */}
                <td>
                  {entry.rejectionReasons
                    .length === 0 ? (
                    <span className="validation-ok">
                      <i /> Eligible
                    </span>
                  ) : (
                    <span
                      className="validation-reject"
                      title={
                        entry.rejectionReasons.join(
                          ", ",
                        )
                      }
                    >
                      {entry.rejectionReasons.join(
                        " · ",
                      )}
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
