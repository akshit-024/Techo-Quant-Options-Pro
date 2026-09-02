import type {
  MarketSnapshot,
  OptionLeg,
  RankingEntry,
} from "./types";

export const MAX_RISK_PERCENT = 2;

export interface ChargeSchedule {
  readonly fixedPerOrder: number;
  readonly entryNotionalRate: number;
  readonly exitNotionalRate: number;
}

export const DEFAULT_CHARGE_SCHEDULE: ChargeSchedule = Object.freeze({
  fixedPerOrder: 20,
  entryNotionalRate: 0.001,
  exitNotionalRate: 0.001,
});

export interface PositionSizingRequest {
  readonly capital: number;
  readonly riskPercent: number;
  readonly allocationPercent: number;
  readonly entryAsk: number;
  readonly stop: number;
  readonly lotSize: number;
  readonly charges?: Partial<ChargeSchedule>;
}

export interface PositionAmounts {
  readonly lots: number;
  readonly quantity: number;
  readonly entryPremium: number;
  readonly entryCharges: number;
  readonly estimatedExitCharges: number;
  readonly estimatedCharges: number;
  readonly capitalRequired: number;
  readonly estimatedRisk: number;
}

export interface InvalidPositionSizing {
  readonly valid: false;
  readonly errors: readonly string[];
}

export interface ValidPositionSizing {
  readonly valid: true;
  readonly errors: readonly [];
  readonly request: Omit<PositionSizingRequest, "charges">;
  readonly charges: ChargeSchedule;
  readonly maximumRisk: number;
  readonly maximumAllocation: number;
  readonly oneLot: PositionAmounts;
  readonly lotsByRisk: number;
  readonly lotsByAllocation: number;
  readonly recommendedLots: number;
  readonly recommended: PositionAmounts;
  readonly riskPerUnit: number;
  readonly targets: readonly [number, number, number];
  readonly affordabilityMessage: string | null;
}

export type PositionSizingResult =
  | InvalidPositionSizing
  | ValidPositionSizing;

export interface ResolvedRankedLeg {
  readonly key: string;
  readonly ranking: RankingEntry;
  readonly leg: OptionLeg;
}

export interface RiskControls {
  readonly capital: number;
  readonly riskPercent: number;
  readonly allocationPercent: number;
  readonly stop: number;
  readonly charges?: Partial<ChargeSchedule>;
}

export interface RiskTradePlanEvaluation {
  readonly selected: ResolvedRankedLeg | null;
  readonly sizing: PositionSizingResult | null;
  readonly dataMode: MarketSnapshot["dataMode"];
  readonly effectiveEventRisk: string | null;
  readonly decision: MarketSnapshot["analytics"]["decision"];
  readonly actionable: boolean;
  readonly blockers: readonly string[];
}

export interface SnapshotRiskDefaults {
  readonly capital: number;
  readonly riskPercent: number;
  readonly allocationPercent: number;
  readonly stop: number;
}

function roundMoney(value: number): number {
  return Math.round((value + Number.EPSILON) * 100) / 100;
}

function mergedCharges(
  override: Partial<ChargeSchedule> | undefined,
): ChargeSchedule {
  return { ...DEFAULT_CHARGE_SCHEDULE, ...override };
}

function validateRequest(
  request: PositionSizingRequest,
  charges: ChargeSchedule,
): readonly string[] {
  const errors: string[] = [];
  if (!Number.isFinite(request.capital) || request.capital <= 0) {
    errors.push("Account capital must be a positive finite amount.");
  }
  if (!Number.isFinite(request.riskPercent) || request.riskPercent <= 0) {
    errors.push("Risk per trade must be greater than 0%.");
  } else if (request.riskPercent > MAX_RISK_PERCENT) {
    errors.push("Risk per trade cannot exceed the 2.00% hard cap.");
  }
  if (
    !Number.isFinite(request.allocationPercent) ||
    request.allocationPercent <= 0 ||
    request.allocationPercent > 100
  ) {
    errors.push("Premium allocation must be greater than 0% and at most 100%.");
  }
  if (!Number.isFinite(request.entryAsk) || request.entryAsk <= 0) {
    errors.push("Ask entry must be a positive finite price.");
  }
  if (!Number.isFinite(request.stop) || request.stop <= 0) {
    errors.push("Stop price must be a positive finite price.");
  } else if (
    Number.isFinite(request.entryAsk) &&
    request.stop >= request.entryAsk
  ) {
    errors.push("A long-option stop must be below the ask entry price.");
  }
  if (
    !Number.isSafeInteger(request.lotSize) ||
    request.lotSize <= 0
  ) {
    errors.push("Verified lot size must be a positive integer.");
  }
  if (
    !Number.isFinite(charges.fixedPerOrder) ||
    charges.fixedPerOrder < 0 ||
    !Number.isFinite(charges.entryNotionalRate) ||
    charges.entryNotionalRate < 0 ||
    charges.entryNotionalRate >= 1 ||
    !Number.isFinite(charges.exitNotionalRate) ||
    charges.exitNotionalRate < 0 ||
    charges.exitNotionalRate >= 1
  ) {
    errors.push("Charge assumptions must be finite, non-negative, and below 100%.");
  }
  return errors;
}

