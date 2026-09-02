import type { RiskTradePlanEvaluation } from "../../domain/risk";
import { formatRiskMoney } from "../../domain/risk";
import { formatNumber, formatPrice } from "../../lib/format";

interface RiskPlanSummaryProps {
  readonly evaluation: RiskTradePlanEvaluation;
}

export function RiskPlanSummary({ evaluation }: RiskPlanSummaryProps) {
  const selected = evaluation.selected;
  const sizing = evaluation.sizing?.valid === true ? evaluation.sizing : null;
  return (
    <section className="risk-plan-summary" aria-label="Risk plan summary">
      <div className="risk-plan-summary__header">
        <div>
          <span>Risk plan</span>
          <strong>
            {selected === null
              ? "No contract selected"
              : `${formatNumber(selected.ranking.strike, 0)} ${selected.ranking.side}`}
          </strong>
        </div>
        <span
          className={`risk-status risk-status--${evaluation.actionable ? "ready" : "locked"}`}
        >
          {evaluation.actionable ? "Ready" : "Locked"}
        </span>
      </div>
      {sizing === null ? (
        <p className="risk-plan-summary__message">
          {evaluation.blockers[0] ?? "Complete the position inputs to size this plan."}
        </p>
      ) : (
        <dl className="risk-plan-summary__metrics">
          <div><dt>Ask entry</dt><dd>{formatPrice(sizing.request.entryAsk)}</dd></div>
          <div>
            <dt>Size</dt>
            <dd>
              {sizing.recommendedLots} lot
              {sizing.recommendedLots === 1 ? "" : "s"}
            </dd>
          </div>
          <div>
            <dt>Max loss</dt>
            <dd>{formatRiskMoney(sizing.recommended.estimatedRisk)}</dd>
          </div>
          <div><dt>1R target</dt><dd>{formatPrice(sizing.targets[0])}</dd></div>
        </dl>
      )}
      {evaluation.actionable || sizing === null ? null : (
        <p className="risk-plan-summary__message">
          {evaluation.blockers[0] ?? "An actionability gate is not clear."}
        </p>
      )}
    </section>
  );
}
