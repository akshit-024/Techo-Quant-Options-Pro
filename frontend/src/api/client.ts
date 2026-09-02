import {
  READ_ONLY_ROUTES,
  type BackendReadSnapshot,
  type ContractLookupResponse,
  type ExecutionCounts,
  type ExecutionMode,
  type HealthResponse,
  type JournalSummaryResponse,
  type KillSwitchStatus,
  type LatestAnalysisSignal,
  type LatestExecutionSignal,
  type LatestSignal,
  type LatestSignalResponse,
  type MarketAnalytics,
  type MarketAnalyticsResponse,
  type MarketCatalogResponse,
  type MarketChain,
  type MarketChainLeg,
  type MarketChainResponse,
  type MarketContractResponse,
  type MarketDataMode,
  type MarketDataStatusResponse,
  type MarketFeedStatusResponse,
  type MarketJsonRecord,
  type MarketReadFreshness,
  type MarketReadStatus,
  type MarketSelection,
  type MarketUpdatesResponse,
  type MarketUpdateEvent,
  type MarketValidationResponse,
  type MarketWorkspaceResponse,
  type PaperPositionResponse,
  type PaperPositionsResponse,
  type ReadOnlyRoute,
  type StatusResponse,
} from "./contracts";

export const DEFAULT_REQUEST_TIMEOUT_MS = 5_000;
export const DEFAULT_MARKET_UPDATE_TIMEOUT_SECONDS = 15;

export type BackendApiErrorCode =
  | "ABORTED"
  | "HTTP_ERROR"
  | "INVALID_BASE_URL"
  | "INVALID_RESPONSE"
  | "NETWORK_ERROR"
  | "TIMEOUT";

export class BackendApiError extends Error {
  readonly code: BackendApiErrorCode;
  readonly route?: ReadOnlyRoute;
  readonly status?: number;

  constructor(
    code: BackendApiErrorCode,
    message: string,
    details: { route?: ReadOnlyRoute; status?: number } = {},
  ) {
    super(message);
    this.name = "BackendApiError";
    this.code = code;
    this.route = details.route;
    this.status = details.status;
  }
}

export interface FetchBackendSnapshotOptions {
  baseUrl: string;
  signal?: AbortSignal;
  timeoutMs?: number;
  fetchImpl?: typeof fetch;
  now?: () => Date;
}

export interface MarketApiRequestOptions {
  baseUrl: string;
  signal?: AbortSignal;
  timeoutMs?: number;
  fetchImpl?: typeof fetch;
}

export interface MarketSelectionRequest extends MarketApiRequestOptions {
  selection: MarketSelection;
}

export interface MarketUpdatesRequest extends Omit<MarketApiRequestOptions, "timeoutMs"> {
  after: number;
  timeoutSeconds?: number;
  /** Must exceed the server's bounded wait; defaults to wait + five seconds. */
  timeoutMs?: number;
}

export function normalizeApiBaseUrl(value: string | null | undefined): string | null {
  const trimmed = value?.trim();
  if (!trimmed) return null;

  if (trimmed.startsWith("/")) {
    if (trimmed.startsWith("//") || trimmed.includes("?") || trimmed.includes("#")) {
      throw new BackendApiError(
        "INVALID_BASE_URL",
        "API base URL must be a same-origin path without query or fragment",
      );
    }
    return trimmed.replace(/\/+$/, "") || "/";
  }

  let parsed: URL;
  try {
    parsed = new URL(trimmed);
  } catch {
    throw new BackendApiError("INVALID_BASE_URL", "API base URL is invalid");
  }
  if (!(["http:", "https:"] as const).includes(parsed.protocol as "http:" | "https:")) {
    throw new BackendApiError("INVALID_BASE_URL", "API base URL must use HTTP or HTTPS");
  }
  if (parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new BackendApiError(
      "INVALID_BASE_URL",
      "API base URL cannot contain credentials, query, or fragment",
    );
  }
  return parsed.toString().replace(/\/+$/, "");
}

export function configuredApiBaseUrl(): string | null {
  return normalizeApiBaseUrl(import.meta.env.VITE_API_BASE_URL);
}

export function fetchBackendStatus(
  options: MarketApiRequestOptions,
): Promise<StatusResponse> {
  return getSingleJson(options, READ_ONLY_ROUTES.status, parseStatus);
}

export function fetchMarketCatalog(
  options: MarketApiRequestOptions,
): Promise<MarketCatalogResponse> {
  return getSingleJson(options, READ_ONLY_ROUTES.markets, parseMarketCatalog);
}

