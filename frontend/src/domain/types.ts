export type MarketId = "NIFTY" | "BANKNIFTY" | "SENSEX" | "STOCK_FNO" | "MCX";

export type ViewId =
  | "start"
  | "dashboard"
  | "calculator"
  | "greeks"
  | "market_leaders"
  | "ranking"
  | "position_sizer"
  | "trade_plan"
  | "api_status"
  | "signals"
  | "journal"
  | "backtests"
  | "contract_master"
  | "settings"
  | "guide"
  | "audit";

export type Decision =
  | "BUY CALL"
  | "BUY PUT"
  | "WAIT"
  | "NO TRADE"
  | "INSUFFICIENT DATA";

export type InputSource =
  | "DEMO FEED"
  | "LIVE FEED"
  | "MANUAL OVERRIDE"
  | "COMPUTED";

export type ScoreBand = "STRONG" | "TRADABLE" | "WATCHLIST" | "NO TRADE";

export interface WorkspaceSelection {
  market: MarketId;
  symbol: string;
  expiry: string;
}

export interface MarketDefinition {
  id: MarketId;
  label: string;
  shortLabel: string;
  symbols: readonly string[];
  expiries: readonly string[];
  baseSpot: number;
  strikeStep: number;
  lotSize: number;
  marketKind: "INDEX" | "STOCK" | "COMMODITY";
}

export interface CalculatorInput {
  id: string;
  label: string;
  group: "SESSION" | "MARKET" | "TECHNICAL" | "RISK";
  importedValue: string;
  manualOverride?: string;
  effectiveValue: string;
  source: InputSource;
  helper?: string;
}

export interface Greeks {
  delta: number;
  gamma: number;
  theta: number;
  vega: number;
  theoreticalPrice: number | null;
}

export interface OptionLeg {
  side: "CE" | "PE";
  securityId: string;
  bid: number;
  ask: number;
  ltp: number;
  volume: number;
  openInterest: number;
  changeOpenInterest: number | null;
  iv: number;
  spreadPercent: number;
  liquidityScore: number;
  strikeScore: number;
  greeks: Greeks;
  rejectionReasons: readonly string[];
}

export interface OptionStrike {
  strike: number;
  moneyness: "ATM−2" | "ATM−1" | "ATM" | "ATM+1" | "ATM+2";
  call: OptionLeg;
  put: OptionLeg;
}

export interface AnalyticsSummary {
  expectedMove: number;
  expectedLow: number;
  expectedHigh: number;
  syntheticFutures: number;
  trend: "BULLISH" | "BEARISH" | "MIXED";
  trendStrength: number;
  pcr: number;
  changeOiPcr: number | null;
  support: number;
  resistance: number;
  atmIv: number;
  callScore: number;
  putScore: number;
  decision: Decision;
  decisionReason: string;
  signalGap: number;
  spotSeries: readonly number[];
}

export interface RankingEntry {
  rank: number;
  side: "CE" | "PE";
  strike: number;

  contractName: string;

  score: number;
  band: ScoreBand;
  askEntry: number;
  bidExit: number;
  spreadPercent: number;
  liquidityScore: number;
  delta: number;
  rejectionReasons: readonly string[];
}

export interface BackendSnapshotAuthority {
  snapshotId: string;
  contractKey: string;
  source: string;
  receivedAt: string;
  complete: boolean;
  fresh: boolean;
  actionable: boolean;
  blockers: readonly string[];
  warnings: readonly string[];
  validationAccepted: boolean;
}

export interface MarketSnapshot {
  selection: WorkspaceSelection;
  definition: MarketDefinition;
  capturedAt: string;
  dataMode: "DEMO" | "LIVE" | "STALE";
  inputs: readonly CalculatorInput[];
  chain: readonly OptionStrike[];
  analytics: AnalyticsSummary;
  ranking: readonly RankingEntry[];
  backendAuthority?: BackendSnapshotAuthority;
}

export interface ExecutionHealth {
  status: "ok" | "offline";
  mode: string;
  liveLocked: boolean;
}
