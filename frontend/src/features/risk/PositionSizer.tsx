import type { RankingEntry } from "../../domain/types";
import type {
  PositionSizingResult,
  RiskTradePlanEvaluation,
} from "../../domain/risk";
import { formatRiskMoney, rankedLegKey } from "../../domain/risk";
import { formatNumber, formatPrice } from "../../lib/format";

export interface RiskFormValues {
  readonly capital: string;
  readonly riskPercent: string;
  readonly allocationPercent: string;
  readonly stop: string;
}

interface PositionSizerProps {
  readonly evaluation: RiskTradePlanEvaluation;
  readonly entries: readonly RankingEntry[];
  readonly selectedLegKey: string;
  readonly values: RiskFormValues;
  readonly onLegSelect: (key: string) => void;
  readonly onValueChange: (field: keyof RiskFormValues, value: string) => void;
}

function SizingSummary({ sizing }: { sizing: PositionSizingResult | null }) {
  if (sizing === null) return null;
  if (!sizing.valid) {
    return (
      <div className="risk-errors" role="alert">
        <strong>Position size unavailable</strong>
        <ul>
          {sizing.errors.map((error) => <li key={error}>{error}</li>)}
        </ul>
      </div>
    );
  }
  return (
    <div className="risk-sizing-output" aria-live="polite">
      <div className="risk-lot-result">
        <span>Recommended size</span>
        <strong>{sizing.recommendedLots} lot{sizing.recommendedLots === 1 ? "" : "s"}</strong>
        <small>{sizing.recommended.quantity} units</small>
      </div>
      <dl className="risk-metrics">
        <div><dt>Risk limit</dt><dd>{formatRiskMoney(sizing.maximumRisk)}</dd></div>
        <div>
          <dt>Allocation limit</dt>
          <dd>{formatRiskMoney(sizing.maximumAllocation)}</dd>
        </div>
        <div><dt>Lots by risk</dt><dd>{sizing.lotsByRisk}</dd></div>
        <div><dt>Lots by allocation</dt><dd>{sizing.lotsByAllocation}</dd></div>
        <div>
          <dt>Estimated charges</dt>
          <dd>{formatRiskMoney(sizing.recommended.estimatedCharges)}</dd>
        </div>
        <div>
          <dt>Total risk incl. charges</dt>
          <dd>{formatRiskMoney(sizing.recommended.estimatedRisk)}</dd>
        </div>
      </dl>
      {sizing.affordabilityMessage === null ? null : (
        <div className="risk-affordability" role="status">
          <strong>TRADE NOT AFFORDABLE WITH CURRENT RISK LIMIT</strong>
          <p>{sizing.affordabilityMessage}</p>
        </div>
      )}
    </div>
  );
}

export function PositionSizer({
  evaluation,
  entries,
  selectedLegKey,
  values,
  onLegSelect,
  onValueChange,
}: PositionSizerProps) {
  const askEntry = evaluation.selected?.ranking.askEntry;
  return (
    <section className="risk-panel risk-position-sizer" aria-labelledby="position-sizer-title">
      <div className="risk-panel__header">
        <div>
          <p className="risk-eyebrow">CAPITAL CONTROL</p>
          <h2 id="position-sizer-title">Position sizer</h2>
          <p>Risk and premium affordability are both charge-aware.</p>
        </div>
        <span className="risk-cap-badge">2% hard cap</span>
      </div>

      <form className="risk-form" onSubmit={(event) => event.preventDefault()}>
        <label className="risk-field risk-field--wide" htmlFor="risk-contract">
          <span>Ranked contract</span>
          <select
            id="risk-contract"
            onChange={(event) => onLegSelect(event.currentTarget.value)}
            value={selectedLegKey}
          >
            {entries.map((entry) => (
              <option key={rankedLegKey(entry)} value={rankedLegKey(entry)}>
                #{entry.rank} · {formatNumber(entry.strike, 0)} {entry.side}
                {" · ask "}{formatPrice(entry.askEntry)}
              </option>
            ))}
          </select>
        </label>

        <label className="risk-field" htmlFor="risk-capital">
          <span>Account capital</span>
          <input
            id="risk-capital"
            inputMode="decimal"
            min="0.01"
            onChange={(event) => onValueChange("capital", event.currentTarget.value)}
            step="0.01"
            type="number"
            value={values.capital}
          />
        </label>
        <label className="risk-field" htmlFor="risk-percent">
          <span>Risk per trade (%)</span>
          <input
            aria-describedby="risk-percent-help"
            id="risk-percent"
            inputMode="decimal"
            max="2"
            min="0.01"
            onChange={(event) => onValueChange("riskPercent", event.currentTarget.value)}
            step="0.05"
            type="number"
            value={values.riskPercent}
          />
          <small id="risk-percent-help">Must not exceed 2.00%.</small>
        </label>
        <label className="risk-field" htmlFor="risk-allocation">
          <span>Premium allocation (%)</span>
          <input
            id="risk-allocation"
            inputMode="decimal"
            max="100"
            min="0.01"
            onChange={(event) => onValueChange("allocationPercent", event.currentTarget.value)}
            step="0.5"
            type="number"
            value={values.allocationPercent}
          />
        </label>
        <label className="risk-field" htmlFor="risk-stop">
          <span>Long-option stop</span>
          <input
            aria-describedby="risk-stop-help"
            id="risk-stop"
            inputMode="decimal"
            min="0.01"
            onChange={(event) => onValueChange("stop", event.currentTarget.value)}
            step="0.01"
            type="number"
            value={values.stop}
          />
          <small id="risk-stop-help">Must remain below the immutable ask entry.</small>
        </label>
      </form>

      <div className="risk-entry-reference">
        <span>Executable entry reference</span>
        <strong>{askEntry === undefined ? "Unavailable" : formatPrice(askEntry)}</strong>
        <small>Selected contract ask; LTP is not used for sizing.</small>
      </div>
      <SizingSummary sizing={evaluation.sizing} />
    </section>
  );
}