export function fetchMarketContract(
  options: MarketSelectionRequest,
): Promise<ContractLookupResponse> {
  return getSingleJson(
    options,
    READ_ONLY_ROUTES.contracts,
    parseContractLookup,
    marketSelectionQuery(options.selection),
  );
}

export function fetchMarketWorkspace(
  options: MarketSelectionRequest,
): Promise<MarketWorkspaceResponse> {
  return getSingleJson(
    options,
    READ_ONLY_ROUTES.marketWorkspace,
    parseMarketWorkspace,
    marketSelectionQuery(options.selection),
  );
}

export function fetchMarketChain(
  options: MarketSelectionRequest,
): Promise<MarketChainResponse> {
  return getSingleJson(
    options,
    READ_ONLY_ROUTES.marketChain,
    parseMarketChainResponse,
    marketSelectionQuery(options.selection),
  );
}

export function fetchMarketAnalytics(
  options: MarketSelectionRequest,
): Promise<MarketAnalyticsResponse> {
  return getSingleJson(
    options,
    READ_ONLY_ROUTES.marketAnalytics,
    parseMarketAnalyticsResponse,
    marketSelectionQuery(options.selection),
  );
}

export function waitForMarketUpdates({
  after,
  timeoutSeconds = DEFAULT_MARKET_UPDATE_TIMEOUT_SECONDS,
  timeoutMs = (timeoutSeconds + 5) * 1_000,
  ...options
}: MarketUpdatesRequest): Promise<MarketUpdatesResponse> {
  if (!Number.isSafeInteger(after) || after < 0) {
    throw new RangeError("after must be a non-negative safe integer");
  }
  if (!Number.isFinite(timeoutSeconds) || timeoutSeconds < 0 || timeoutSeconds > 30) {
    throw new RangeError("timeoutSeconds must be between 0 and 30");
  }
  return getSingleJson(
    { ...options, timeoutMs },
    READ_ONLY_ROUTES.marketUpdates,
    parseMarketUpdates,
    new URLSearchParams({ after: String(after), timeout: String(timeoutSeconds) }),
  );
}

export async function fetchBackendSnapshot({
  baseUrl,
  signal,
  timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS,
  fetchImpl = fetch,
  now = () => new Date(),
}: FetchBackendSnapshotOptions): Promise<BackendReadSnapshot> {
  const normalizedBase = normalizeApiBaseUrl(baseUrl);
  if (normalizedBase === null) {
    throw new BackendApiError("INVALID_BASE_URL", "API base URL is not configured");
  }
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
    throw new RangeError("timeoutMs must be finite and positive");
  }

  const controller = new AbortController();
  let timedOut = false;
  const abortFromCaller = () => controller.abort(signal?.reason);
  if (signal?.aborted) abortFromCaller();
  else signal?.addEventListener("abort", abortFromCaller, { once: true });
  const timeout = globalThis.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);

  try {
    const [health, status, latestSignal, paperPositions, journalSummary] =
      await Promise.all([
        getJson(normalizedBase, READ_ONLY_ROUTES.health, controller.signal, fetchImpl, parseHealth),
        getJson(normalizedBase, READ_ONLY_ROUTES.status, controller.signal, fetchImpl, parseStatus),
        getJson(
          normalizedBase,
          READ_ONLY_ROUTES.latestSignal,
          controller.signal,
          fetchImpl,
          parseLatestSignal,
        ),
        getJson(
          normalizedBase,
          READ_ONLY_ROUTES.paperPositions,
          controller.signal,
          fetchImpl,
          parsePaperPositions,
        ),
        getJson(
          normalizedBase,
          READ_ONLY_ROUTES.journalSummary,
          controller.signal,
          fetchImpl,
          parseJournalSummary,
        ),
      ]);
    return {
      fetchedAt: now().toISOString(),
      health,
      status,
      latestSignal,
      paperPositions,
      journalSummary,
    };
  } catch (error) {
    controller.abort();
    if (error instanceof BackendApiError) throw error;
    if (timedOut) {
      throw new BackendApiError("TIMEOUT", "Backend status request timed out");
    }
    if (signal?.aborted || isAbortError(error)) {
      throw new BackendApiError("ABORTED", "Backend status request was cancelled");
    }
    throw new BackendApiError("NETWORK_ERROR", "Backend status request failed");
  } finally {
    globalThis.clearTimeout(timeout);
    signal?.removeEventListener("abort", abortFromCaller);
  }
}

