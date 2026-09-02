import { useEffect, useMemo, useState } from "react";

import {
  buildRiskTradePlan,
  parseRiskNumber,
  rankedLegKey,
  snapshotRiskDefaults,
} from "../../domain/risk";
import type { MarketSnapshot } from "../../domain/types";
import { PositionSizer, type RiskFormValues } from "./PositionSizer";
import { TradePlan } from "./TradePlan";

interface RiskWorkspaceProps {
  readonly snapshot: MarketSnapshot;
  readonly selectedLegKey: string;
  readonly onLegSelect: (key: string) => void;
}

function formValues(
  snapshot: MarketSnapshot,
  selectedLegKey: string,
): RiskFormValues {
  const defaults = snapshotRiskDefaults(snapshot, selectedLegKey);
  return {
    capital: Number.isFinite(defaults.capital) ? String(defaults.capital) : "",
    riskPercent: Number.isFinite(defaults.riskPercent)
      ? String(defaults.riskPercent)
      : "",
    allocationPercent: String(defaults.allocationPercent),
    stop: Number.isFinite(defaults.stop) ? String(defaults.stop) : "",
  };
}

export function RiskWorkspace({
  snapshot,
  selectedLegKey,
  onLegSelect,
}: RiskWorkspaceProps) {
  const selectedKeyIsValid = snapshot.ranking.some(
    (entry) => rankedLegKey(entry) === selectedLegKey,
  );
  const fallbackEntry = snapshot.ranking[0];
  const effectiveLegKey = selectedKeyIsValid
    ? selectedLegKey
    : fallbackEntry === undefined
      ? ""
      : rankedLegKey(fallbackEntry);
  const [values, setValues] = useState<RiskFormValues>(() =>
    formValues(snapshot, effectiveLegKey),
  );
  const importedCapital = snapshot.inputs.find(
    (input) => input.id === "capital",
  )?.effectiveValue;
  const importedRisk = snapshot.inputs.find(
    (input) => input.id === "risk_rate",
  )?.effectiveValue;

  useEffect(() => {
    setValues(formValues(snapshot, effectiveLegKey));
  }, [
    effectiveLegKey,
    importedCapital,
    importedRisk,
    snapshot.selection.expiry,
    snapshot.selection.market,
    snapshot.selection.symbol,
  ]);

  const evaluation = useMemo(
    () => buildRiskTradePlan(snapshot, effectiveLegKey, {
      capital: parseRiskNumber(values.capital),
      riskPercent: parseRiskNumber(values.riskPercent),
      allocationPercent: parseRiskNumber(values.allocationPercent),
      stop: parseRiskNumber(values.stop),
    }),
    [effectiveLegKey, snapshot, values],
  );

  const changeValue = (field: keyof RiskFormValues, value: string) => {
    setValues((current) => ({ ...current, [field]: value }));
  };

  return (
    <div className="risk-workspace">
      <header className="risk-workspace__intro">
        <div>
          <p className="risk-eyebrow">RISK WORKSPACE</p>
          <h1>Position sizing and trade plan</h1>
          <p>
            Size the selected ranked leg against capital, a hard risk ceiling,
            allocation, and estimated round-trip charges.
          </p>
        </div>
        <div className="risk-workspace__guardrail">
          <strong>Analysis only</strong>
          <span>No broker action is exposed</span>
        </div>
      </header>
      <div className="risk-workspace__grid">
        <PositionSizer
          entries={snapshot.ranking}
          evaluation={evaluation}
          onLegSelect={onLegSelect}
          onValueChange={changeValue}
          selectedLegKey={effectiveLegKey}
          values={values}
        />
        <TradePlan evaluation={evaluation} />
      </div>
    </div>
  );
}
