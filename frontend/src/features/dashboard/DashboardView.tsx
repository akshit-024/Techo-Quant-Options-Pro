import type { OperationalGate } from "../../domain/gate";
import type { RiskTradePlanEvaluation } from "../../domain/risk";
import type { MarketSnapshot, ViewId } from "../../domain/types";
import { formatNumber, formatPrice } from "../../lib/format";
import { Badge } from "../../components/ui/Badge";
import { Icon } from "../../components/ui/Icon";
import { SectionHeader } from "../../components/ui/SectionHeader";
import { OperationalGateBanner } from "../../components/OperationalGateBanner";
import { MarketPulse } from "../analytics/MarketPulse";
import { ScoreGauge } from "../analytics/ScoreGauge";
import { OptionChainTable } from "../calculator/OptionChainTable";
import { RankingTable } from "../ranking/RankingTable";
import { RiskPlanSummary } from "../risk";

interface DashboardViewProps {
  snapshot: MarketSnapshot;
  operationalGate: OperationalGate;
  riskEvaluation: RiskTradePlanEvaluation;
  selectedLegKey: string;
  onLegSelect: (key: string) => void;
  onNavigate: (view: ViewId) => void;
}

function decisionTone(
  decision: MarketSnapshot["analytics"]["decision"],
): string {
  if (decision === "BUY CALL") return "call";
  if (decision === "BUY PUT") return "put";
  if (decision === "WAIT") return "wait";
  return "blocked";
}