function amountsForLots(
  lots: number,
  request: Omit<PositionSizingRequest, "charges">,
  charges: ChargeSchedule,
): PositionAmounts {
  if (lots === 0) {
    return {
      lots: 0,
      quantity: 0,
      entryPremium: 0,
      entryCharges: 0,
      estimatedExitCharges: 0,
      estimatedCharges: 0,
      capitalRequired: 0,
      estimatedRisk: 0,
    };
  }
  const quantity = lots * request.lotSize;
  const entryPremium = roundMoney(request.entryAsk * quantity);
  const estimatedExitNotional = roundMoney(request.stop * quantity);
  const entryCharges = roundMoney(
    charges.fixedPerOrder + entryPremium * charges.entryNotionalRate,
  );
  const estimatedExitCharges = roundMoney(
    charges.fixedPerOrder +
      estimatedExitNotional * charges.exitNotionalRate,
  );
  const estimatedCharges = roundMoney(entryCharges + estimatedExitCharges);
  return {
    lots,
    quantity,
    entryPremium,
    entryCharges,
    estimatedExitCharges,
    estimatedCharges,
    capitalRequired: roundMoney(entryPremium + estimatedCharges),
    estimatedRisk: roundMoney(
      (request.entryAsk - request.stop) * quantity + estimatedCharges,
    ),
  };
}

function maximumLotsFor(
  budget: number,
  basePerLot: number,
  request: Omit<PositionSizingRequest, "charges">,
  charges: ChargeSchedule,
  field: "estimatedRisk" | "capitalRequired",
): number {
  const safeQuantityLimit = Math.floor(
    Number.MAX_SAFE_INTEGER / request.lotSize,
  );
  let lower = 0;
  let upper = Math.min(
    Math.max(0, Math.floor(budget / basePerLot)),
    safeQuantityLimit,
  );
  let best = 0;
  while (lower <= upper) {
    const candidate = lower + Math.floor((upper - lower) / 2);
    const amount = amountsForLots(candidate, request, charges)[field];
    if (amount <= budget) {
      best = candidate;
      lower = candidate + 1;
    } else {
      upper = candidate - 1;
    }
  }
  return best;
}

export function formatRiskMoney(value: number): string {
  return `INR ${new Intl.NumberFormat("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value)}`;
}

export function calculatePositionSize(
  rawRequest: PositionSizingRequest,
): PositionSizingResult {
  const charges = mergedCharges(rawRequest.charges);
  const errors = validateRequest(rawRequest, charges);
  if (errors.length > 0) return { valid: false, errors };

  const request = {
    capital: rawRequest.capital,
    riskPercent: rawRequest.riskPercent,
    allocationPercent: rawRequest.allocationPercent,
    entryAsk: rawRequest.entryAsk,
    stop: rawRequest.stop,
    lotSize: rawRequest.lotSize,
  };
  const maximumRisk = roundMoney(
    request.capital * (request.riskPercent / 100),
  );
  const maximumAllocation = roundMoney(
    request.capital * (request.allocationPercent / 100),
  );
  const oneLot = amountsForLots(1, request, charges);
  const priceRiskPerLot =
    (request.entryAsk - request.stop) * request.lotSize;
  const entryPremiumPerLot = request.entryAsk * request.lotSize;
  const lotsByRisk = maximumLotsFor(
    maximumRisk,
    priceRiskPerLot,
    request,
    charges,
    "estimatedRisk",
  );
  const lotsByAllocation = maximumLotsFor(
    maximumAllocation,
    entryPremiumPerLot,
    request,
    charges,
    "capitalRequired",
  );
  const recommendedLots = Math.min(lotsByRisk, lotsByAllocation);
  const recommended = amountsForLots(recommendedLots, request, charges);
  const riskPerUnit = roundMoney(request.entryAsk - request.stop);
  const targets = [1, 2, 3].map((multiple) =>
    roundMoney(request.entryAsk + riskPerUnit * multiple),
  ) as [number, number, number];
  const affordabilityMessage =
    recommendedLots === 0
      ? [
          `0 lots: one lot requires ${formatRiskMoney(oneLot.capitalRequired)} allocation`,
          `and ${formatRiskMoney(oneLot.estimatedRisk)} risk capacity;`,
          `available ${formatRiskMoney(maximumAllocation)} allocation`,
          `and ${formatRiskMoney(maximumRisk)} risk.`,
        ].join(" ")
      : null;

  return {
    valid: true,
    errors: [],
    request,
    charges,
    maximumRisk,
    maximumAllocation,
    oneLot,
    lotsByRisk,
    lotsByAllocation,
    recommendedLots,
    recommended,
    riskPerUnit,
    targets,
    affordabilityMessage,
  };
}