async function getSingleJson<T>(
  {
    baseUrl,
    signal,
    timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS,
    fetchImpl = fetch,
  }: MarketApiRequestOptions,
  route: ReadOnlyRoute,
  parser: (value: unknown, route: ReadOnlyRoute) => T,
  query?: URLSearchParams,
): Promise<T> {
  const normalizedBase = normalizeApiBaseUrl(baseUrl);
  if (normalizedBase === null) {
    throw new BackendApiError("INVALID_BASE_URL", "API base URL is not configured");
  }
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
    throw new RangeError("timeoutMs must be finite and positive");
  }

  const controller = new AbortController();
  let timedOut = false;
  const abortFromCaller = () => controller.abort(signal?.reason);
  if (signal?.aborted) abortFromCaller();
  else signal?.addEventListener("abort", abortFromCaller, { once: true });
  const timeout = globalThis.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);

  try {
    return await getJson(
      normalizedBase,
      route,
      controller.signal,
      fetchImpl,
      parser,
      query,
    );
  } catch (error) {
    if (timedOut) {
      throw new BackendApiError("TIMEOUT", `GET ${route} timed out`, { route });
    }
    if (signal?.aborted || isAbortError(error)) {
      throw new BackendApiError("ABORTED", `GET ${route} was cancelled`, { route });
    }
    if (error instanceof BackendApiError) throw error;
    throw new BackendApiError("NETWORK_ERROR", `GET ${route} failed`, { route });
  } finally {
    globalThis.clearTimeout(timeout);
    signal?.removeEventListener("abort", abortFromCaller);
  }
}

async function getJson<T>(
  baseUrl: string,
  route: ReadOnlyRoute,
  signal: AbortSignal,
  fetchImpl: typeof fetch,
  parser: (value: unknown, route: ReadOnlyRoute) => T,
  query?: URLSearchParams,
): Promise<T> {
  let response: Response;
  try {
    response = await fetchImpl(joinRoute(baseUrl, route, query), {
      method: "GET",
      headers: { Accept: "application/json" },
      credentials: "omit",
      cache: "no-store",
      signal,
    });
  } catch (error) {
    if (isAbortError(error) || signal.aborted) throw error;
    throw new BackendApiError("NETWORK_ERROR", `GET ${route} failed`, { route });
  }
  if (!response.ok) {
    throw new BackendApiError(
      "HTTP_ERROR",
      `GET ${route} returned HTTP ${response.status}`,
      { route, status: response.status },
    );
  }
  let value: unknown;
  try {
    value = await response.json();
  } catch {
    throw invalidResponse(route, "response body is not valid JSON");
  }
  return parser(value, route);
}

function joinRoute(
  baseUrl: string,
  route: ReadOnlyRoute,
  query?: URLSearchParams,
): string {
  const path = baseUrl === "/" ? route : `${baseUrl}${route}`;
  const encoded = query?.toString();
  return encoded ? `${path}?${encoded}` : path;
}

function parseHealth(value: unknown, route: ReadOnlyRoute): HealthResponse {
  const record = asRecord(value, route);
  if (record.status !== "ok") throw invalidResponse(route, "status must be ok");
  return {
    status: "ok",
    mode: executionMode(record.mode, route),
    live_locked: requiredBoolean(record.live_locked, "live_locked", route),
  };
}

function parseStatus(value: unknown, route: ReadOnlyRoute): StatusResponse {
  const record = asRecord(value, route);
  const result: StatusResponse = {
    mode: executionMode(record.mode, route),
    live_enabled: requiredBoolean(record.live_enabled, "live_enabled", route),
    live_gateway_configured: requiredBoolean(
      record.live_gateway_configured,
      "live_gateway_configured",
      route,
    ),
    kill_switch: parseKillSwitch(record.kill_switch, route),
    counts: parseCounts(record.counts, route),
  };
  if (record.market_data !== undefined) {
    result.market_data = parseMarketDataStatus(record.market_data, route);
  }
  return result;
}

function parseMarketDataStatus(
  value: unknown,
  route: ReadOnlyRoute,
): MarketDataStatusResponse {
  const record = asRecord(value, route);
  return {
    read_model_configured: requiredBoolean(
      record.read_model_configured,
      "market_data.read_model_configured",
      route,
    ),
    revision: nonNegativeInteger(record.revision, "market_data.revision", route),
    feed: parseMarketFeedStatus(record.feed, route),
  };
}

