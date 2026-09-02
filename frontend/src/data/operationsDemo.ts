import { decisionFromScores, scoreBand } from "../domain/score";
import type { Decision, MarketSnapshot } from "../domain/types";

export type DemoRecordSource = "DEMO";
export type SignalRecordState = "ACTIONABLE" | "WAIT" | "BLOCKED";
export type TimelineState = "SUCCESS" | "WARNING" | "BLOCKED";
export type JournalOutcome = "WIN" | "LOSS" | "FLAT" | "OPEN";
export type JournalFilter = "ALL" | JournalOutcome;
export type ExecutionModeId =
  | "OFF"
  | "DATA_ONLY"
  | "PAPER_TRADING"
  | "MANUAL_APPROVAL"
  | "LIVE_AUTOMATIC";
export type AuditStatus = "PASS" | "WARN" | "FAIL";

export interface SignalHistoryRow {
  id: string;
  capturedAt: string;
  market: string;
  symbol: string;
  expiry: string;
  decision: Decision;
  callScore: number;
  putScore: number;
  selectedContract: string | null;
  state: SignalRecordState;
  reason: string;
  source: DemoRecordSource;
}

export interface AutomationEvent {
  id: string;
  occurredAt: string;
  title: string;
  detail: string;
  state: TimelineState;
  source: DemoRecordSource;
}

export interface JournalTrade {
  id: string;
  market: string;
  contract: string;
  side: "CE" | "PE";
  quantity: number;
  enteredAt: string;
  exitedAt: string | null;
  entryPrice: number;
  exitPrice: number | null;
  realizedPnl: number | null;
  rMultiple: number | null;
  outcome: JournalOutcome;
  exitReason: string;
  source: DemoRecordSource;
}

export interface BacktestMetric {
  id: string;
  label: string;
  value: string;
  detail: string;
  tone: "POSITIVE" | "NEUTRAL" | "WARNING";
}

export interface EquityPoint {
  observation: number;
  label: string;
  equity: number;
  drawdownPercent: number;
}

export interface BacktestReport {
  title: string;
  period: string;
  market: string;
  timeframe: string;
  sample: string;
  generatedAt: string;
  source: DemoRecordSource;
  metrics: readonly BacktestMetric[];
  equity: readonly EquityPoint[];
  trades: readonly JournalTrade[];
}

export interface GuardrailSetting {
  id: string;
  label: string;
  value: string;
  description: string;
  locked: boolean;
}

export interface ExecutionModeOption {
  id: ExecutionModeId;
  label: string;
  description: string;
  selected: boolean;
  disabled: boolean;
}

export interface ContractMasterRow {
  market: string;
  symbol: string;
  exchange: "NSE" | "BSE" | "MCX";
  segment: string;
  securityId: string;
  instrumentClass: string;
  expiry: string;
  lotSize: number;
  strikeInterval: number;
  tickSize: number;
  pricingModel: "BLACK_SCHOLES" | "BLACK_76";
  status: "VERIFIED_DEMO" | "EXPIRY_REVIEW";
  source: DemoRecordSource;
}

export interface GuideSection {
  id: string;
  step: string;
  title: string;
  description: string;
  checks: readonly string[];
  destination: string;
}

export interface FormulaAuditCheck {
  id: string;
  category: "IDENTITY" | "COMPLETENESS" | "PRICING" | "SCORING" | "PROVENANCE";
  label: string;
  status: AuditStatus;
  evidence: string;
}

export const DEMO_SIGNAL_HISTORY = [
  {
    id: "SIG-DEMO-1042",
    capturedAt: "2026-08-21T11:42:08+05:30",
    market: "NIFTY",
    symbol: "NIFTY",
    expiry: "27 Aug 2026",
    decision: "BUY CALL",
    callScore: 88.4,
    putScore: 68.1,
    selectedContract: "24,850 CE",
    state: "ACTIONABLE",
    reason: "Call evidence cleared the score gap and liquidity gates.",
    source: "DEMO",
  },
  {
    id: "SIG-DEMO-1041",
    capturedAt: "2026-08-21T11:27:08+05:30",
    market: "BANKNIFTY",
    symbol: "BANKNIFTY",
    expiry: "27 Aug 2026",
    decision: "WAIT",
    callScore: 76.2,
    putScore: 72.8,
    selectedContract: null,
    state: "WAIT",
    reason: "The directional score gap remained below eight points.",
    source: "DEMO",
  },
  {
    id: "SIG-DEMO-1040",
    capturedAt: "2026-08-21T11:12:08+05:30",
    market: "MCX",
    symbol: "GOLD",
    expiry: "04 Sep 2026",
    decision: "NO TRADE",
    callScore: 62.7,
    putScore: 71.9,
    selectedContract: null,
    state: "BLOCKED",
    reason: "The leading leg remained in the watchlist band.",
    source: "DEMO",
  },
  {
    id: "SIG-DEMO-1039",
    capturedAt: "2026-08-21T10:57:08+05:30",
    market: "STOCK F&O",
    symbol: "RELIANCE",
    expiry: "27 Aug 2026",
    decision: "BUY PUT",
    callScore: 66.5,
    putScore: 85.7,
    selectedContract: "1,400 PE",
    state: "ACTIONABLE",
    reason: "Put evidence led with an eligible spread and volume profile.",
    source: "DEMO",
  },
] as const satisfies readonly SignalHistoryRow[];

