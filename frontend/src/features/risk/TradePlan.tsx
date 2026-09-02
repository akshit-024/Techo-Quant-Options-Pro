import type { RiskTradePlanEvaluation } from "../../domain/risk";
import { formatRiskMoney } from "../../domain/risk";
import { formatNumber, formatPrice } from "../../lib/format";

interface TradePlanProps {
  readonly evaluation: RiskTradePlanEvaluation;
}

export function TradePlan({ evaluation }: TradePlanProps) {
  const sizing = evaluation.sizing;
  const selected = evaluation.selected;
  const validSizing = sizing?.valid === true ? sizing : null;

  return (
    <section className="risk-panel risk-trade-plan" aria-labelledby="trade-plan-title">
      <div className="risk-panel__header">
        <div>
          <p className="risk-eyebrow">REVIEW BEFORE ACTION</p>
          <h2 id="trade-plan-title">Trade plan</h2>
          <p>Ask-based entry, explicit stop, and fixed R-multiple targets.</p>
        </div>
        <span
          className={`risk-status risk-status--${evaluation.actionable ? "ready" : "locked"}`}
          role="status"
        >
          {evaluation.actionable ? "Actionable analytical plan" : "Plan locked"}
        </span>
      </div>

      {selected === null ? (
        <p className="risk-empty">Select a ranked option contract to build a plan.</p>
      ) : (
        <>
          <div className="risk-contract-summary">
            <div>
              <span>Selected contract</span>
              <strong>
                {formatNumber(selected.ranking.strike, 0)} {selected.ranking.side}
              </strong>
              <small>Rank #{selected.ranking.rank} · {selected.leg.securityId}</small>
            </div>
            <div>
              <span>Decision</span>
              <strong>{evaluation.decision}</strong>
              <small>
                {evaluation.dataMode} data · event risk{" "}
                {evaluation.effectiveEventRisk ?? "missing"}
              </small>
            </div>
          </div>

          {validSizing === null ? null : (
            <dl className="risk-plan-grid">
              <div><dt>Entry ask</dt><dd>{formatPrice(validSizing.request.entryAsk)}</dd></div>
              <div><dt>Stop</dt><dd>{formatPrice(validSizing.request.stop)}</dd></div>
              <div><dt>1R target</dt><dd>{formatPrice(validSizing.targets[0])}</dd></div>
              <div><dt>2R target</dt><dd>{formatPrice(validSizing.targets[1])}</dd></div>
              <div><dt>3R target</dt><dd>{formatPrice(validSizing.targets[2])}</dd></div>
              <div><dt>Quantity</dt><dd>{validSizing.recommended.quantity}</dd></div>
              <div>
                <dt>Premium + charge reserve</dt>
                <dd>{formatRiskMoney(validSizing.recommended.capitalRequired)}</dd>
              </div>
              <div>
                <dt>Maximum estimated loss</dt>
                <dd>{formatRiskMoney(validSizing.recommended.estimatedRisk)}</dd>
              </div>
            </dl>
          )}
        </>
      )}

      {evaluation.blockers.length === 0 ? (
        <div className="risk-ready-note">
          All analytical gates are clear. Execution remains outside this interface.
        </div>
      ) : (
        <div className="risk-blockers">
          <strong>Actionability checks</strong>
          <ul>
            {evaluation.blockers.map((blocker) => <li key={blocker}>{blocker}</li>)}
          </ul>
        </div>
      )}
      <p className="risk-execution-note">
        This workspace creates a reviewable plan only. It cannot place an order.
      </p>
    </section>
  );
}