function parseMarketFeedStatus(
  value: unknown,
  route: ReadOnlyRoute,
): MarketFeedStatusResponse {
  const record = asRecord(value, route);
  const result: MarketFeedStatusResponse = {
    configured: requiredBoolean(record.configured, "market_data.feed.configured", route),
    state: requiredString(record.state, "market_data.feed.state", route),
    connected: requiredBoolean(record.connected, "market_data.feed.connected", route),
    healthy: requiredBoolean(record.healthy, "market_data.feed.healthy", route),
  };
  copyOptionalBooleans(record, result, route, "market_data.feed", [
    "transport_healthy",
    "data_healthy",
    "decision_inputs_configured",
    "actionable_ready",
  ]);
  copyOptionalNonNegativeIntegers(record, result, route, "market_data.feed", [
    "expected_instruments",
    "ready_instruments",
    "attempted_markets",
    "accepted_markets",
    "published_markets",
    "data_successful_markets",
    "successful_markets",
    "failed_markets",
  ]);
  if (record.missing_instruments !== undefined) {
    result.missing_instruments = stringArray(
      record.missing_instruments,
      "market_data.feed.missing_instruments",
      route,
    );
  }
  return result;
}

function parseKillSwitch(value: unknown, route: ReadOnlyRoute): KillSwitchStatus {
  const record = asRecord(value, route);
  return {
    active: requiredBoolean(record.active, "kill_switch.active", route),
    reason: nullableString(record.reason, "kill_switch.reason", route),
    actor: nullableString(record.actor, "kill_switch.actor", route),
    changed_at: requiredString(record.changed_at, "kill_switch.changed_at", route),
  };
}

function parseCounts(value: unknown, route: ReadOnlyRoute): ExecutionCounts {
  const record = asRecord(value, route);
  return {
    signals: nonNegativeInteger(record.signals, "counts.signals", route),
    approvals: nonNegativeInteger(record.approvals, "counts.approvals", route),
    orders: nonNegativeInteger(record.orders, "counts.orders", route),
    fills: nonNegativeInteger(record.fills, "counts.fills", route),
    positions: nonNegativeInteger(record.positions, "counts.positions", route),
  };
}

function parseLatestSignal(value: unknown, route: ReadOnlyRoute): LatestSignalResponse {
  const record = asRecord(value, route);
  if (record.signal === null) return { signal: null };
  const signal = asRecord(record.signal, route);
  if ("ranked_strikes" in signal) {
    requiredString(signal.evaluation_id, "signal.evaluation_id", route);
    requiredString(signal.generated_at, "signal.generated_at", route);
    if (!Array.isArray(signal.ranked_strikes)) {
      throw invalidResponse(route, "signal.ranked_strikes must be an array");
    }
    return { signal: signal as unknown as LatestAnalysisSignal };
  }
  requiredString(signal.signal_id, "signal.signal_id", route);
  requiredString(signal.received_at, "signal.received_at", route);
  asRecord(signal.plan, route);
  return { signal: signal as unknown as LatestExecutionSignal };
}

function parsePaperPositions(value: unknown, route: ReadOnlyRoute): PaperPositionsResponse {
  const record = asRecord(value, route);
  if (!Array.isArray(record.positions)) {
    throw invalidResponse(route, "positions must be an array");
  }
  const positions = record.positions.map((position, index) => {
    const item = asRecord(position, route);
    requiredString(item.position_id, `positions[${index}].position_id`, route);
    requiredString(item.security_id, `positions[${index}].security_id`, route);
    nonNegativeInteger(item.quantity, `positions[${index}].quantity`, route);
    return item as unknown as PaperPositionResponse;
  });
  return { positions };
}

function parseJournalSummary(value: unknown, route: ReadOnlyRoute): JournalSummaryResponse {
  const record = asRecord(value, route);
  return {
    journal_entries: nonNegativeInteger(record.journal_entries, "journal_entries", route),
    orders: nonNegativeInteger(record.orders, "orders", route),
    closed_positions: nonNegativeInteger(record.closed_positions, "closed_positions", route),
    realized_pnl: requiredString(record.realized_pnl, "realized_pnl", route),
  };
}

