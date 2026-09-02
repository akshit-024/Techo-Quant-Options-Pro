import { decisionFromScores, scoreBand } from "../domain/score";
import type {
  CalculatorInput,
  MarketDefinition,
  MarketSnapshot,
  OptionLeg,
  OptionStrike,
  RankingEntry,
  WorkspaceSelection,
} from "../domain/types";
import { definitionFor } from "./marketDefinitions";

export type ManualOverrides = Readonly<Record<string, string>>;

const SYMBOL_PROFILE: Record<
  string,
  Partial<Pick<MarketDefinition, "baseSpot" | "strikeStep" | "lotSize">>
> = {
  RELIANCE: { baseSpot: 1_412.6, strikeStep: 20, lotSize: 500 },
  TCS: { baseSpot: 3_185.45, strikeStep: 50, lotSize: 175 },
  INFY: { baseSpot: 1_536.8, strikeStep: 20, lotSize: 400 },
  GOLD: { baseSpot: 102_480, strikeStep: 500, lotSize: 1 },
  CRUDEOIL: { baseSpot: 6_462, strikeStep: 50, lotSize: 100 },
  SILVER: { baseSpot: 118_240, strikeStep: 1_000, lotSize: 30 },
};

const BIAS: Record<string, number> = {
  NIFTY: 7,
  BANKNIFTY: -5,
  SENSEX: 2,
  RELIANCE: 8,
  TCS: -7,
  INFY: 4,
  GOLD: -8,
  CRUDEOIL: 6,
  SILVER: -3,
};

function withSymbolProfile(
  definition: MarketDefinition,
  symbol: string,
): MarketDefinition {
  const profile = SYMBOL_PROFILE[symbol];
  return profile === undefined ? definition : { ...definition, ...profile };
}

function roundTo(value: number, digits = 2): number {
  const factor = 10 ** digits;
  return Math.round(value * factor) / factor;
}

function numericOverride(
  rawValue: string | undefined,
  fallback: number,
): { value: number; valid: boolean } {
  if (rawValue === undefined || !rawValue.trim()) return { value: fallback, valid: true };
  const parsed = Number(rawValue.replace(/[₹,%\s]/g, ""));
  return Number.isFinite(parsed) && parsed > 0
    ? { value: parsed, valid: true }
    : { value: fallback, valid: false };
}

function input(
  id: string,
  label: string,
  group: CalculatorInput["group"],
  importedValue: string,
  overrides: ManualOverrides,
  helper?: string,
): CalculatorInput {
  const manualOverride = overrides[id]?.trim();
  return {
    id,
    label,
    group,
    importedValue,
    manualOverride: manualOverride || undefined,
    effectiveValue: manualOverride || importedValue,
    source: manualOverride ? "MANUAL OVERRIDE" : "DEMO FEED",
    helper,
  };
}

function computedInput(
  id: string,
  label: string,
  group: CalculatorInput["group"],
  value: string,
  helper?: string,
): CalculatorInput {
  return {
    id,
    label,
    group,
    importedValue: "—",
    effectiveValue: value,
    source: "COMPUTED",
    helper,
  };
}

function makeLeg(args: {
  side: "CE" | "PE";
  strike: number;
  spot: number;
  step: number;
  offset: number;
  bias: number;
  rowIndex: number;
}): OptionLeg {
  const { side, strike, spot, step, offset, bias, rowIndex } = args;
  const intrinsic =
    side === "CE" ? Math.max(0, spot - strike) : Math.max(0, strike - spot);
  const baseTimeValue = Math.max(step * 0.7, spot * 0.0065);
  const timeValue = baseTimeValue * (1 - Math.abs(offset) * 0.09);
  const ltp = roundTo(intrinsic + timeValue + (side === "CE" ? bias : -bias) * 0.35);
  const spreadPercent = roundTo(0.65 + Math.abs(offset) * 0.42 + (rowIndex % 2) * 0.13);
  const spread = Math.max(0.1, ltp * (spreadPercent / 100));
  const bid = roundTo(Math.max(0.05, ltp - spread / 2));
  const ask = roundTo(ltp + spread / 2);
  const callDeltas = [0.69, 0.6, 0.51, 0.42, 0.34];
  const putDeltas = [-0.31, -0.4, -0.49, -0.58, -0.66];
  const delta = side === "CE" ? callDeltas[rowIndex] : putDeltas[rowIndex];
  const liquidityScore = roundTo(
    Math.max(42, 94 - Math.abs(offset) * 8 - spreadPercent * 4 + (side === "CE" ? 2 : -1)),
  );
  const shapeScore = [76, 84, 89, 85, 74][rowIndex];
  const directionAdjustment = side === "CE" ? bias : -bias;
  const strikeScore = roundTo(
    Math.max(42, Math.min(96, shapeScore + directionAdjustment - spreadPercent)),
  );
  const rejectionReasons: string[] = [];
  if (spreadPercent > 2.25) rejectionReasons.push("Wide spread");
  if (liquidityScore < 60) rejectionReasons.push("Weak liquidity");
  if (Math.abs(delta) < 0.35) rejectionReasons.push("Delta below 0.35");

  return {
    side,
    securityId: `DEMO-${side}-${Math.round(strike)}`,
    bid,
    ask,
    ltp,
    volume: Math.round(148_000 - Math.abs(offset) * 17_500 + (side === "CE" ? 8_500 : 0)),
    openInterest: Math.round(242_000 + (side === "CE" ? -offset : offset) * 21_000),
    changeOpenInterest: Math.round((side === "CE" ? 1 : -1) * (7_800 - offset * 1_450) + bias * 260),
    iv: roundTo(17.4 + Math.abs(offset) * 0.85 + (side === "PE" ? 0.9 : 0) - bias * 0.05),
    spreadPercent,
    liquidityScore,
    strikeScore,
    greeks: {
      delta,
      gamma: roundTo((0.00046 - Math.abs(offset) * 0.000035) * (25_000 / spot), 6),
      theta: roundTo(-(11.8 + (2 - Math.abs(offset)) * 0.9 + (side === "PE" ? 0.4 : 0))),
      vega: roundTo(17.2 - Math.abs(offset) * 1.35),
      theoreticalPrice: roundTo(ltp * (0.985 + (side === "CE" ? 0.006 : -0.003))),
    },
    rejectionReasons,
  };
}

