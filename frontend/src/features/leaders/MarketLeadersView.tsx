import { useState } from "react";

import type { MarketLeadersResponse } from "../../api/contracts";
import { Badge } from "../../components/ui/Badge";
import { SectionHeader } from "../../components/ui/SectionHeader";
import { MARKET_DEFINITIONS, MARKET_ORDER } from "../../data/marketDefinitions";
import type { MarketId } from "../../domain/types";
import type { MarketLeadersConnection } from "../../hooks/useMarketLeaders";
import { formatCompact, formatPrice, signed } from "../../lib/format";

interface MarketLeadersViewProps {
  market: MarketId;
  dataSourceMode: "LIVE" | "DEMO";
  connection: MarketLeadersConnection;
  response: MarketLeadersResponse | null;
  error: string | null;
  onMarketChange: (market: MarketId) => void;
  onRefresh: () => void;
}

type LeaderScope = "TOP_5" | "ALL";

export function MarketLeadersView({
  market,
  dataSourceMode,
  connection,
  response,
  error,
  onMarketChange,
  onRefresh,
}: MarketLeadersViewProps) {
  const [scope, setScope] = useState<LeaderScope>("TOP_5");
  const compatibleResponse = response?.market_id === market ? response : null;
  const effectiveState =
    connection === "STALE"
      ? "STALE"
      : compatibleResponse?.market_state ?? "UNAVAILABLE";
  const visibleLeaders =
    scope === "TOP_5"
      ? compatibleResponse?.leaders.slice(0, 5) ?? []
      : compatibleResponse?.leaders ?? [];

  return (
    <div className="page-stack market-leaders-view">
      <div className="page-intro">
        <div>
          <p className="eyebrow">LIVE MARKET SCANNER</p>
          <h1>Top-performing instruments</h1>
          <p>
            Instruments in the selected bracket ranked by current day percentage
            change. This market-level scanner is separate from option strike ranking.
          </p>
        </div>

        <div className="page-intro__meta">
          <span>
            Bracket <strong>{MARKET_DEFINITIONS[market].shortLabel}</strong>
          </span>
          <span>
            Available <strong>{compatibleResponse?.available_count ?? 0}</strong>
          </span>
          <span>
            Updated <strong>{formatTimestamp(compatibleResponse?.generated_at)}</strong>
          </span>
        </div>
      </div>

      <section className="panel market-leaders-panel">
        <SectionHeader
          eyebrow="Market bracket"
          title={`Top 5 live · ${MARKET_DEFINITIONS[market].label}`}
          description="Choose a bracket below. Prices come from the backend Dhan quote cache and are never replaced with demo values."
          action={
            <Badge
              dot
              tone={
                effectiveState === "LIVE"
                  ? "positive"
                  : effectiveState === "STALE"
                    ? "warning"
                    : "danger"
              }
            >
              {dataSourceMode === "DEMO" ? "LIVE scanner disabled" : effectiveState}
            </Badge>
          }
        />

        <div className="market-bracket-filter" role="group" aria-label="Market bracket">
          {MARKET_ORDER.map((candidate) => (
            <button
              aria-pressed={candidate === market}
              className={candidate === market ? "is-active" : ""}
              key={candidate}
              onClick={() => onMarketChange(candidate)}
              type="button"
            >
              {MARKET_DEFINITIONS[candidate].shortLabel}
            </button>
          ))}
        </div>

        {dataSourceMode === "DEMO" ? (
          <LeaderNotice
            message="Market leaders are a live-data feature. Select LIVE to request the current bracket from the backend."
            onRefresh={null}
            title="Live scanner paused"
          />
        ) : compatibleResponse === null || visibleLeaders.length === 0 ? (
          <LeaderNotice
            message={
              error ??
              (connection === "LOADING"
                ? `Waiting for the first ${MARKET_DEFINITIONS[market].shortLabel} quote batch.`
                : `No current Dhan quotes are available for ${MARKET_DEFINITIONS[market].shortLabel}.`)
            }
            onRefresh={onRefresh}
            title={connection === "LOADING" ? "Loading live leaders" : "Live leaders unavailable"}
          />
        ) : (
          <>
            <div className="market-leader-toolbar">
              <div className="ranking-scope-toggle" role="group" aria-label="Leader count">
                <button
                  aria-pressed={scope === "TOP_5"}
                  className={scope === "TOP_5" ? "is-active" : ""}
                  onClick={() => setScope("TOP_5")}
                  type="button"
                >
                  Top 5
                </button>
                <button
                  aria-pressed={scope === "ALL"}
                  className={scope === "ALL" ? "is-active" : ""}
                  onClick={() => setScope("ALL")}
                  type="button"
                >
                  All {compatibleResponse.leaders.length}
                </button>
              </div>
              <span aria-live="polite">
                Showing {visibleLeaders.length} of {compatibleResponse.available_count}
              </span>
            </div>

            {connection === "STALE" || compatibleResponse.market_state === "STALE" ? (
              <div className="market-leader-warning" role="status">
                These are retained quotes and are not current actionable market data.
                {error === null ? "" : ` ${error}`}
              </div>
            ) : null}

            <div className="ranking-table-wrap">
              <table className="ranking-table market-leaders-table">
                <thead>
                  <tr>
                    <th>Rank</th>
                    <th>Instrument</th>
                    <th>LTP</th>
                    <th>Day change</th>
                    <th>Change %</th>
                    <th>Volume</th>
                    <th>Quote time</th>
                    <th>State</th>
                  </tr>
                </thead>
                <tbody>
                  {visibleLeaders.map((leader) => {
                    const change = Number(leader.change);
                    const positive = leader.change_percent >= 0;
                    return (
                      <tr key={leader.symbol}>
                        <td>
                          <span className={`rank-number rank-number--${Math.min(leader.rank, 3)}`}>
                            {leader.rank}
                          </span>
                        </td>
                        <th scope="row">
                          <strong>{leader.display_name}</strong>
                          <small className="market-leader-symbol">{leader.symbol}</small>
                        </th>
                        <td><strong>{formatPrice(Number(leader.last_price))}</strong></td>
                        <td className={positive ? "market-move--up" : "market-move--down"}>
                          {signed(change)}
                        </td>
                        <td className={positive ? "market-move--up" : "market-move--down"}>
                          <strong>{signed(leader.change_percent, "%")}</strong>
                        </td>
                        <td>{leader.volume === null ? "—" : formatCompact(leader.volume)}</td>
                        <td>{formatTimestamp(leader.observed_at)}</td>
                        <td>
                          <Badge tone={effectiveState === "LIVE" ? "positive" : "warning"} dot>
                            {effectiveState}
                          </Badge>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </>
        )}

        {compatibleResponse !== null && compatibleResponse.missing_symbols.length > 0 ? (
          <p className="market-leader-missing">
            Awaiting quotes: {compatibleResponse.missing_symbols.join(", ")}
          </p>
        ) : null}
      </section>
    </div>
  );
}

function LeaderNotice({
  title,
  message,
  onRefresh,
}: {
  title: string;
  message: string;
  onRefresh: (() => void) | null;
}) {
  return (
    <div className="live-data-unavailable market-leader-empty" role="status">
      <div>
        <strong>{title}</strong>
        <p>{message}</p>
      </div>
      {onRefresh === null ? null : (
        <button className="text-button" onClick={onRefresh} type="button">
          Retry now
        </button>
      )}
    </div>
  );
}

function formatTimestamp(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "—";
  return new Intl.DateTimeFormat("en-IN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
    timeZone: "Asia/Kolkata",
  }).format(date);
}