function parseMarketCatalog(
  value: unknown,
  route: ReadOnlyRoute,
): MarketCatalogResponse {
  const record = asRecord(value, route);
  return {
    generated_at: requiredString(record.generated_at, "generated_at", route),
    markets: recordArray(record.markets, "markets", route).map((market, marketIndex) => ({
      market_id: requiredString(
        market.market_id,
        `markets[${marketIndex}].market_id`,
        route,
      ),
      symbols: recordArray(
        market.symbols,
        `markets[${marketIndex}].symbols`,
        route,
      ).map((symbol, symbolIndex) => ({
        symbol: requiredString(
          symbol.symbol,
          `markets[${marketIndex}].symbols[${symbolIndex}].symbol`,
          route,
        ),
        expiries: stringArray(
          symbol.expiries,
          `markets[${marketIndex}].symbols[${symbolIndex}].expiries`,
          route,
        ),
        latest: parseMarketReadStatus(symbol.latest, route),
      })),
    })),
  };
}

function parseContractLookup(
  value: unknown,
  route: ReadOnlyRoute,
): ContractLookupResponse {
  const record = asRecord(value, route);
  return {
    read_model: parseMarketReadStatus(record.read_model, route),
    selection: parseSelection(record.selection, route),
    contract: parseMarketContract(record.contract, route),
  };
}

function parseMarketWorkspace(
  value: unknown,
  route: ReadOnlyRoute,
): MarketWorkspaceResponse {
  const record = asRecord(value, route);
  const base = parseContractLookup(record, route);
  return {
    ...base,
    market: jsonRecord(record.market, "market", route),
    technicals: jsonRecord(record.technicals, "technicals", route),
    context: jsonRecord(record.context, "context", route),
    chain: parseMarketChain(record.chain, route),
    analytics: parseMarketAnalytics(record.analytics, route),
    validation: parseMarketValidation(record.validation, route),
  };
}

function parseMarketChainResponse(
  value: unknown,
  route: ReadOnlyRoute,
): MarketChainResponse {
  const record = asRecord(value, route);
  return {
    read_model: parseMarketReadStatus(record.read_model, route),
    selection: parseSelection(record.selection, route),
    chain: parseMarketChain(record.chain, route),
  };
}

function parseMarketAnalyticsResponse(
  value: unknown,
  route: ReadOnlyRoute,
): MarketAnalyticsResponse {
  const record = asRecord(value, route);
  return {
    read_model: parseMarketReadStatus(record.read_model, route),
    selection: parseSelection(record.selection, route),
    analytics: parseMarketAnalytics(record.analytics, route),
  };
}

function parseMarketReadStatus(value: unknown, route: ReadOnlyRoute): MarketReadStatus {
  const record = asRecord(value, route);
  return {
    snapshot_id: requiredString(record.snapshot_id, "read_model.snapshot_id", route),
    contract_key: requiredString(record.contract_key, "read_model.contract_key", route),
    sequence: nonNegativeInteger(record.sequence, "read_model.sequence", route),
    source: requiredString(record.source, "read_model.source", route),
    captured_at: requiredString(record.captured_at, "read_model.captured_at", route),
    received_at: requiredString(record.received_at, "read_model.received_at", route),
    data_mode: marketDataMode(record.data_mode, route),
    complete: requiredBoolean(record.complete, "read_model.complete", route),
    fresh: requiredBoolean(record.fresh, "read_model.fresh", route),
    actionable: requiredBoolean(record.actionable, "read_model.actionable", route),
    operational_decision: requiredString(
      record.operational_decision,
      "read_model.operational_decision",
      route,
    ),
    blockers: stringArray(record.blockers, "read_model.blockers", route),
    warnings: stringArray(record.warnings, "read_model.warnings", route),
    freshness: parseMarketFreshness(record.freshness, route),
  };
}

function parseMarketFreshness(
  value: unknown,
  route: ReadOnlyRoute,
): MarketReadFreshness {
  const record = asRecord(value, route);
  return {
    evaluated_at: requiredString(
      record.evaluated_at,
      "read_model.freshness.evaluated_at",
      route,
    ),
    oldest_component_age_seconds: finiteNumber(
      record.oldest_component_age_seconds,
      "read_model.freshness.oldest_component_age_seconds",
      route,
    ),
    newest_component_age_seconds: finiteNumber(
      record.newest_component_age_seconds,
      "read_model.freshness.newest_component_age_seconds",
      route,
    ),
    maximum_age_seconds: nonNegativeNumber(
      record.maximum_age_seconds,
      "read_model.freshness.maximum_age_seconds",
      route,
    ),
    future_clock_skew_seconds: nonNegativeNumber(
      record.future_clock_skew_seconds,
      "read_model.freshness.future_clock_skew_seconds",
      route,
    ),
  };
}