function makeChain(
  definition: MarketDefinition,
  spot: number,
  bias: number,
): readonly OptionStrike[] {
  const atm = Math.round(spot / definition.strikeStep) * definition.strikeStep;
  const labels = ["ATM−2", "ATM−1", "ATM", "ATM+1", "ATM+2"] as const;
  return [-2, -1, 0, 1, 2].map((offset, rowIndex) => {
    const strike = atm + offset * definition.strikeStep;
    return {
      strike,
      moneyness: labels[rowIndex],
      call: makeLeg({
        side: "CE",
        strike,
        spot,
        step: definition.strikeStep,
        offset,
        bias,
        rowIndex,
      }),
      put: makeLeg({
        side: "PE",
        strike,
        spot,
        step: definition.strikeStep,
        offset,
        bias,
        rowIndex,
      }),
    };
  });
}

function makeRanking(chain: readonly OptionStrike[]): readonly RankingEntry[] {
  const candidates = chain.flatMap((row) =>
    [row.call, row.put].map((leg) => ({
      side: leg.side,
      strike: row.strike,
      score: leg.strikeScore,
      band: scoreBand(leg.strikeScore),
      askEntry: leg.ask,
      bidExit: leg.bid,
      spreadPercent: leg.spreadPercent,
      liquidityScore: leg.liquidityScore,
      delta: leg.greeks.delta,
      rejectionReasons: leg.rejectionReasons,
    })),
  );

  return candidates
    .sort((left, right) => {
      const eligibility = Number(left.rejectionReasons.length > 0) - Number(right.rejectionReasons.length > 0);
      return eligibility || right.score - left.score;
    })
    .map((entry, index) => ({ ...entry, rank: index + 1 }));
}

function buildInputs(args: {
  selection: WorkspaceSelection;
  definition: MarketDefinition;
  importedSpot: number;
  importedFutures: number;
  atm: number;
  bias: number;
  overrides: ManualOverrides;
}): readonly CalculatorInput[] {
  const { selection, definition, importedSpot, importedFutures, atm, bias, overrides } = args;
  const vwap = importedSpot - bias * 2.1;
  const atr = definition.strikeStep * 1.16;
  const rsi = roundTo(52 + bias * 1.8, 1);

  return [
    input("mode", "Operating mode", "SESSION", "PRO", overrides, "Quick / Pro presentation mode"),
    input("symbol", "Symbol", "SESSION", selection.symbol, overrides),
    input("option_expiry", "Option expiry", "SESSION", selection.expiry, overrides),
    input("market_timestamp", "Market timestamp", "SESSION", "21 Aug 2026 · 11:42:08 IST", overrides),
    input("spot", "Spot price", "MARKET", importedSpot.toFixed(2), overrides, "Generated demo underlying quote"),
    input("futures", "Exact futures", "MARKET", importedFutures.toFixed(2), overrides, "Generated demo futures quote"),
    input("ohlc", "15m OHLC", "MARKET", `${roundTo(importedSpot - 42)} / ${roundTo(importedSpot + 68)} / ${roundTo(importedSpot - 74)} / ${roundTo(importedSpot)}`, overrides),
    input("vwap", "VWAP", "TECHNICAL", vwap.toFixed(2), overrides),
    input("ema20", "EMA 20", "TECHNICAL", (importedSpot - bias * 3.8).toFixed(2), overrides),
    input("wma50", "WMA 50", "TECHNICAL", (importedSpot - bias * 8.4).toFixed(2), overrides),
    input("rsi14", "RSI 14", "TECHNICAL", rsi.toFixed(1), overrides),
    input("atr14", "ATR 14", "TECHNICAL", atr.toFixed(2), overrides),
    computedInput("atm", "ATM strike", "MARKET", atm.toFixed(0), `Rounded to ${definition.strikeStep}-point interval`),
    input("capital", "Account capital", "RISK", "₹5,00,000", overrides),
    input("risk_rate", "Risk per trade", "RISK", "1.00%", overrides),
    input("style", "Trading style", "RISK", "INTRADAY", overrides),
    input("event_risk", "Event risk", "RISK", "CLEAR", overrides),
    input("signal_candle", "Signal candle H / L", "TECHNICAL", `${roundTo(importedSpot + 31)} / ${roundTo(importedSpot - 36)}`, overrides),
    input("lot_size", "Demo lot size", "RISK", definition.lotSize.toString(), overrides),
  ];
}

