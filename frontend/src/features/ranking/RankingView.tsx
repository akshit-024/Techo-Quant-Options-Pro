import type { MarketSnapshot, OptionLeg } from "../../domain/types";
import { formatNumber, formatPrice } from "../../lib/format";
import { Badge } from "../../components/ui/Badge";
import { SectionHeader } from "../../components/ui/SectionHeader";
import { RankingTable } from "./RankingTable";

interface RankingViewProps {
  snapshot: MarketSnapshot;
  selectedLegKey: string;
  onLegSelect: (key: string) => void;
}

function selectedLeg(
  snapshot: MarketSnapshot,
  key: string,
): {
  strike: number;
  leg: OptionLeg;
} {
  const [rawStrike, side] = key.split(":");
  const strike = Number(rawStrike);

  const row =
    snapshot.chain.find(
      (item) => item.strike === strike,
    ) ?? snapshot.chain[2];

  return {
    strike: row.strike,
    leg: side === "PE" ? row.put : row.call,
  };
}

export function RankingView({
  snapshot,
  selectedLegKey,
  onLegSelect,
}: RankingViewProps) {
  const selection = selectedLeg(
    snapshot,
    selectedLegKey,
  );

  const topEligible = snapshot.ranking
    .filter((entry) => entry.rejectionReasons.length === 0)
    .slice(0, 2);

  const dataModeTone =
    snapshot.dataMode === "LIVE"
      ? "positive"
      : snapshot.dataMode === "STALE"
        ? "warning"
        : "neutral";

  return (
    <div className="page-stack">
      <div className="page-intro">
        <div>
          <p className="eyebrow">
            RANKING ENGINE
          </p>

          <h1>
            Executable strike selection
          </h1>

          <p>
            Independent CE and PE liquidity,
            explicit rejection reasons and
            price-aware ranking.
          </p>
        </div>

        <div className="page-intro__meta">
          <span>
            Universe{" "}
            <strong>{snapshot.ranking.length} legs</strong>
          </span>

          <span>
            Eligible{" "}
            <strong>
              {
                snapshot.ranking.filter(
                  (item) =>
                    item.rejectionReasons
                      .length === 0,
                ).length
              }
            </strong>
          </span>

          <span>
            Score floor{" "}
            <strong>
              75 tradable
            </strong>
          </span>
        </div>
      </div>

      <div className="top-picks-grid">
        {topEligible.map(
          (entry, index) => (
            <button
              className={
                `top-pick-card ` +
                `top-pick-card--${index + 1}`
              }
              key={
                `${entry.strike}:` +
                `${entry.side}`
              }
              onClick={() =>
                onLegSelect(
                  `${entry.strike}:` +
                    `${entry.side}`,
                )
              }
              type="button"
            >
              <span className="top-pick-card__rank">
                {index === 0
                  ? "BEST STRIKE"
                  : "SECOND BEST"}
              </span>

              <div>
                <strong>
                  {entry.contractName}
                </strong>

                <span
                  className={
                    `side-pill ` +
                    `side-pill--${entry.side.toLowerCase()}`
                  }
                >
                  {entry.side}
                </span>
              </div>

              <p>
                {formatNumber(
                  entry.score,
                  1,
                )}{" "}
                score ·{" "}
                {formatNumber(
                  entry.liquidityScore,
                  1,
                )}{" "}
                liquidity
              </p>

              <span className="top-pick-card__price">
                Ask entry{" "}
                <strong>
                  {formatPrice(
                    entry.askEntry,
                  )}
                </strong>
              </span>
            </button>
          ),
        )}

        {topEligible.length === 0 ? (
          <article className="top-pick-card top-pick-card--1">
            <span className="top-pick-card__rank">NO ELIGIBLE STRIKE</span>
            <div><strong>All {snapshot.ranking.length} legs are gated</strong></div>
            <p>Review the rejection reasons in the complete ranking below.</p>
          </article>
        ) : null}

        <article className="selected-contract-card">
          <div className="selected-contract-card__heading">
            <span>
              INSPECTING
            </span>

            <Badge
              tone={
                selection.leg
                  .rejectionReasons
                  .length
                  ? "warning"
                  : "positive"
              }
            >
              {selection.leg
                .rejectionReasons
                .length
                ? "Review"
                : "Eligible"}
            </Badge>
          </div>

          <div className="selected-contract-card__contract">
            <strong>
              {snapshot.selection.symbol}{" "}
              {snapshot.selection.expiry.slice(0, 10)}{" "}
              {formatNumber(
                selection.strike,
                0,
              )}{" "}
              {selection.leg.side}
            </strong>

            <span>
              {selection.leg.securityId}
            </span>
          </div>

          <div className="selected-contract-card__prices">
            <span>
              Entry ask{" "}
              <strong>
                {formatPrice(
                  selection.leg.ask,
                )}
              </strong>
            </span>

            <span>
              Exit bid{" "}
              <strong>
                {formatPrice(
                  selection.leg.bid,
                )}
              </strong>
            </span>
          </div>
        </article>
      </div>

      <section className="panel ranking-panel">
        <SectionHeader
          eyebrow="Ranked candidates"
          title={
            `${snapshot.selection.symbol} ` +
            "strike ranking"
          }
          description={
            `Showing all option legs from the current ` +
            `${snapshot.definition.label} / ` +
            `${snapshot.selection.symbol} / ` +
            `${snapshot.selection.expiry} snapshot. ` +
            `Rejected contracts remain visible for audit.`
          }
          action={
            <div className="ranking-scope-panel">
              <div className="ranking-scope-panel__status">
                <Badge
                  tone={dataModeTone}
                  dot
                >
                  {snapshot.dataMode} snapshot
                </Badge>

                <span aria-live="polite">
                  {snapshot.ranking.length} option legs
                </span>
              </div>
            </div>
          }
        />

        <RankingTable
          entries={snapshot.ranking}
          onSelect={onLegSelect}
          selectedLegKey={
            selectedLegKey
          }
          symbol={
            snapshot.selection.symbol
          }
          dataMode={
            snapshot.dataMode
          }
        />
      </section>

      <div
        className="score-legend"
        aria-label="Score band legend"
      >
        <span>
          <i className="legend-strong" />{" "}
          85–100 Strong
        </span>

        <span>
          <i className="legend-tradable" />{" "}
          75–84 Tradable
        </span>

        <span>
          <i className="legend-watch" />{" "}
          65–74 Watchlist
        </span>

        <span>
          <i className="legend-blocked" />{" "}
          Below 65 No trade
        </span>
      </div>
    </div>
  );
}