function parseSelection(value: unknown, route: ReadOnlyRoute): MarketSelection {
  const record = asRecord(value, route);
  return {
    market_id: requiredString(record.market_id, "selection.market_id", route),
    symbol: requiredString(record.symbol, "selection.symbol", route),
    expiry: requiredString(record.expiry, "selection.expiry", route),
  };
}

function parseMarketContract(
  value: unknown,
  route: ReadOnlyRoute,
): MarketContractResponse {
  const record = asRecord(value, route);
  return {
    contract_key: requiredString(record.contract_key, "contract.contract_key", route),
    underlying: jsonRecord(record.underlying, "contract.underlying", route),
    market_kind: requiredString(record.market_kind, "contract.market_kind", route),
    pricing_model: requiredString(record.pricing_model, "contract.pricing_model", route),
    option_expiry: requiredString(record.option_expiry, "contract.option_expiry", route),
    lot_size: positiveInteger(record.lot_size, "contract.lot_size", route),
    strike_interval: requiredString(
      record.strike_interval,
      "contract.strike_interval",
      route,
    ),
    tick_size: requiredString(record.tick_size, "contract.tick_size", route),
    master: jsonRecord(record.master, "contract.master", route),
    futures:
      record.futures === null
        ? null
        : jsonRecord(record.futures, "contract.futures", route),
    option_contracts: recordArray(
      record.option_contracts,
      "contract.option_contracts",
      route,
    ),
  };
}

function parseMarketChain(value: unknown, route: ReadOnlyRoute): MarketChain {
  const record = asRecord(value, route);
  const strikes = recordArray(record.strikes, "chain.strikes", route).map(
    (row, index) => ({
      strike: requiredString(row.strike, `chain.strikes[${index}].strike`, route),
      moneyness: nullableString(
        row.moneyness,
        `chain.strikes[${index}].moneyness`,
        route,
      ),
      call: parseMarketChainLeg(row.call, `chain.strikes[${index}].call`, route),
      put: parseMarketChainLeg(row.put, `chain.strikes[${index}].put`, route),
    }),
  );
  const missingLegs = recordArray(record.missing_legs, "chain.missing_legs", route).map(
    (item, index) => ({
      strike: requiredString(item.strike, `chain.missing_legs[${index}].strike`, route),
      option_type: optionType(
        item.option_type,
        `chain.missing_legs[${index}].option_type`,
        route,
      ),
    }),
  );
  const legCount = nonNegativeInteger(record.leg_count, "chain.leg_count", route);
  const actualLegs = strikes.reduce(
    (count, row) => count + Number(row.call !== null) + Number(row.put !== null),
    0,
  );
  if (legCount !== actualLegs) {
    throw invalidResponse(route, "chain.leg_count does not match the returned legs");
  }
  return {
    atm_strike: nullableString(record.atm_strike, "chain.atm_strike", route),
    strike_interval: requiredString(record.strike_interval, "chain.strike_interval", route),
    leg_count: legCount,
    strikes,
    missing_legs: missingLegs,
  };
}

function parseMarketChainLeg(
  value: unknown,
  field: string,
  route: ReadOnlyRoute,
): MarketChainLeg | null {
  if (value === null) return null;
  const record = jsonRecord(value, field, route);
  return {
    ...record,
    security_id: requiredString(record.security_id, `${field}.security_id`, route),
    strike: requiredString(record.strike, `${field}.strike`, route),
    option_type: optionType(record.option_type, `${field}.option_type`, route),
  };
}