export function rankedLegKey(entry: Pick<RankingEntry, "strike" | "side">): string {
  return `${entry.strike}:${entry.side}`;
}

export function resolveRankedLeg(
  snapshot: MarketSnapshot,
  selectedLegKey: string,
): ResolvedRankedLeg | null {
  const ranking = snapshot.ranking.find(
    (entry) => rankedLegKey(entry) === selectedLegKey,
  );
  if (ranking === undefined) return null;
  const row = snapshot.chain.find((candidate) => candidate.strike === ranking.strike);
  if (row === undefined) return null;
  return {
    key: selectedLegKey,
    ranking,
    leg: ranking.side === "CE" ? row.call : row.put,
  };
}

export function parseRiskNumber(value: string | undefined): number {
  if (value === undefined) return Number.NaN;
  const match = value.replaceAll(",", "").match(/[-+]?\d+(?:\.\d+)?/);
  if (match === null) return Number.NaN;
  return Number(match[0]);
}

function effectiveInput(snapshot: MarketSnapshot, id: string): string | undefined {
  return snapshot.inputs.find((input) => input.id === id)?.effectiveValue;
}

export function suggestedLongOptionStop(entryAsk: number): number {
  if (!Number.isFinite(entryAsk) || entryAsk <= 0.01) return Number.NaN;
  return Math.max(
    0.01,
    Math.min(
      Math.floor(entryAsk * 0.8 * 100) / 100,
      roundMoney(entryAsk - 0.01),
    ),
  );
}

export function snapshotRiskDefaults(
  snapshot: MarketSnapshot,
  selectedLegKey: string,
): SnapshotRiskDefaults {
  const selected = resolveRankedLeg(snapshot, selectedLegKey);
  return {
    capital: parseRiskNumber(effectiveInput(snapshot, "capital")),
    riskPercent: parseRiskNumber(effectiveInput(snapshot, "risk_rate")),
    allocationPercent: 25,
    stop: suggestedLongOptionStop(selected?.ranking.askEntry ?? Number.NaN),
  };
}

export function buildRiskTradePlan(
  snapshot: MarketSnapshot,
  selectedLegKey: string,
  controls: RiskControls,
): RiskTradePlanEvaluation {
  const selected = resolveRankedLeg(snapshot, selectedLegKey);
  const eventRisk = effectiveInput(snapshot, "event_risk")?.trim() ?? null;
  const blockers: string[] = [];
  if (selected === null) {
    blockers.push("Select a ranked contract before sizing a position.");
    return {
      selected,
      sizing: null,
      dataMode: snapshot.dataMode,
      effectiveEventRisk: eventRisk,
      decision: snapshot.analytics.decision,
      actionable: false,
      blockers,
    };
  }

  const sizing = calculatePositionSize({
    ...controls,
    entryAsk: selected.ranking.askEntry,
    lotSize: snapshot.definition.lotSize,
  });
  if (!sizing.valid) blockers.push(...sizing.errors);
  else if (sizing.recommendedLots === 0 && sizing.affordabilityMessage !== null) {
    blockers.push(sizing.affordabilityMessage);
  }

  if (snapshot.dataMode !== "LIVE") {
    blockers.push(
      `Live market data is required; current data mode is ${snapshot.dataMode}.`,
    );
  }
  if (eventRisk?.toUpperCase() !== "CLEAR") {
    blockers.push(
      `Event risk must be explicitly CLEAR; effective value is ${eventRisk ?? "MISSING"}.`,
    );
  }
  const requiredDecision = selected.ranking.side === "CE" ? "BUY CALL" : "BUY PUT";
  if (snapshot.analytics.decision !== requiredDecision) {
    blockers.push(
      `Decision ${snapshot.analytics.decision} does not authorize ${selected.ranking.side}.`,
    );
  }
  if (selected.ranking.rejectionReasons.length > 0) {
    blockers.push(
      `Selected contract is rejected: ${selected.ranking.rejectionReasons.join("; ")}.`,
    );
  }
  if (
    snapshot.backendAuthority !== undefined &&
    !snapshot.backendAuthority.actionable
  ) {
    blockers.push(
      snapshot.backendAuthority.blockers[0] ??
        "The backend has not authorized this snapshot as actionable.",
    );
  }

  return {
    selected,
    sizing,
    dataMode: snapshot.dataMode,
    effectiveEventRisk: eventRisk,
    decision: snapshot.analytics.decision,
    actionable: blockers.length === 0,
    blockers,
  };
}