export function DashboardView({
  snapshot,
  operationalGate,
  riskEvaluation,
  selectedLegKey,
  onLegSelect,
  onNavigate,
}: DashboardViewProps) {
  const { analytics } = snapshot;
  const eligibleRanking = snapshot.ranking.filter(
    (entry) => entry.rejectionReasons.length === 0,
  );
  const best = eligibleRanking[0] ?? null;

  const sourceDescription =
    snapshot.dataMode === "LIVE"
      ? "Validated live backend snapshot"
      : snapshot.dataMode === "STALE"
        ? "Retained backend snapshot — stale and non-actionable"
        : "Demo snapshot for interface development";

  const decisionLabel =
    snapshot.dataMode === "LIVE"
      ? "LIVE ANALYTICAL DECISION"
      : snapshot.dataMode === "STALE"
        ? "STALE ANALYTICAL DECISION"
        : "DEMO ANALYTICAL DECISION";

  return (
    <div className="page-stack dashboard-page">
      <div className="dashboard-heading">
        <div>
          <p className="eyebrow">DECISION WORKSPACE</p>
          <h1>Market evidence at a glance.</h1>
          <p>
            {snapshot.definition.label} · {snapshot.selection.expiry} ·{" "}
            {sourceDescription}
          </p>
        </div>

        <div className="dashboard-heading__legend">
          <Badge tone="positive">
            <i className="legend-strong" /> 85+ Strong
          </Badge>
          <Badge tone="info">
            <i className="legend-tradable" /> 75–84 Tradable
          </Badge>
          <Badge tone="warning">
            <i className="legend-watch" /> 65–74 Watch
          </Badge>
        </div>
      </div>

      <OperationalGateBanner
        gate={operationalGate}
        onOpenStatus={() => onNavigate("api_status")}
      />

      <section
        className={`decision-hero decision-hero--${decisionTone(
          analytics.decision,
        )}`}
      >
        <div className="decision-hero__glow" />

        <div className="decision-hero__copy">
          <div className="decision-hero__status">
            <span className="decision-pulse" />
            {decisionLabel}
          </div>

          <h2>{analytics.decision}</h2>
          <p>{analytics.decisionReason}</p>

          <div className="decision-hero__safety">
            <Icon name="lock" size={14} /> Execution is locked; this screen
            cannot place an order.
          </div>
        </div>

        <div className="decision-hero__scores">
          <ScoreGauge
            label="Call score"
            score={analytics.callScore}
            side="call"
          />

          <div className="score-divider">
            <span>GAP</span>
            <strong>{formatNumber(analytics.signalGap, 1)}</strong>
            <small>Min. 8</small>
          </div>

          <ScoreGauge
            label="Put score"
            score={analytics.putScore}
            side="put"
          />
        </div>

        <div className="decision-hero__leader">
          <span>
            {snapshot.dataMode === "STALE"
              ? "LAST RANKED LEG"
              : snapshot.dataMode === "DEMO"
                ? "DEMO TOP LEG"
                : "TOP ELIGIBLE LEG"}
          </span>

          {best === null ? (
            <>
              <div><strong>No eligible leg</strong></div>
              <p>Every current contract is blocked by one or more validation gates.</p>
              <button onClick={() => onNavigate("ranking")} type="button">
                Review rejections <Icon name="chevron" size={14} />
              </button>
            </>
          ) : (
            <>
              <div>
                <strong>{best.contractName}</strong>
              </div>

              <p>
                {snapshot.dataMode === "STALE" ? "Last ask reference" : "Entry reference"}{" "}
                <strong>{formatPrice(best.askEntry)}</strong> ask
              </p>

              <button
                onClick={() => {
                  onLegSelect(`${best.strike}:${best.side}`);
                  onNavigate("ranking");
                }}
                type="button"
              >
                Inspect ranking <Icon name="chevron" size={14} />
              </button>
            </>
          )}
        </div>
      </section>

      <div className="metric-strip">
        <article>
          <span>
            <Icon name="activity" size={15} /> Expected move
          </span>
          <strong>± {formatNumber(analytics.expectedMove)}</strong>
          <small>
            {formatNumber(analytics.expectedLow)} —{" "}
            {formatNumber(analytics.expectedHigh)}
          </small>
        </article>

        <article>
          <span>
            <Icon name="pulse" size={15} /> Trend
          </span>
          <strong>{analytics.trend}</strong>
          <small>{analytics.trendStrength}/100 strength</small>
        </article>

        <article>
          <span>
            <Icon name="database" size={15} /> OI PCR
          </span>
          <strong>{formatNumber(analytics.pcr)}</strong>
          <small>
            Change-OI {formatNumber(analytics.changeOiPcr)}
          </small>
        </article>

        <article>
          <span>
            <Icon name="greeks" size={15} /> ATM IV
          </span>
          <strong>{formatNumber(analytics.atmIv, 1)}%</strong>
          <small>CE/PE independently sourced</small>
        </article>
      </div>

      <MarketPulse snapshot={snapshot} />

      <div className="dashboard-risk-row">
        <RiskPlanSummary evaluation={riskEvaluation} />

        <article className="dashboard-risk-callout">
          <div className="dashboard-risk-callout__icon">
            <Icon name="target" size={19} />
          </div>

          <div>
            <p className="eyebrow">CAPITAL BEFORE CONVICTION</p>
            <h3>Review the complete sizing model</h3>
            <p>
              Charges, the 2% hard ceiling, premium allocation and whole-lot
              affordability are applied before a plan can become actionable.
            </p>
          </div>

          <button
            className="text-button"
            onClick={() => onNavigate("position_sizer")}
            type="button"
          >
            Open risk workspace <Icon name="chevron" size={14} />
          </button>
        </article>
      </div>

      <section className="panel dashboard-ranking-panel">
        <SectionHeader
          eyebrow="Best eligible contracts"
          title="Strike ranking preview"
          description="Up to five currently eligible option legs, ordered by the backend score."
          action={
            <button
              className="text-button"
              onClick={() => onNavigate("ranking")}
              type="button"
            >
              Open all 10 <Icon name="chevron" size={14} />
            </button>
          }
        />

        <RankingTable
          entries={eligibleRanking}
          limit={5}
          onSelect={onLegSelect}
          selectedLegKey={selectedLegKey}
          symbol={snapshot.selection.symbol}
          dataMode={snapshot.dataMode}
        />
      </section>

      <OptionChainTable
        chain={snapshot.chain}
        dataMode={snapshot.dataMode}
        compact
        onLegSelect={onLegSelect}
        presentationMode="QUICK"
        selectedLegKey={selectedLegKey}
      />

      <div className="dashboard-disclaimer">
        <Icon name="shield" size={18} />

        <p>
          <strong>Data boundary.</strong>{" "}
          {snapshot.dataMode === "DEMO"
            ? "These values are deterministic demo data, explicitly labelled and gated to NO TRADE."
            : snapshot.dataMode === "STALE"
              ? "This is the last validated backend snapshot, retained for visibility but blocked from action because it is stale."
              : "These values came from the validated backend market workspace; backend actionability remains authoritative."}{" "}
          This interface cannot place an order.
        </p>
      </div>
    </div>
  );
}