function parseMarketAnalytics(value: unknown, route: ReadOnlyRoute): MarketAnalytics {
  const record = asRecord(value, route);
  const trend = asRecord(record.trend, route);
  return {
    pricing_underlying: nullableString(
      record.pricing_underlying,
      "analytics.pricing_underlying",
      route,
    ),
    expected_move: nullableString(record.expected_move, "analytics.expected_move", route),
    expected_low: nullableString(record.expected_low, "analytics.expected_low", route),
    expected_high: nullableString(record.expected_high, "analytics.expected_high", route),
    synthetic_futures: nullableString(
      record.synthetic_futures,
      "analytics.synthetic_futures",
      route,
    ),
    put_call_ratio: nullableFiniteNumber(
      record.put_call_ratio,
      "analytics.put_call_ratio",
      route,
    ),
    change_oi_put_call_ratio: nullableFiniteNumber(
      record.change_oi_put_call_ratio,
      "analytics.change_oi_put_call_ratio",
      route,
    ),
    support: nullableString(record.support, "analytics.support", route),
    resistance: nullableString(record.resistance, "analytics.resistance", route),
    atm_iv_decimal: nullableFiniteNumber(
      record.atm_iv_decimal,
      "analytics.atm_iv_decimal",
      route,
    ),
    trend: {
      direction: requiredString(trend.direction, "analytics.trend.direction", route),
      strength: nonNegativeNumber(trend.strength, "analytics.trend.strength", route),
    },
    call_score: nullableFiniteNumber(record.call_score, "analytics.call_score", route),
    put_score: nullableFiniteNumber(record.put_score, "analytics.put_score", route),
    score_gap: nullableFiniteNumber(record.score_gap, "analytics.score_gap", route),
    decision: requiredString(record.decision, "analytics.decision", route),
    decision_reason: requiredString(
      record.decision_reason,
      "analytics.decision_reason",
      route,
    ),
    ranked_strikes: recordArray(
      record.ranked_strikes,
      "analytics.ranked_strikes",
      route,
    ),
    trade_plan:
      record.trade_plan === null
        ? null
        : jsonRecord(record.trade_plan, "analytics.trade_plan", route),
    generated_at: nullableString(record.generated_at, "analytics.generated_at", route),
  };
}

function parseMarketValidation(
  value: unknown,
  route: ReadOnlyRoute,
): MarketValidationResponse {
  const record = asRecord(value, route);
  return {
    accepted: requiredBoolean(record.accepted, "validation.accepted", route),
    snapshot_id: requiredString(record.snapshot_id, "validation.snapshot_id", route),
    snapshot_hash: requiredString(record.snapshot_hash, "validation.snapshot_hash", route),
    issues: recordArray(record.issues, "validation.issues", route),
  };
}

function parseMarketUpdates(
  value: unknown,
  route: ReadOnlyRoute,
): MarketUpdatesResponse {
  const record = asRecord(value, route);
  const after = nonNegativeInteger(record.after, "after", route);
  const revision = nonNegativeInteger(record.revision, "revision", route);
  const changed = requiredBoolean(record.changed, "changed", route);
  const resetRequired = requiredBoolean(record.reset_required, "reset_required", route);
  const event = record.event === null ? null : parseMarketUpdateEvent(record.event, route);
  if (!changed && (event !== null || resetRequired)) {
    throw invalidResponse(route, "unchanged update cannot contain an event or reset");
  }
  if (event !== null && event.revision > revision) {
    throw invalidResponse(route, "event revision cannot exceed response revision");
  }
  return {
    after,
    revision,
    changed,
    reset_required: resetRequired,
    event,
  };
}

function parseMarketUpdateEvent(
  value: unknown,
  route: ReadOnlyRoute,
): MarketUpdateEvent {
  const record = asRecord(value, route);
  return {
    revision: nonNegativeInteger(record.revision, "event.revision", route),
    event_type: requiredString(record.event_type, "event.event_type", route),
    occurred_at: requiredString(record.occurred_at, "event.occurred_at", route),
    market_id: nullableString(record.market_id, "event.market_id", route),
    symbol: nullableString(record.symbol, "event.symbol", route),
    expiry: nullableString(record.expiry, "event.expiry", route),
    snapshot_id: nullableString(record.snapshot_id, "event.snapshot_id", route),
    security_id: nullableString(record.security_id, "event.security_id", route),
  };
}

function marketSelectionQuery(selection: MarketSelection): URLSearchParams {
  return new URLSearchParams({
    market: boundedQueryText(selection.market_id, "selection.market_id", 64),
    symbol: boundedQueryText(selection.symbol, "selection.symbol", 128),
    expiry: boundedQueryText(selection.expiry, "selection.expiry", 64),
  });
}

function boundedQueryText(value: string, field: string, maximumLength: number): string {
  const normalized = value.trim();
  if (!normalized || normalized.length > maximumLength || /[\u0000-\u001f]/u.test(normalized)) {
    throw new RangeError(`${field} is invalid`);
  }
  return normalized;
}

function asRecord(value: unknown, route: ReadOnlyRoute): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw invalidResponse(route, "response must be an object");
  }
  return value as Record<string, unknown>;
}

function jsonRecord(
  value: unknown,
  field: string,
  route: ReadOnlyRoute,
): MarketJsonRecord {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw invalidResponse(route, `${field} must be an object`);
  }
  return value as MarketJsonRecord;
}