export const DEMO_AUTOMATION_TIMELINE = [
  {
    id: "EVT-DEMO-01",
    occurredAt: "11:42:08",
    title: "Snapshot assembled",
    detail: "Five strikes and ten option legs normalized for the selected contract.",
    state: "SUCCESS",
    source: "DEMO",
  },
  {
    id: "EVT-DEMO-02",
    occurredAt: "11:42:08",
    title: "Completeness evaluated",
    detail: "Bid, ask, LTP, OI, volume, IV, identity, and expiry fields inspected.",
    state: "SUCCESS",
    source: "DEMO",
  },
  {
    id: "EVT-DEMO-03",
    occurredAt: "11:42:09",
    title: "Ranking generated",
    detail: "Eligible contracts ranked ahead of legs carrying rejection reasons.",
    state: "SUCCESS",
    source: "DEMO",
  },
  {
    id: "EVT-DEMO-04",
    occurredAt: "11:42:09",
    title: "Execution boundary held",
    detail: "The browser recorded no approval and submitted no broker mutation.",
    state: "BLOCKED",
    source: "DEMO",
  },
] as const satisfies readonly AutomationEvent[];

export const DEMO_JOURNAL_TRADES = [
  {
    id: "TRD-DEMO-031",
    market: "NIFTY",
    contract: "24,850 CE",
    side: "CE",
    quantity: 75,
    enteredAt: "2026-08-20T10:05:00+05:30",
    exitedAt: "2026-08-20T11:18:00+05:30",
    entryPrice: 182.4,
    exitPrice: 211.75,
    realizedPnl: 2201.25,
    rMultiple: 1.42,
    outcome: "WIN",
    exitReason: "Target 2",
    source: "DEMO",
  },
  {
    id: "TRD-DEMO-030",
    market: "BANKNIFTY",
    contract: "53,400 PE",
    side: "PE",
    quantity: 30,
    enteredAt: "2026-08-19T12:12:00+05:30",
    exitedAt: "2026-08-19T12:46:00+05:30",
    entryPrice: 246.1,
    exitPrice: 229.65,
    realizedPnl: -493.5,
    rMultiple: -0.61,
    outcome: "LOSS",
    exitReason: "Stop loss",
    source: "DEMO",
  },
  {
    id: "TRD-DEMO-029",
    market: "MCX",
    contract: "102,500 CE",
    side: "CE",
    quantity: 1,
    enteredAt: "2026-08-18T16:08:00+05:30",
    exitedAt: "2026-08-18T18:02:00+05:30",
    entryPrice: 1248.5,
    exitPrice: 1391.2,
    realizedPnl: 142.7,
    rMultiple: 0.94,
    outcome: "WIN",
    exitReason: "Trailing stop",
    source: "DEMO",
  },
  {
    id: "TRD-DEMO-028",
    market: "SENSEX",
    contract: "81,300 PE",
    side: "PE",
    quantity: 20,
    enteredAt: "2026-08-18T11:04:00+05:30",
    exitedAt: "2026-08-18T11:39:00+05:30",
    entryPrice: 318.25,
    exitPrice: 318.05,
    realizedPnl: -4,
    rMultiple: 0,
    outcome: "FLAT",
    exitReason: "Time stop",
    source: "DEMO",
  },
  {
    id: "TRD-DEMO-027",
    market: "STOCK F&O",
    contract: "RELIANCE 1,420 CE",
    side: "CE",
    quantity: 500,
    enteredAt: "2026-08-21T11:35:00+05:30",
    exitedAt: null,
    entryPrice: 24.3,
    exitPrice: null,
    realizedPnl: null,
    rMultiple: null,
    outcome: "OPEN",
    exitReason: "Paper position open",
    source: "DEMO",
  },
] as const satisfies readonly JournalTrade[];

