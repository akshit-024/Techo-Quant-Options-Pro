/** Exact JSON contracts exposed by the backend's read-only status and market routes. */

export const READ_ONLY_ROUTES = {
  health: "/health",
  status: "/status",
  latestSignal: "/signals/latest",
  paperPositions: "/paper/positions",
  journalSummary: "/journal/summary",
  markets: "/markets",
  contracts: "/contracts",
  marketWorkspace: "/market/workspace",
  marketChain: "/market/chain",
  marketAnalytics: "/market/analytics",
  marketUpdates: "/market/updates",
} as const;

export type ReadOnlyRoute = (typeof READ_ONLY_ROUTES)[keyof typeof READ_ONLY_ROUTES];

export type ExecutionMode =
  | "OFF"
  | "DATA_ONLY"
  | "PAPER_TRADING"
  | "MANUAL_APPROVAL"
  | "LIVE_AUTOMATIC";

export interface HealthResponse {
  status: "ok";
  mode: ExecutionMode;
  live_locked: boolean;
}

export interface KillSwitchStatus {
  active: boolean;
  reason: string | null;
  actor: string | null;
  changed_at: string;
}

export interface ExecutionCounts {
  signals: number;
  approvals: number;
  orders: number;
  fills: number;
  positions: number;
}

export interface StatusResponse {
  mode: ExecutionMode;
  live_enabled: boolean;
  live_gateway_configured: boolean;
  kill_switch: KillSwitchStatus;
  counts: ExecutionCounts;
  market_data?: MarketDataStatusResponse;
}

export interface MarketFeedStatusResponse {
  configured: boolean;
  state: string;
  connected: boolean;
  healthy: boolean;
  transport_healthy?: boolean;
  data_healthy?: boolean;
  decision_inputs_configured?: boolean;
  actionable_ready?: boolean;
  expected_instruments?: number;
  ready_instruments?: number;
  attempted_markets?: number;
  accepted_markets?: number;
  published_markets?: number;
  data_successful_markets?: number;
  successful_markets?: number;
  failed_markets?: number;
  missing_instruments?: readonly string[];
}

export interface MarketDataStatusResponse {
  read_model_configured: boolean;
  revision: number;
  feed: MarketFeedStatusResponse;
}

export interface MarketReadFreshness {
  evaluated_at: string;
  oldest_component_age_seconds: number;
  newest_component_age_seconds: number;
  maximum_age_seconds: number;
  future_clock_skew_seconds: number;
}

export type MarketDataMode = "LIVE" | "STALE" | "INCOMPLETE" | "NON_LIVE";

export interface MarketReadStatus {
  snapshot_id: string;
  contract_key: string;
  sequence: number;
  source: string;
  captured_at: string;
  received_at: string;
  data_mode: MarketDataMode;
  complete: boolean;
  fresh: boolean;
  actionable: boolean;
  operational_decision: string;
  blockers: readonly string[];
  warnings: readonly string[];
  freshness: MarketReadFreshness;
}

export interface MarketSelection {
  market_id: string;
  symbol: string;
  expiry: string;
}

export interface MarketCatalogSymbol {
  symbol: string;
  expiries: readonly string[];
  latest: MarketReadStatus;
}

export interface MarketCatalogMarket {
  market_id: string;
  symbols: readonly MarketCatalogSymbol[];
}

export interface MarketCatalogResponse {
  generated_at: string;
  markets: readonly MarketCatalogMarket[];
}

/** JSON-safe backend domain details whose leaf fields vary by instrument kind. */
export type MarketJsonRecord = Record<string, unknown>;

export interface MarketContractResponse {
  contract_key: string;
  underlying: MarketJsonRecord;
  market_kind: string;
  pricing_model: string;
  option_expiry: string;
  lot_size: number;
  strike_interval: string;
  tick_size: string;
  master: MarketJsonRecord;
  futures: MarketJsonRecord | null;
  option_contracts: readonly MarketJsonRecord[];
}

export interface MarketChainLeg extends MarketJsonRecord {
  security_id: string;
  strike: string;
  option_type: "CE" | "PE";
}

export interface MarketChainStrike {
  strike: string;
  moneyness: string | null;
  call: MarketChainLeg | null;
  put: MarketChainLeg | null;
}

export interface MarketMissingLeg {
  strike: string;
  option_type: "CE" | "PE";
}

export interface MarketChain {
  atm_strike: string | null;
  strike_interval: string;
  leg_count: number;
  strikes: readonly MarketChainStrike[];
  missing_legs: readonly MarketMissingLeg[];
}

export interface MarketTrend {
  direction: string;
  strength: number;
}