function recordArray(
  value: unknown,
  field: string,
  route: ReadOnlyRoute,
): MarketJsonRecord[] {
  if (!Array.isArray(value)) {
    throw invalidResponse(route, `${field} must be an array`);
  }
  return value.map((item, index) => jsonRecord(item, `${field}[${index}]`, route));
}

function stringArray(value: unknown, field: string, route: ReadOnlyRoute): string[] {
  if (!Array.isArray(value)) {
    throw invalidResponse(route, `${field} must be an array`);
  }
  return value.map((item, index) => requiredString(item, `${field}[${index}]`, route));
}

function copyOptionalBooleans(
  source: Record<string, unknown>,
  target: MarketFeedStatusResponse,
  route: ReadOnlyRoute,
  prefix: string,
  fields: readonly string[],
): void {
  const output = target as unknown as Record<string, unknown>;
  for (const field of fields) {
    if (source[field] !== undefined) {
      output[field] = requiredBoolean(source[field], `${prefix}.${field}`, route);
    }
  }
}

function copyOptionalNonNegativeIntegers(
  source: Record<string, unknown>,
  target: MarketFeedStatusResponse,
  route: ReadOnlyRoute,
  prefix: string,
  fields: readonly string[],
): void {
  const output = target as unknown as Record<string, unknown>;
  for (const field of fields) {
    if (source[field] !== undefined) {
      output[field] = nonNegativeInteger(source[field], `${prefix}.${field}`, route);
    }
  }
}

function requiredString(value: unknown, field: string, route: ReadOnlyRoute): string {
  if (typeof value !== "string" || !value.trim()) {
    throw invalidResponse(route, `${field} must be a non-empty string`);
  }
  return value;
}

function nullableString(
  value: unknown,
  field: string,
  route: ReadOnlyRoute,
): string | null {
  if (value === null) return null;
  return requiredString(value, field, route);
}

function requiredBoolean(value: unknown, field: string, route: ReadOnlyRoute): boolean {
  if (typeof value !== "boolean") {
    throw invalidResponse(route, `${field} must be a boolean`);
  }
  return value;
}

function nonNegativeInteger(value: unknown, field: string, route: ReadOnlyRoute): number {
  if (!Number.isInteger(value) || (value as number) < 0) {
    throw invalidResponse(route, `${field} must be a non-negative integer`);
  }
  return value as number;
}

function positiveInteger(value: unknown, field: string, route: ReadOnlyRoute): number {
  const result = nonNegativeInteger(value, field, route);
  if (result === 0) throw invalidResponse(route, `${field} must be positive`);
  return result;
}

function finiteNumber(value: unknown, field: string, route: ReadOnlyRoute): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw invalidResponse(route, `${field} must be a finite number`);
  }
  return value;
}

function nonNegativeNumber(value: unknown, field: string, route: ReadOnlyRoute): number {
  const result = finiteNumber(value, field, route);
  if (result < 0) throw invalidResponse(route, `${field} must be non-negative`);
  return result;
}

function nullableFiniteNumber(
  value: unknown,
  field: string,
  route: ReadOnlyRoute,
): number | null {
  if (value === null) return null;
  return finiteNumber(value, field, route);
}

function optionType(value: unknown, field: string, route: ReadOnlyRoute): "CE" | "PE" {
  if (value !== "CE" && value !== "PE") {
    throw invalidResponse(route, `${field} must be CE or PE`);
  }
  return value;
}

function marketDataMode(value: unknown, route: ReadOnlyRoute): MarketDataMode {
  if (value !== "LIVE" && value !== "STALE" && value !== "INCOMPLETE" && value !== "NON_LIVE") {
    throw invalidResponse(route, "read_model.data_mode is invalid");
  }
  return value;
}

function executionMode(value: unknown, route: ReadOnlyRoute): ExecutionMode {
  if (
    value !== "OFF" &&
    value !== "DATA_ONLY" &&
    value !== "PAPER_TRADING" &&
    value !== "MANUAL_APPROVAL" &&
    value !== "LIVE_AUTOMATIC"
  ) {
    throw invalidResponse(route, "mode is invalid");
  }
  return value;
}

function invalidResponse(route: ReadOnlyRoute, detail: string): BackendApiError {
  return new BackendApiError("INVALID_RESPONSE", `Invalid ${route} response: ${detail}`, {
    route,
  });
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

export type { LatestSignal };