export const DEMO_BACKTEST_REPORT: BacktestReport = {
  title: "NIFTY intraday call/put selection",
  period: "01 Apr 2026 - 31 Jul 2026",
  market: "NIFTY",
  timeframe: "5-minute decisions / 1-minute execution",
  sample: "Out-of-sample demonstration",
  generatedAt: "2026-08-21T09:30:00+05:30",
  source: "DEMO",
  metrics: [
    { id: "trades", label: "Total trades", value: "84", detail: "42 CE / 42 PE", tone: "NEUTRAL" },
    { id: "win-rate", label: "Win rate", value: "54.8%", detail: "46 wins", tone: "POSITIVE" },
    { id: "profit-factor", label: "Profit factor", value: "1.47", detail: "After demo costs", tone: "POSITIVE" },
    { id: "expectancy", label: "Expectancy", value: "+0.21 R", detail: "Per closed trade", tone: "POSITIVE" },
    { id: "drawdown", label: "Maximum drawdown", value: "-6.3%", detail: "Peak to trough", tone: "WARNING" },
    { id: "slippage", label: "Slippage impact", value: "-1.8%", detail: "Ask entry / bid exit", tone: "WARNING" },
  ],
  equity: [
    { observation: 0, label: "Apr 01", equity: 500000, drawdownPercent: 0 },
    { observation: 1, label: "Apr 15", equity: 506400, drawdownPercent: -0.8 },
    { observation: 2, label: "May 01", equity: 514900, drawdownPercent: -0.3 },
    { observation: 3, label: "May 15", equity: 508700, drawdownPercent: -2.1 },
    { observation: 4, label: "Jun 01", equity: 522800, drawdownPercent: 0 },
    { observation: 5, label: "Jun 15", equity: 529600, drawdownPercent: -0.5 },
    { observation: 6, label: "Jul 01", equity: 518900, drawdownPercent: -3.4 },
    { observation: 7, label: "Jul 15", equity: 536200, drawdownPercent: -0.7 },
    { observation: 8, label: "Jul 31", equity: 548750, drawdownPercent: 0 },
  ],
  trades: DEMO_JOURNAL_TRADES.filter((trade) => trade.outcome !== "OPEN"),
};

export const DEMO_EXECUTION_MODES = [
  { id: "OFF", label: "Off", description: "No execution requests are accepted.", selected: false, disabled: false },
  { id: "DATA_ONLY", label: "Data only", description: "Observe data and decisions without orders.", selected: true, disabled: false },
  { id: "PAPER_TRADING", label: "Paper trading", description: "Deterministic simulated fills in the backend ledger.", selected: false, disabled: false },
  { id: "MANUAL_APPROVAL", label: "Manual approval", description: "A trusted server-side approval is required.", selected: false, disabled: false },
  { id: "LIVE_AUTOMATIC", label: "Live automatic", description: "Locked until deployment acceptance and explicit activation.", selected: false, disabled: true },
] as const satisfies readonly ExecutionModeOption[];

export const DEMO_GUARDRAILS = [
  { id: "data-age", label: "Maximum data age", value: "30 seconds", description: "Older inputs must fail closed.", locked: true },
  { id: "risk", label: "Maximum risk per trade", value: "2.00%", description: "Hard ceiling enforced by the backend.", locked: true },
  { id: "daily-loss", label: "Daily loss switch", value: "INR 100,000", description: "Blocks new execution after the configured loss.", locked: true },
  { id: "loss-streak", label: "Consecutive losses", value: "3 trades", description: "Activates the persistent kill switch.", locked: true },
  { id: "expiry", label: "Minimum expiry buffer", value: "15 minutes", description: "Prevents entry inside the expiry lockout window.", locked: true },
  { id: "spread", label: "Maximum spread", value: "2.50%", description: "Evaluated independently for CE and PE.", locked: true },
] as const satisfies readonly GuardrailSetting[];