export interface MarketAnalytics {
  pricing_underlying: string | null;
  expected_move: string | null;
  expected_low: string | null;
  expected_high: string | null;
  synthetic_futures: string | null;
  put_call_ratio: number | null;
  change_oi_put_call_ratio: number | null;
  support: string | null;
  resistance: string | null;
  atm_iv_decimal: number | null;
  trend: MarketTrend;
  call_score: number | null;
  put_score: number | null;
  score_gap: number | null;
  decision: string;
  decision_reason: string;
  ranked_strikes: readonly MarketJsonRecord[];
  trade_plan: MarketJsonRecord | null;
  generated_at: string | null;
}

export interface MarketValidationResponse {
  accepted: boolean;
  snapshot_id: string;
  snapshot_hash: string;
  issues: readonly MarketJsonRecord[];
}

export interface ContractLookupResponse {
  read_model: MarketReadStatus;
  selection: MarketSelection;
  contract: MarketContractResponse;
}

/** A single coherent, validated snapshot used by all live-market screens. */
export interface MarketWorkspaceResponse extends ContractLookupResponse {
  market: MarketJsonRecord;
  technicals: MarketJsonRecord;
  context: MarketJsonRecord;
  chain: MarketChain;
  analytics: MarketAnalytics;
  validation: MarketValidationResponse;
}

export interface MarketChainResponse {
  read_model: MarketReadStatus;
  selection: MarketSelection;
  chain: MarketChain;
}

export interface MarketAnalyticsResponse {
  read_model: MarketReadStatus;
  selection: MarketSelection;
  analytics: MarketAnalytics;
}

export interface MarketUpdateEvent {
  revision: number;
  event_type: string;
  occurred_at: string;
  market_id: string | null;
  symbol: string | null;
  expiry: string | null;
  snapshot_id: string | null;
  security_id: string | null;
}

export interface MarketUpdatesResponse {
  after: number;
  revision: number;
  changed: boolean;
  reset_required: boolean;
  event: MarketUpdateEvent | null;
}

export interface EvidenceBreakdownResponse {
  factors: Readonly<Record<string, number>>;
  points: Readonly<Record<string, number>>;
  total: number;
}

export interface RankedStrikeResponse {
  rank: number;
  security_id: string;
  strike: string;
  option_type: "CE" | "PE";
  entry_ask: string | null;
  score: number;
  evidence: EvidenceBreakdownResponse | null;
  liquidity_score: number;
  eligible: boolean;
  rejection_reasons: readonly string[];
}

export interface AnalysisTradePlanResponse {
  signal_id: string;
  snapshot_id: string;
  contract_key: string;
  strategy_version: string;
  evidence_version: string;
  generated_at: string;
  decision: string;
  actionable: boolean;
  symbol: string;
  security_id: string;
  option_type: "CE" | "PE";
  strike: string;
  expiry: string;
  score: number;
  score_gap: number;
  entry: string;
  stop: string;
  targets: readonly string[];
  lot_size: number;
  lots: number;
  quantity: number;
  maximum_risk: string;
  risk_per_lot: string;
  premium_required: string;
}

export interface LatestAnalysisSignal {
  evaluation_id: string;
  snapshot_id: string;
  generated_at: string;
  decision: string;
  reason: string;
  call_score: number;
  put_score: number;
  score_gap: number;
  ranked_strikes: readonly RankedStrikeResponse[];
  trade_plan: AnalysisTradePlanResponse | null;
  warnings: readonly string[];
}

export interface ExecutionPlanResponse {
  signal_id: string;
  correlation_id: string;
  symbol: string;
  security_id: string;
  side: "BUY" | "SELL";
  quantity: number;
  limit_price: string;
  maximum_loss: string;
  signal_time: string;
  data_time: string;
  valid_until: string;
  contract_expiry: string;
  event_risk_active: boolean | null;
  expiry_risk_clear: boolean;
  strategy_version: string;
}

export interface LatestExecutionSignal {
  signal_id: string;
  correlation_id: string;
  symbol: string;
  security_id: string;
  state: string;
  received_at: string;
  updated_at: string;
  plan: ExecutionPlanResponse;
}

export type LatestSignal = LatestAnalysisSignal | LatestExecutionSignal;

export interface LatestSignalResponse {
  signal: LatestSignal | null;
}

export interface PaperPositionResponse {
  position_id: string;
  signal_id: string;
  symbol: string;
  security_id: string;
  side: "BUY" | "SELL";
  quantity: number;
  average_entry_price: string;
  state: "OPEN" | "CLOSED";
  opened_at: string;
  closed_at: string | null;
  closed_day: string | null;
  realized_pnl: string | null;
}

export interface PaperPositionsResponse {
  positions: readonly PaperPositionResponse[];
}

export interface JournalSummaryResponse {
  journal_entries: number;
  orders: number;
  closed_positions: number;
  realized_pnl: string;
}

export interface BackendReadSnapshot {
  fetchedAt: string;
  health: HealthResponse;
  status: StatusResponse;
  latestSignal: LatestSignalResponse;
  paperPositions: PaperPositionsResponse;
  journalSummary: JournalSummaryResponse;
}

export interface ApiErrorResponse {
  error: {
    code: string;
    message: string;
  };
}