export function buildDemoSnapshot(
  selection: WorkspaceSelection,
  overrides: ManualOverrides = {},
): MarketSnapshot {
  const definition = withSymbolProfile(definitionFor(selection.market), selection.symbol);
  const baseBias = BIAS[selection.symbol] ?? BIAS[selection.market] ?? 0;
  const importedSpot = roundTo(definition.baseSpot + baseBias * definition.strikeStep * 0.08);
  const importedFutures = roundTo(importedSpot + definition.strikeStep * (0.22 + baseBias * 0.008));
  const importedVwap = importedSpot - baseBias * 2.1;
  const importedRsi = roundTo(52 + baseBias * 1.8, 1);
  const spotOverride = numericOverride(overrides.spot, importedSpot);
  const futuresOverride = numericOverride(overrides.futures, importedFutures);
  const vwapOverride = numericOverride(overrides.vwap, importedVwap);
  const rsiOverride = numericOverride(overrides.rsi14, importedRsi);
  const spot = roundTo(spotOverride.value);
  const hasDirectionalOverride = overrides.spot !== undefined || overrides.vwap !== undefined || overrides.rsi14 !== undefined;
  const derivedBias =
    (spot - vwapOverride.value) / Math.max(definition.strikeStep, 1) * 3 +
    (rsiOverride.value - 50) / 3;
  const bias = hasDirectionalOverride
    ? Math.max(-10, Math.min(10, roundTo(baseBias * 0.35 + derivedBias, 1)))
    : baseBias;
  const invalidOverride = !spotOverride.valid || !futuresOverride.valid || !vwapOverride.valid || !rsiOverride.valid;
  const eventRiskValue = overrides.event_risk?.trim().toUpperCase() ?? "CLEAR";
  const eventRiskActive = !["CLEAR", "NO", "FALSE", "0"].includes(eventRiskValue);
  const atm = Math.round(spot / definition.strikeStep) * definition.strikeStep;
  const chain = makeChain(definition, spot, bias);
  const ranking = makeRanking(chain);
  const callScore = Math.max(...chain.map((row) => row.call.strikeScore));
  const putScore = Math.max(...chain.map((row) => row.put.strikeScore));
  const scoreDecision = decisionFromScores(callScore, putScore);
  const decision = invalidOverride
    ? { decision: "INSUFFICIENT DATA" as const, reason: "A numeric manual override is invalid, so the demo fails closed." }
    : eventRiskActive
      ? { decision: "NO TRADE" as const, reason: "The manual event-risk gate is active." }
      : scoreDecision;
  const expectedMove = roundTo(spot * 0.0091);
  const trend = bias > 3 ? "BULLISH" : bias < -3 ? "BEARISH" : "MIXED";
  const spotSeries = [-0.48, -0.27, -0.31, -0.08, 0.05, -0.01, 0.24, 0.19, 0.38, 0.5, 0.44, 0.68].map(
    (move, index) => roundTo(spot + move * definition.strikeStep + bias * index * 0.09),
  );

  return {
    selection,
    definition,
    capturedAt: "2026-08-21T11:42:08+05:30",
    dataMode: "DEMO",
    inputs: buildInputs({
      selection,
      definition,
      importedSpot,
      importedFutures,
      atm,
      bias: baseBias,
      overrides,
    }),
    chain,
    ranking,
    analytics: {
      expectedMove,
      expectedLow: roundTo(spot - expectedMove),
      expectedHigh: roundTo(spot + expectedMove),
      syntheticFutures: roundTo(spot + chain[2].call.ltp - chain[2].put.ltp),
      trend,
      trendStrength: Math.min(96, 58 + Math.abs(bias) * 4),
      pcr: roundTo(0.92 - bias * 0.018),
      changeOiPcr: roundTo(1.04 - bias * 0.024),
      support: atm - definition.strikeStep * 2,
      resistance: atm + definition.strikeStep * 2,
      atmIv: roundTo((chain[2].call.iv + chain[2].put.iv) / 2),
      callScore,
      putScore,
      decision: decision.decision,
      decisionReason: decision.reason,
      signalGap: roundTo(Math.abs(callScore - putScore)),
      spotSeries,
    },
  };
}