export const DEMO_CONTRACT_MASTER = [
  { market: "NIFTY", symbol: "NIFTY", exchange: "NSE", segment: "NSE_FNO", securityId: "DEMO-NIFTY-FUT", instrumentClass: "FUTIDX / OPTIDX", expiry: "2026-08-27", lotSize: 75, strikeInterval: 50, tickSize: 0.05, pricingModel: "BLACK_SCHOLES", status: "VERIFIED_DEMO", source: "DEMO" },
  { market: "BANKNIFTY", symbol: "BANKNIFTY", exchange: "NSE", segment: "NSE_FNO", securityId: "DEMO-BNF-FUT", instrumentClass: "FUTIDX / OPTIDX", expiry: "2026-08-27", lotSize: 30, strikeInterval: 100, tickSize: 0.05, pricingModel: "BLACK_SCHOLES", status: "VERIFIED_DEMO", source: "DEMO" },
  { market: "SENSEX", symbol: "SENSEX", exchange: "BSE", segment: "BSE_FNO", securityId: "DEMO-SENSEX-FUT", instrumentClass: "FUTIDX / OPTIDX", expiry: "2026-08-27", lotSize: 20, strikeInterval: 100, tickSize: 0.05, pricingModel: "BLACK_SCHOLES", status: "VERIFIED_DEMO", source: "DEMO" },
  { market: "STOCK F&O", symbol: "RELIANCE", exchange: "NSE", segment: "NSE_FNO", securityId: "DEMO-RELIANCE-FUT", instrumentClass: "FUTSTK / OPTSTK", expiry: "2026-08-27", lotSize: 500, strikeInterval: 20, tickSize: 0.05, pricingModel: "BLACK_SCHOLES", status: "EXPIRY_REVIEW", source: "DEMO" },
  { market: "MCX", symbol: "GOLD", exchange: "MCX", segment: "MCX_COMM", securityId: "DEMO-GOLD-FUT", instrumentClass: "FUTCOM / OPTFUT", expiry: "2026-09-04", lotSize: 1, strikeInterval: 500, tickSize: 1, pricingModel: "BLACK_76", status: "VERIFIED_DEMO", source: "DEMO" },
] as const satisfies readonly ContractMasterRow[];

export const DEMO_GUIDE_SECTIONS = [
  { id: "select", step: "01", title: "Select one contract context", description: "Choose market, symbol, and expiry in the workspace header before reviewing evidence.", checks: ["Correct market", "Correct option expiry", "Verified lot and interval"], destination: "Dashboard" },
  { id: "verify", step: "02", title: "Verify imported inputs", description: "Review source labels and resolve every critical missing or stale value before interpreting a score.", checks: ["Timestamp recent", "Five strikes complete", "No unexpected override"], destination: "Calculator" },
  { id: "evidence", step: "03", title: "Read the evidence stack", description: "Combine trend, OI, volatility, liquidity, Greeks, and expiry context. No single metric is a trade.", checks: ["Score gap >= 8", "Event risk clear", "Direction independently confirmed"], destination: "Dashboard" },
  { id: "rank", step: "04", title: "Inspect executable pricing", description: "Compare both sides, every rejection reason, ask entry, and bid exit before selecting a contract.", checks: ["Ask >= bid", "Spread acceptable", "Security identity exact"], destination: "Strike ranking" },
  { id: "risk", step: "05", title: "Keep execution server-side", description: "The browser is a monitoring surface. Approval, risk checks, reconciliation, and mutations belong to the trusted backend.", checks: ["No browser secret", "No direct broker call", "Live automatic remains disabled"], destination: "Settings" },
] as const satisfies readonly GuideSection[];

function audit(
  id: string,
  category: FormulaAuditCheck["category"],
  label: string,
  condition: boolean,
  passEvidence: string,
  failEvidence: string,
): FormulaAuditCheck {
  return {
    id,
    category,
    label,
    status: condition ? "PASS" : "FAIL",
    evidence: condition ? passEvidence : failEvidence,
  };
}

export function buildFormulaAudit(
  snapshot: MarketSnapshot,
  now: Date = new Date(),
): readonly FormulaAuditCheck[] {
  const legs = snapshot.chain.flatMap((row) => [row.call, row.put]);
  const strikes = snapshot.chain.map((row) => row.strike);
  const uniqueStrikes = new Set(strikes);
  const expectedDecision = decisionFromScores(
    snapshot.analytics.callScore,
    snapshot.analytics.putScore,
  );
  const expectedTopology = strikes.every(
    (strike, index) => index === 0 || strike - strikes[index - 1] === snapshot.definition.strikeStep,
  );
  const quotesValid = legs.every(
    (leg) =>
      [leg.bid, leg.ask, leg.ltp, leg.iv].every(
        (value) => Number.isFinite(value) && value > 0,
      ) &&
      leg.ask >= leg.bid &&
      Number.isInteger(leg.volume) &&
      leg.volume >= 0 &&
      Number.isInteger(leg.openInterest) &&
      leg.openInterest >= 0,
  );
  const greeksFinite = legs.every((leg) =>
    [
      leg.greeks.delta,
      leg.greeks.gamma,
      leg.greeks.theta,
      leg.greeks.vega,
      leg.greeks.theoreticalPrice,
    ].every(Number.isFinite),
  );
  const effectiveInputsValid = snapshot.inputs.every((item) => {
    if (item.source === "MANUAL OVERRIDE") {
      return item.manualOverride !== undefined && item.effectiveValue === item.manualOverride;
    }
    if (item.source === "COMPUTED") return item.manualOverride === undefined;
    return item.manualOverride === undefined && item.effectiveValue === item.importedValue;
  });
  const rankingLinked = snapshot.ranking.every((entry) => {
    const row = snapshot.chain.find((candidate) => candidate.strike === entry.strike);
    const leg = entry.side === "CE" ? row?.call : row?.put;
    return (
      leg !== undefined &&
      entry.askEntry === leg.ask &&
      entry.bidExit === leg.bid &&
      entry.score === leg.strikeScore &&
      entry.band === scoreBand(entry.score)
    );
  });
  const eligibleBeforeRejected = snapshot.ranking.every(
    (entry, index, entries) =>
      index === 0 ||
      entries[index - 1].rejectionReasons.length === 0 ||
      entry.rejectionReasons.length > 0,
  );
  const capturedMs = Date.parse(snapshot.capturedAt);
  const ageSeconds = Number.isFinite(capturedMs)
    ? Math.round((now.getTime() - capturedMs) / 1000)
    : Number.NaN;
  const provenanceStatus: AuditStatus =
    snapshot.dataMode === "STALE" || !Number.isFinite(capturedMs)
      ? "FAIL"
      : snapshot.dataMode === "DEMO"
        ? "WARN"
        : ageSeconds >= 0 && ageSeconds <= 30
          ? "PASS"
          : "FAIL";

  return [
    audit("selection", "IDENTITY", "Selection matches market definition", snapshot.selection.market === snapshot.definition.id, `${snapshot.selection.market} is bound to ${snapshot.definition.label}.`, "Selection and definition identify different markets."),
    audit("five-strikes", "COMPLETENESS", "Exactly five unique strikes", snapshot.chain.length === 5 && uniqueStrikes.size === 5, `Five rows and ${uniqueStrikes.size} unique strikes are present.`, `${snapshot.chain.length} rows and ${uniqueStrikes.size} unique strikes were found.`),
    audit("topology", "COMPLETENESS", "ATM band follows verified interval", expectedTopology, `Adjacent strikes use a ${snapshot.definition.strikeStep}-point interval.`, "One or more adjacent strikes break the configured interval."),
    audit("legs", "COMPLETENESS", "Both CE and PE exist for every strike", legs.length === 10, `${legs.length} option legs are available.`, `Expected 10 legs but found ${legs.length}.`),
    audit("quotes", "PRICING", "Executable quote fields are valid", quotesValid, "All legs have positive bid/ask/LTP/IV, non-negative OI/volume, and ask >= bid.", "At least one leg has an invalid price, IV, OI, volume, or crossed quote."),
    audit("greeks", "PRICING", "Greek outputs are finite", greeksFinite, "All displayed Greek and theoretical values are finite.", "A displayed Greek or theoretical value is non-finite."),
    audit("effective-input", "PROVENANCE", "Effective input source is coherent", effectiveInputsValid, "Imported, computed, and overridden values resolve consistently.", "An effective value does not match its visible source."),
    audit("decision", "SCORING", "Decision matches score policy", snapshot.analytics.decision === expectedDecision.decision, `${snapshot.analytics.decision} matches the runtime score comparison.`, `Expected ${expectedDecision.decision}, received ${snapshot.analytics.decision}.`),
    audit("ranking-links", "SCORING", "Ranking values link to option legs", rankingLinked, "Ask, bid, score, and band values resolve to their source legs.", "At least one ranking entry differs from its source option leg."),
    audit("ranking-order", "SCORING", "Eligible legs precede rejected legs", eligibleBeforeRejected, "No rejected leg ranks ahead of an eligible leg.", "A rejected leg ranks ahead of an eligible leg."),
    {
      id: "data-provenance",
      category: "PROVENANCE",
      label: "Snapshot provenance and freshness",
      status: provenanceStatus,
      evidence:
        snapshot.dataMode === "DEMO"
          ? "DEMO snapshot: freshness is intentionally not certified."
          : Number.isFinite(ageSeconds)
            ? `${snapshot.dataMode} snapshot age is ${ageSeconds} seconds.`
            : "Snapshot timestamp is invalid.",
    },
  ];
}
