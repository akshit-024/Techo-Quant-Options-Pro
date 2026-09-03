import type { MarketWorkspaceResponse } from "../api/contracts";
import { scoreBand } from "../domain/score";
import type {
  CalculatorInput,
  Decision,
  MarketDefinition,
  MarketId,
  MarketSnapshot,
  OptionLeg,
  OptionStrike,
  RankingEntry,
  WorkspaceSelection,
} from "../domain/types";

export type BackendSnapshotAdapterErrorCode =
  | "INVALID_FIELD"
  | "INCOMPLETE_WORKSPACE"
  | "NON_LIVE_WORKSPACE"
  | "INCOHERENT_WORKSPACE"
  | "UNSAFE_WORKSPACE";

/** A fail-closed error raised when a backend workspace cannot be represented faithfully. */
export class BackendSnapshotAdapterError extends Error {
  readonly code: BackendSnapshotAdapterErrorCode;
  readonly path: string;

  constructor(
    code: BackendSnapshotAdapterErrorCode,
    path: string,
    message: string,
  ) {
    super(message);
    this.name = "BackendSnapshotAdapterError";
    this.code = code;
    this.path = path;
  }
}

interface ParsedDecimal {
  readonly number: number;
  readonly text: string;
}

interface ParsedRanking {
  readonly rank: number;
  readonly securityId: string;
  readonly strike: number;
  readonly side: "CE" | "PE";
  readonly entryAsk: number;
  readonly score: number;
  readonly liquidityScore: number;
  readonly eligible: boolean;
  readonly rejectionReasons: readonly string[];
}

const LIVE_SOURCES = new Set(["DHAN_REST", "DHAN_LIVE"]);
const MARKET_IDS = new Set<MarketId>([
  "NIFTY",
  "BANKNIFTY",
  "SENSEX",
  "STOCK_FNO",
  "MCX",
]);
const MONEYNESS = ["ATM-2", "ATM-1", "ATM", "ATM+1", "ATM+2"] as const;
const DISPLAY_MONEYNESS: readonly OptionStrike["moneyness"][] = [
  "ATM−2",
  "ATM−1",
  "ATM",
  "ATM+1",
  "ATM+2",
];

/**
 * Convert one coherent backend workspace into the existing frontend read model.
 *
 * No demo fallback belongs here. A caller may retain its last successful snapshot when this
 * function throws, but must not relabel invalid, incomplete, or non-live data as live.
 */
export function adaptBackendSnapshot(
  workspace: MarketWorkspaceResponse,
): MarketSnapshot {
  const readModel = record(workspace.read_model, "read_model");
  const selectionRecord = record(workspace.selection, "selection");
  const contract = record(workspace.contract, "contract");
  const validation = record(workspace.validation, "validation");
  const market = record(workspace.market, "market");
  const technicals = record(workspace.technicals, "technicals");
  const context = record(workspace.context, "context");
  const chainRecord = record(workspace.chain, "chain");
  const analyticsRecord = record(workspace.analytics, "analytics");

  const snapshotId = nonEmptyString(readModel.snapshot_id, "read_model.snapshot_id");
  const contractKey = nonEmptyString(readModel.contract_key, "read_model.contract_key");
  const source = nonEmptyString(readModel.source, "read_model.source");
  const capturedAt = timestamp(readModel.captured_at, "read_model.captured_at");
  const receivedAt = timestamp(readModel.received_at, "read_model.received_at");
  const complete = booleanValue(readModel.complete, "read_model.complete");
  const fresh = booleanValue(readModel.fresh, "read_model.fresh");
  const actionable = booleanValue(readModel.actionable, "read_model.actionable");
  const blockers = stringArray(readModel.blockers, "read_model.blockers");
  const warnings = stringArray(readModel.warnings, "read_model.warnings");
  const dataMode = nonEmptyString(readModel.data_mode, "read_model.data_mode");
  const validationAccepted = booleanValue(
    validation.accepted,
    "validation.accepted",
  );

  if (!LIVE_SOURCES.has(source)) {
    fail(
      "NON_LIVE_WORKSPACE",
      "read_model.source",
      `workspace source ${source} is not a live Dhan source`,
    );
  }
  if (!complete || !validationAccepted) {
    fail(
      "INCOMPLETE_WORKSPACE",
      !complete ? "read_model.complete" : "validation.accepted",
      "backend workspace is incomplete or was rejected by canonical validation",
    );
  }
  if (dataMode !== "LIVE" && dataMode !== "STALE") {
    fail(
      "NON_LIVE_WORKSPACE",
      "read_model.data_mode",
      `workspace data mode ${dataMode} cannot drive a live frontend snapshot`,
    );
  }
  if (dataMode === "LIVE" && (!fresh || blockers.length > 0)) {
    fail(
      "INCOHERENT_WORKSPACE",
      "read_model",
      "a LIVE workspace must be fresh and free of operational blockers",
    );
  }
  if (dataMode === "STALE" && (fresh || blockers.length === 0)) {
    fail(
      "INCOHERENT_WORKSPACE",
      "read_model",
      "a STALE workspace must declare its freshness blocker",
    );
  }
  if (dataMode === "STALE" && actionable) {
    fail(
      "UNSAFE_WORKSPACE",
      "read_model.actionable",
      "stale market data cannot be actionable",
    );
  }

  const validationSnapshotId = nonEmptyString(
    validation.snapshot_id,
    "validation.snapshot_id",
  );
  positiveInteger(readModel.sequence, "read_model.sequence");
  nonEmptyString(validation.snapshot_hash, "validation.snapshot_hash");
  if (validationSnapshotId !== snapshotId) {
    mismatch("validation.snapshot_id", "validation and read model snapshot IDs differ");
  }
  const responseContractKey = nonEmptyString(
    contract.contract_key,
    "contract.contract_key",
  );
  if (responseContractKey !== contractKey) {
    mismatch("contract.contract_key", "contract and read model keys differ");
  }

  validateFreshness(readModel.freshness);
  const selection = parseSelection(selectionRecord);
  const optionExpiry = timestamp(contract.option_expiry, "contract.option_expiry");
  if (selection.expiry !== optionExpiry) {
    mismatch("selection.expiry", "selection expiry does not match the contract expiry");
  }

  const marketKind = marketKindValue(contract.market_kind, "contract.market_kind");
  validateMarketKind(selection.market, marketKind);
  const strikeInterval = positiveDecimal(
    contract.strike_interval,
    "contract.strike_interval",
  );
  const chainInterval = positiveDecimal(
    chainRecord.strike_interval,
    "chain.strike_interval",
  );
  if (!sameNumber(strikeInterval.number, chainInterval.number)) {
    mismatch("chain.strike_interval", "chain and contract strike intervals differ");
  }
  const lotSize = positiveInteger(contract.lot_size, "contract.lot_size");
  validateContractIdentity(contract);
  validateTechnicalState(technicals);
  validateStrategyContext(context);
  timestamp(analyticsRecord.generated_at, "analytics.generated_at");

  const pricingUnderlying = positiveDecimal(
    analyticsRecord.pricing_underlying,
    "analytics.pricing_underlying",
  );
  validateMarketUnderlying(market, marketKind, pricingUnderlying.number);
  const rankings = parseRankings(analyticsRecord.ranked_strikes);
  const chain = parseChain({
    chain: chainRecord,
    expiry: optionExpiry,
    rankings,
    strikeInterval: strikeInterval.number,
  });
  const contractNames = validateOptionContracts(
    contract.option_contracts,
    chain,
    optionExpiry,
    lotSize,
  );
  const ranking = buildRanking(chain, rankings, contractNames, selection);

  validateActionablePlan({
    actionable,
    analytics: analyticsRecord,
    contractKey,
    decision: nonEmptyString(
      readModel.operational_decision,
      "read_model.operational_decision",
    ),
    securityIds: new Set(ranking.map((entry) => securityIdFor(chain, entry))),
    snapshotId,
  });

  const analyticsDecision = nonEmptyString(
    analyticsRecord.decision,
    "analytics.decision",
  );
  const operationalDecision = nonEmptyString(
    readModel.operational_decision,
    "read_model.operational_decision",
  );
  if (dataMode === "LIVE" && analyticsDecision !== operationalDecision) {
    mismatch(
      "read_model.operational_decision",
      "live operational and analytical decisions differ",
    );
  }

  const marketDefinition: MarketDefinition = {
    id: selection.market,
    label: selection.symbol,
    shortLabel: selection.market,
    symbols: [selection.symbol],
    expiries: [selection.expiry],
    baseSpot: pricingUnderlying.number,
    strikeStep: strikeInterval.number,
    lotSize,
    marketKind,
  };

  return {
    selection,
    definition: marketDefinition,
    capturedAt,
    dataMode,
    inputs: buildInputs({
      analytics: analyticsRecord,
      capturedAt,
      chain: chainRecord,
      context,
      contract,
      market,
      readModel,
      receivedAt,
      selection,
      snapshotId,
      source,
      technicals,
    }),
    chain,
    analytics: {
      expectedMove: positiveDecimal(
        analyticsRecord.expected_move,
        "analytics.expected_move",
      ).number,
      expectedLow: positiveDecimal(
        analyticsRecord.expected_low,
        "analytics.expected_low",
      ).number,
      expectedHigh: positiveDecimal(
        analyticsRecord.expected_high,
        "analytics.expected_high",
      ).number,
      syntheticFutures: positiveDecimal(
        analyticsRecord.synthetic_futures,
        "analytics.synthetic_futures",
      ).number,
      trend: trendValue(
        record(analyticsRecord.trend, "analytics.trend").direction,
        "analytics.trend.direction",
      ),
      trendStrength: boundedNumber(
        record(analyticsRecord.trend, "analytics.trend").strength,
        "analytics.trend.strength",
        0,
        100,
      ),
      pcr: nonNegativeNumber(
        analyticsRecord.put_call_ratio,
        "analytics.put_call_ratio",
      ),
      changeOiPcr: optionalFiniteNumber(
        analyticsRecord.change_oi_put_call_ratio,
        "analytics.change_oi_put_call_ratio",
      ),
      support: positiveDecimal(analyticsRecord.support, "analytics.support").number,
      resistance: positiveDecimal(
        analyticsRecord.resistance,
        "analytics.resistance",
      ).number,
      atmIv:
        positiveNumber(
          analyticsRecord.atm_iv_decimal,
          "analytics.atm_iv_decimal",
        ) * 100,
      callScore: boundedNumber(
        analyticsRecord.call_score,
        "analytics.call_score",
        0,
        100,
      ),
      putScore: boundedNumber(
        analyticsRecord.put_score,
        "analytics.put_score",
        0,
        100,
      ),
      decision: decisionValue(
        dataMode === "STALE" ? operationalDecision : analyticsDecision,
        dataMode === "STALE"
          ? "read_model.operational_decision"
          : "analytics.decision",
      ),
      decisionReason:
        dataMode === "STALE"
          ? `Backend operational blockers: ${blockers.join(", ")}`
          : nonEmptyString(
              analyticsRecord.decision_reason,
              "analytics.decision_reason",
            ),
      signalGap: nonNegativeNumber(
        analyticsRecord.score_gap,
        "analytics.score_gap",
      ),
      // The workspace endpoint exposes the current coherent price, not historical bars.
      // Preserve that one exact observation instead of fabricating an intraday path.
      spotSeries: [pricingUnderlying.number],
    },
    ranking,
    backendAuthority: {
      snapshotId,
      contractKey,
      source,
      receivedAt,
      complete,
      fresh,
      actionable,
      blockers,
      warnings,
      validationAccepted,
    },
  };
}

function parseSelection(value: Record<string, unknown>): MarketSnapshot["selection"] {
  const rawMarket = nonEmptyString(value.market_id, "selection.market_id");
  if (!MARKET_IDS.has(rawMarket as MarketId)) {
    fail("INVALID_FIELD", "selection.market_id", `unsupported market ${rawMarket}`);
  }
  return {
    market: rawMarket as MarketId,
    symbol: nonEmptyString(value.symbol, "selection.symbol"),
    expiry: timestamp(value.expiry, "selection.expiry"),
  };
}

function validateFreshness(raw: unknown): void {
  const freshness = record(raw, "read_model.freshness");
  timestamp(freshness.evaluated_at, "read_model.freshness.evaluated_at");
  finiteNumber(
    freshness.oldest_component_age_seconds,
    "read_model.freshness.oldest_component_age_seconds",
  );
  finiteNumber(
    freshness.newest_component_age_seconds,
    "read_model.freshness.newest_component_age_seconds",
  );
  positiveNumber(
    freshness.maximum_age_seconds,
    "read_model.freshness.maximum_age_seconds",
  );
  nonNegativeNumber(
    freshness.future_clock_skew_seconds,
    "read_model.freshness.future_clock_skew_seconds",
  );
}

function validateMarketKind(
  market: MarketId,
  kind: MarketDefinition["marketKind"],
): void {
  const expected = market === "MCX" ? "COMMODITY" : market === "STOCK_FNO" ? "STOCK" : "INDEX";
  if (kind !== expected) {
    mismatch("contract.market_kind", `${market} cannot use market kind ${kind}`);
  }
}

function validateMarketUnderlying(
  market: Record<string, unknown>,
  kind: MarketDefinition["marketKind"],
  pricingUnderlying: number,
): void {
  timestamp(market.observed_at, "market.observed_at");
  const spot = optionalPositiveDecimal(market.spot_price, "market.spot_price");
  const future = positiveDecimal(market.futures_price, "market.futures_price");
  const selected = kind === "COMMODITY" ? future : spot;
  if (selected === null || !sameNumber(selected.number, pricingUnderlying)) {
    mismatch(
      "analytics.pricing_underlying",
      "analytics pricing underlying does not match the canonical market price",
    );
  }
}

function validateContractIdentity(
  contract: Record<string, unknown>,
): void {
  const underlying = record(contract.underlying, "contract.underlying");
  // Contract identity is provider-exact. Requested canonical selection identity
  // is independently checked against workspace.selection by useLiveMarketData.
  nonEmptyString(underlying.symbol, "contract.underlying.symbol");
  nonEmptyString(
    underlying.security_id,
    "contract.underlying.security_id",
  );
  nonEmptyString(underlying.exchange, "contract.underlying.exchange");
  nonEmptyString(underlying.segment, "contract.underlying.segment");

  const future = record(contract.futures, "contract.futures");
  const futureInstrument = record(
    future.instrument,
    "contract.futures.instrument",
  );
  nonEmptyString(
    futureInstrument.security_id,
    "contract.futures.instrument.security_id",
  );

  const master = record(contract.master, "contract.master");
  nonEmptyString(master.batch_id, "contract.master.batch_id");
  nonEmptyString(master.provider, "contract.master.provider");
  nonEmptyString(master.content_hash, "contract.master.content_hash");
  timestamp(master.fetched_at, "contract.master.fetched_at");
  positiveInteger(master.row_count, "contract.master.row_count");
}

function validateTechnicalState(technicals: Record<string, unknown>): void {
  timestamp(technicals.observed_at, "technicals.observed_at");
  if (!booleanValue(technicals.completed_candle, "technicals.completed_candle")) {
    fail(
      "INCOMPLETE_WORKSPACE",
      "technicals.completed_candle",
      "technical indicators must come from a completed candle",
    );
  }
  nonEmptyString(technicals.timeframe, "technicals.timeframe");
  positiveNumber(
    technicals.reference_volatility,
    "technicals.reference_volatility",
  );
}

function validateStrategyContext(context: Record<string, unknown>): void {
  oneOf(
    context.operating_mode,
    "context.operating_mode",
    ["QUICK", "PRO"] as const,
  );
  oneOf(
    context.trading_style,
    "context.trading_style",
    ["INTRADAY", "POSITIONAL"] as const,
  );
  const high = positiveDecimal(
    context.signal_candle_high,
    "context.signal_candle_high",
  ).number;
  const low = positiveDecimal(
    context.signal_candle_low,
    "context.signal_candle_low",
  ).number;
  if (high < low) {
    mismatch(
      "context.signal_candle_high",
      "signal candle high cannot be below its low",
    );
  }
  fraction(context.risk_per_trade, "context.risk_per_trade");
  fraction(
    context.maximum_premium_allocation,
    "context.maximum_premium_allocation",
  );
  positiveDecimal(context.account_capital, "context.account_capital");
  positiveNumber(
    context.expected_holding_hours,
    "context.expected_holding_hours",
  );
  triState(context.event_risk_active, "context.event_risk_active", "", "");
  triState(
    context.price_action_confirmed,
    "context.price_action_confirmed",
    "",
    "",
  );
}

function validateOptionContracts(
  raw: unknown,
  chain: readonly OptionStrike[],
  expiry: string,
  lotSize: number,
): ReadonlyMap<string, string> {
  const contracts = arrayValue(raw, "contract.option_contracts");
  if (contracts.length !== 10) {
    fail(
      "INCOMPLETE_WORKSPACE",
      "contract.option_contracts",
      "contract master must bind the exact ten option-chain legs",
    );
  }

  const expected = new Map<string, { strike: number; side: "CE" | "PE" }>();
  for (const row of chain) {
    expected.set(row.call.securityId, { strike: row.strike, side: "CE" });
    expected.set(row.put.securityId, { strike: row.strike, side: "PE" });
  }

  const observed = new Set<string>();
  const contractNames = new Map<string, string>();

  contracts.forEach((rawContract, index) => {
    const path = `contract.option_contracts[${index}]`;
    const contract = record(rawContract, path);
    const instrument = record(contract.instrument, `${path}.instrument`);
    const securityId = nonEmptyString(
      instrument.security_id,
      `${path}.instrument.security_id`,
    );
    const contractName = nonEmptyString(
      instrument.symbol,
      `${path}.instrument.symbol`,
    );

    const bound = expected.get(securityId);
    if (bound === undefined || observed.has(securityId)) {
      mismatch(
        `${path}.instrument.security_id`,
        "contract master identities do not match the coherent chain",
      );
    }

    observed.add(securityId);
    contractNames.set(securityId, contractName);

    if (
      optionSide(contract.option_type, `${path}.option_type`) !== bound.side ||
      !sameNumber(
        positiveDecimal(contract.strike, `${path}.strike`).number,
        bound.strike,
      ) ||
      timestamp(contract.expiry, `${path}.expiry`) !== expiry ||
      positiveInteger(contract.lot_size, `${path}.lot_size`) !== lotSize
    ) {
      mismatch(path, "contract master leg differs from its market quote");
    }
  });

  if (contractNames.size !== expected.size) {
    mismatch(
      "contract.option_contracts",
      "every coherent option-chain security ID must have one verified instrument symbol",
    );
  }

  return contractNames;
}

function parseRankings(raw: unknown): ReadonlyMap<string, ParsedRanking> {
  if (!Array.isArray(raw) || raw.length !== 10) {
    fail(
      "INCOMPLETE_WORKSPACE",
      "analytics.ranked_strikes",
      "exactly ten ranked option legs are required",
    );
  }
  const values = raw.map((item, index) =>
    parseRanking(record(item, `analytics.ranked_strikes[${index}]`), `analytics.ranked_strikes[${index}]`),
  );
  values.sort((left, right) => left.rank - right.rank);
  values.forEach((entry, index) => {
    if (entry.rank !== index + 1) {
      mismatch(
        `analytics.ranked_strikes[${index}].rank`,
        "ranking positions must be unique and contiguous from 1 to 10",
      );
    }
  });
  const result = new Map<string, ParsedRanking>();
  for (const item of values) {
    if (result.has(item.securityId)) {
      mismatch("analytics.ranked_strikes", "ranked security IDs must be unique");
    }
    result.set(item.securityId, item);
  }
  return result;
}

function parseRanking(value: Record<string, unknown>, path: string): ParsedRanking {
  const eligible = booleanValue(value.eligible, `${path}.eligible`);
  const rejectionReasons = stringArray(
    value.rejection_reasons,
    `${path}.rejection_reasons`,
  );
  if (eligible === (rejectionReasons.length > 0)) {
    mismatch(
      `${path}.eligible`,
      "ranking eligibility and rejection reasons contradict one another",
    );
  }
  return {
    rank: positiveInteger(value.rank, `${path}.rank`),
    securityId: nonEmptyString(value.security_id, `${path}.security_id`),
    strike: positiveDecimal(value.strike, `${path}.strike`).number,
    side: optionSide(value.option_type, `${path}.option_type`),
    entryAsk: positiveDecimal(value.entry_ask, `${path}.entry_ask`).number,
    score: boundedNumber(value.score, `${path}.score`, 0, 100),
    liquidityScore: boundedNumber(
      value.liquidity_score,
      `${path}.liquidity_score`,
      0,
      100,
    ),
    eligible,
    rejectionReasons,
  };
}

function parseChain(args: {
  chain: Record<string, unknown>;
  expiry: string;
  rankings: ReadonlyMap<string, ParsedRanking>;
  strikeInterval: number;
}): readonly OptionStrike[] {
  const { chain, expiry, rankings, strikeInterval } = args;
  if (positiveInteger(chain.leg_count, "chain.leg_count") !== 10) {
    fail(
      "INCOMPLETE_WORKSPACE",
      "chain.leg_count",
      "the frontend requires five strikes and ten legs",
    );
  }
  const missingLegs = arrayValue(chain.missing_legs, "chain.missing_legs");
  if (missingLegs.length > 0) {
    fail(
      "INCOMPLETE_WORKSPACE",
      "chain.missing_legs",
      "the option chain declares missing legs",
    );
  }
  const rows = arrayValue(chain.strikes, "chain.strikes");
  if (rows.length !== 5) {
    fail(
      "INCOMPLETE_WORKSPACE",
      "chain.strikes",
      "exactly five strikes are required",
    );
  }
  const atm = positiveDecimal(chain.atm_strike, "chain.atm_strike").number;
  const securityIds = new Set<string>();
  const parsed = rows.map((rawRow, index): OptionStrike => {
    const path = `chain.strikes[${index}]`;
    const row = record(rawRow, path);
    const moneyness = nonEmptyString(row.moneyness, `${path}.moneyness`);
    if (moneyness !== MONEYNESS[index]) {
      mismatch(
        `${path}.moneyness`,
        `five-strike rows must be ordered ${MONEYNESS.join(", ")}`,
      );
    }
    const strike = positiveDecimal(row.strike, `${path}.strike`).number;
    const expectedStrike = atm + (index - 2) * strikeInterval;
    if (!sameNumber(strike, expectedStrike)) {
      mismatch(`${path}.strike`, "strike is not on the declared five-strike ladder");
    }
    const call = parseLeg({
      raw: row.call,
      path: `${path}.call`,
      side: "CE",
      strike,
      expiry,
      rankings,
    });
    const put = parseLeg({
      raw: row.put,
      path: `${path}.put`,
      side: "PE",
      strike,
      expiry,
      rankings,
    });
    for (const leg of [call, put]) {
      if (securityIds.has(leg.securityId)) {
        mismatch(`${path}.${leg.side}.security_id`, "chain security IDs must be unique");
      }
      securityIds.add(leg.securityId);
    }
    return {
      strike,
      moneyness: DISPLAY_MONEYNESS[index],
      call,
      put,
    };
  });
  if (securityIds.size !== rankings.size) {
    mismatch(
      "analytics.ranked_strikes",
      "ranking must cover the exact ten option-chain security IDs",
    );
  }
  return parsed;
}

function parseLeg(args: {
  raw: unknown;
  path: string;
  side: "CE" | "PE";
  strike: number;
  expiry: string;
  rankings: ReadonlyMap<string, ParsedRanking>;
}): OptionLeg {
  const { raw, path, side, strike, expiry, rankings } = args;
  if (raw === null) {
    fail("INCOMPLETE_WORKSPACE", path, "option-chain leg is missing");
  }
  const value = record(raw, path);
  const securityId = nonEmptyString(value.security_id, `${path}.security_id`);
  const legStrike = positiveDecimal(value.strike, `${path}.strike`).number;
  if (!sameNumber(legStrike, strike)) {
    mismatch(`${path}.strike`, "leg strike does not match its chain row");
  }
  if (optionSide(value.option_type, `${path}.option_type`) !== side) {
    mismatch(`${path}.option_type`, "leg option type does not match its chain side");
  }
  if (timestamp(value.expiry, `${path}.expiry`) !== expiry) {
    mismatch(`${path}.expiry`, "leg expiry does not match the selected contract");
  }
  timestamp(value.observed_at, `${path}.observed_at`);

  const bid = positiveDecimal(value.bid, `${path}.bid`).number;
  const ask = positiveDecimal(value.ask, `${path}.ask`).number;
  const ltp = positiveDecimal(value.ltp, `${path}.ltp`).number;
  if (bid > ask) {
    mismatch(`${path}.ask`, "ask cannot be below bid");
  }
  const spread = nonNegativeDecimal(value.spread, `${path}.spread`).number;

  /*
   * bid, ask, spread and spread_ratio originate as canonical decimal
   * values in the Python backend, but JavaScript converts them to IEEE-754
   * numbers. Subtraction/division can therefore introduce tiny binary
   * floating-point differences even when the backend values are coherent.
   *
   * Keep strict sameNumber() checks for identity/topology fields, but use
   * a narrowly bounded tolerance for these two derived quote values.
   */
  if (!sameDerivedNumber(spread, ask - bid)) {
    mismatch(`${path}.spread`, "reported spread does not match bid and ask");
  }

  const spreadRatio = nonNegativeNumber(
    value.spread_ratio,
    `${path}.spread_ratio`,
  );

  if (!sameDerivedNumber(spreadRatio, spread / ask)) {
    mismatch(
      `${path}.spread_ratio`,
      "reported spread ratio does not match bid and ask",
    );
  }

  const ranking = rankings.get(securityId);
  if (ranking === undefined) {
    fail(
      "INCOMPLETE_WORKSPACE",
      `${path}.ranking`,
      "option leg has no corresponding analytical ranking",
    );
  }
  if (
    !sameNumber(ranking.strike, strike) ||
    ranking.side !== side ||
    !sameNumber(ranking.entryAsk, ask)
  ) {
    mismatch(`${path}.ranking`, "chain quote and analytical ranking differ");
  }
  validateEmbeddedRanking(value.ranking, ranking, `${path}.ranking`);

  const greeks = record(value.greeks, `${path}.greeks`);
  const theoreticalPrice = optionalNonNegativeDecimal(
    greeks.theoretical_price,
    `${path}.greeks.theoretical_price`,
  );
  return {
    side,
    securityId,
    bid,
    ask,
    ltp,
    volume: nonNegativeInteger(value.volume, `${path}.volume`),
    openInterest: nonNegativeInteger(
      value.open_interest,
      `${path}.open_interest`,
    ),
    changeOpenInterest: optionalIntegerValue(
      value.change_open_interest,
      `${path}.change_open_interest`,
    ),
    iv:
      positiveNumber(
        value.implied_volatility,
        `${path}.implied_volatility`,
      ) * 100,
    spreadPercent: spreadRatio * 100,
    liquidityScore: ranking.liquidityScore,
    strikeScore: ranking.score,
    greeks: {
      delta: boundedNumber(greeks.delta, `${path}.greeks.delta`, -1, 1),
      gamma: nonNegativeNumber(greeks.gamma, `${path}.greeks.gamma`),
      theta: finiteNumber(greeks.theta, `${path}.greeks.theta`),
      vega: nonNegativeNumber(greeks.vega, `${path}.greeks.vega`),
      theoreticalPrice: theoreticalPrice?.number ?? null,
    },
    rejectionReasons: ranking.rejectionReasons,
  };
}

function validateEmbeddedRanking(
  raw: unknown,
  expected: ParsedRanking,
  path: string,
): void {
  const actual = parseRanking(record(raw, path), path);
  if (
    actual.rank !== expected.rank ||
    actual.securityId !== expected.securityId ||
    !sameNumber(actual.strike, expected.strike) ||
    actual.side !== expected.side ||
    !sameNumber(actual.entryAsk, expected.entryAsk) ||
    !sameNumber(actual.score, expected.score) ||
    !sameNumber(actual.liquidityScore, expected.liquidityScore) ||
    actual.eligible !== expected.eligible ||
    actual.rejectionReasons.join("\u0000") !== expected.rejectionReasons.join("\u0000")
  ) {
    mismatch(path, "embedded and aggregate rankings differ");
  }
}

function buildRanking(
  chain: readonly OptionStrike[],
  rankings: ReadonlyMap<string, ParsedRanking>,
  contractNames: ReadonlyMap<string, string>,
  selection: WorkspaceSelection,
): readonly RankingEntry[] {
  const legs = new Map(
    chain.flatMap((row) => [
      [row.call.securityId, { strike: row.strike, leg: row.call }] as const,
      [row.put.securityId, { strike: row.strike, leg: row.put }] as const,
    ]),
  );

  return [...rankings.values()]
    .sort((left, right) => left.rank - right.rank)
    .map((entry): RankingEntry => {
      const matched = legs.get(entry.securityId);
      if (matched === undefined) {
        mismatch(
          "analytics.ranked_strikes",
          "ranking contains a security outside the five-strike chain",
        );
      }

      const providerContractName = contractNames.get(entry.securityId);
      if (providerContractName === undefined) {
        mismatch(
          "contract.option_contracts",
          "ranked option security has no verified Dhan instrument symbol",
        );
      }

      return {
        rank: entry.rank,
        side: entry.side,
        strike: matched.strike,
        // MCX master rows may expose only a generic underlying name. Build the
        // visible label solely from already verified contract fields; quote and
        // selection identity remain bound to the exact Dhan security ID above.
        contractName: `${selection.symbol} ${selection.expiry.slice(0, 10)} ${entry.strike} ${entry.side}`,
        score: entry.score,
        band: scoreBand(entry.score),
        askEntry: entry.entryAsk,
        bidExit: matched.leg.bid,
        spreadPercent: matched.leg.spreadPercent,
        liquidityScore: entry.liquidityScore,
        delta: matched.leg.greeks.delta,
        rejectionReasons: entry.rejectionReasons,
      };
    });
}

function buildInputs(args: {
  analytics: Record<string, unknown>;
  capturedAt: string;
  chain: Record<string, unknown>;
  context: Record<string, unknown>;
  contract: Record<string, unknown>;
  market: Record<string, unknown>;
  readModel: Record<string, unknown>;
  receivedAt: string;
  selection: MarketSnapshot["selection"];
  snapshotId: string;
  source: string;
  technicals: Record<string, unknown>;
}): readonly CalculatorInput[] {
  const {
    analytics,
    capturedAt,
    chain,
    context,
    contract,
    market,
    readModel,
    receivedAt,
    selection,
    snapshotId,
    source,
    technicals,
  } = args;
  const underlying = record(contract.underlying, "contract.underlying");
  const master = record(contract.master, "contract.master");
  const inputs: CalculatorInput[] = [
    liveInput("mode", "Operating mode", "SESSION", enumString(context.operating_mode, "context.operating_mode")),
    liveInput("symbol", "Symbol", "SESSION", selection.symbol),
    liveInput("option_expiry", "Option expiry", "SESSION", selection.expiry),
    liveInput("market_timestamp", "Market timestamp", "SESSION", capturedAt),
    liveInput("received_timestamp", "Backend received timestamp", "SESSION", receivedAt),
    liveInput("snapshot_id", "Snapshot ID", "SESSION", snapshotId),
    liveInput(
      "contract_key",
      "Contract key",
      "SESSION",
      nonEmptyString(contract.contract_key, "contract.contract_key"),
    ),
    liveInput(
      "underlying_security_id",
      "Underlying security ID",
      "SESSION",
      nonEmptyString(
        underlying.security_id,
        "contract.underlying.security_id",
      ),
    ),
    liveInput("data_source", "Provider source", "SESSION", source),
    liveInput(
      "master_batch",
      "Instrument-master batch",
      "SESSION",
      nonEmptyString(master.batch_id, "contract.master.batch_id"),
    ),
    liveInput(
      "master_fetched_at",
      "Instrument master fetched",
      "SESSION",
      timestamp(master.fetched_at, "contract.master.fetched_at"),
    ),
    computedInput(
      "oldest_component_age",
      "Oldest component age",
      "SESSION",
      `${finiteNumber(record(readModel.freshness, "read_model.freshness").oldest_component_age_seconds, "read_model.freshness.oldest_component_age_seconds")} seconds`,
    ),
  ];

  appendOptionalDecimal(inputs, "spot", "Spot price", "MARKET", market.spot_price, "market.spot_price");
  appendOptionalDecimal(inputs, "futures", "Exact futures", "MARKET", market.futures_price, "market.futures_price");
  appendOptionalDecimal(inputs, "previous_close", "Previous close", "MARKET", market.previous_close, "market.previous_close");
  appendOptionalDecimal(inputs, "day_open", "Day open", "MARKET", market.day_open, "market.day_open");
  appendOptionalDecimal(inputs, "day_high", "Day high", "MARKET", market.day_high, "market.day_high");
  appendOptionalDecimal(inputs, "day_low", "Day low", "MARKET", market.day_low, "market.day_low");
  appendOptionalDecimal(inputs, "vwap", "VWAP", "TECHNICAL", market.vwap, "market.vwap");

  inputs.push(
    liveInput("ema9", "EMA 9", "TECHNICAL", positiveDecimal(technicals.ema_9, "technicals.ema_9").text),
    liveInput("ema21", "EMA 21", "TECHNICAL", positiveDecimal(technicals.ema_21, "technicals.ema_21").text),
    liveInput("wma44", "WMA 44", "TECHNICAL", positiveDecimal(technicals.wma_44, "technicals.wma_44").text),
    liveInput("previous_wma44", "Previous WMA 44", "TECHNICAL", positiveDecimal(technicals.previous_wma_44, "technicals.previous_wma_44").text),
    liveInput("rsi14", "RSI 14", "TECHNICAL", String(boundedNumber(technicals.rsi_14, "technicals.rsi_14", 0, 100))),
    liveInput("atr14", "ATR 14", "TECHNICAL", positiveDecimal(technicals.atr_14, "technicals.atr_14").text),
    liveInput(
      "reference_volatility",
      "Reference volatility",
      "TECHNICAL",
      `${positiveNumber(technicals.reference_volatility, "technicals.reference_volatility") * 100}%`,
    ),
    liveInput("timeframe", "Completed-candle timeframe", "TECHNICAL", nonEmptyString(technicals.timeframe, "technicals.timeframe")),
    computedInput("atm", "ATM strike", "MARKET", positiveDecimal(chain.atm_strike, "chain.atm_strike").text),
    liveInput("capital", "Account capital", "RISK", positiveDecimal(context.account_capital, "context.account_capital").text),
    liveInput("risk_rate", "Risk per trade", "RISK", `${fraction(context.risk_per_trade, "context.risk_per_trade") * 100}%`),
    liveInput("premium_allocation", "Maximum premium allocation", "RISK", `${fraction(context.maximum_premium_allocation, "context.maximum_premium_allocation") * 100}%`),
    liveInput("style", "Trading style", "RISK", enumString(context.trading_style, "context.trading_style")),
    liveInput("event_risk", "Event risk", "RISK", triState(context.event_risk_active, "context.event_risk_active", "ACTIVE", "CLEAR")),
    liveInput("price_confirmation", "Price-action confirmation", "RISK", triState(context.price_action_confirmed, "context.price_action_confirmed", "CONFIRMED", "NOT CONFIRMED")),
    liveInput("signal_candle", "Signal candle H / L", "TECHNICAL", `${positiveDecimal(context.signal_candle_high, "context.signal_candle_high").text} / ${positiveDecimal(context.signal_candle_low, "context.signal_candle_low").text}`),
    liveInput("holding_hours", "Expected holding hours", "RISK", String(positiveNumber(context.expected_holding_hours, "context.expected_holding_hours"))),
    liveInput("lot_size", "Verified lot size", "RISK", String(positiveInteger(contract.lot_size, "contract.lot_size"))),
    computedInput("expected_move", "Expected move", "MARKET", positiveDecimal(analytics.expected_move, "analytics.expected_move").text),
  );
  return inputs;
}

function liveInput(
  id: string,
  label: string,
  group: CalculatorInput["group"],
  value: string,
): CalculatorInput {
  return {
    id,
    label,
    group,
    importedValue: value,
    effectiveValue: value,
    source: "LIVE FEED",
  };
}

function computedInput(
  id: string,
  label: string,
  group: CalculatorInput["group"],
  value: string,
): CalculatorInput {
  return {
    id,
    label,
    group,
    importedValue: "—",
    effectiveValue: value,
    source: "COMPUTED",
  };
}

function appendOptionalDecimal(
  inputs: CalculatorInput[],
  id: string,
  label: string,
  group: CalculatorInput["group"],
  raw: unknown,
  path: string,
): void {
  const parsed = optionalPositiveDecimal(raw, path);
  if (parsed !== null) inputs.push(liveInput(id, label, group, parsed.text));
}

function validateActionablePlan(args: {
  actionable: boolean;
  analytics: Record<string, unknown>;
  contractKey: string;
  decision: string;
  securityIds: ReadonlySet<string>;
  snapshotId: string;
}): void {
  if (!args.actionable) return;
  if (args.decision !== "BUY_CALL" && args.decision !== "BUY_PUT") {
    fail(
      "UNSAFE_WORKSPACE",
      "read_model.operational_decision",
      "an actionable workspace requires a buy decision",
    );
  }
  const plan = record(args.analytics.trade_plan, "analytics.trade_plan");
  if (!booleanValue(plan.actionable, "analytics.trade_plan.actionable")) {
    fail(
      "UNSAFE_WORKSPACE",
      "analytics.trade_plan.actionable",
      "read model is actionable but its trade plan is not",
    );
  }
  if (
    nonEmptyString(plan.snapshot_id, "analytics.trade_plan.snapshot_id") !==
    args.snapshotId
  ) {
    mismatch("analytics.trade_plan.snapshot_id", "trade plan snapshot ID differs");
  }
  if (
    nonEmptyString(plan.contract_key, "analytics.trade_plan.contract_key") !==
    args.contractKey
  ) {
    mismatch("analytics.trade_plan.contract_key", "trade plan contract key differs");
  }
  const securityId = nonEmptyString(
    plan.security_id,
    "analytics.trade_plan.security_id",
  );
  if (!args.securityIds.has(securityId)) {
    mismatch(
      "analytics.trade_plan.security_id",
      "trade plan security is outside the coherent chain",
    );
  }
}

function securityIdFor(
  chain: readonly OptionStrike[],
  ranking: Pick<RankingEntry, "side" | "strike">,
): string {
  const row = chain.find((item) => sameNumber(item.strike, ranking.strike));
  if (row === undefined) {
    mismatch("ranking.strike", "ranking strike is outside the chain");
  }
  return ranking.side === "CE" ? row.call.securityId : row.put.securityId;
}

function record(value: unknown, path: string): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    fail("INVALID_FIELD", path, "expected an object");
  }
  return value as Record<string, unknown>;
}

function arrayValue(value: unknown, path: string): readonly unknown[] {
  if (!Array.isArray(value)) fail("INVALID_FIELD", path, "expected an array");
  return value;
}

function stringArray(value: unknown, path: string): readonly string[] {
  const items = arrayValue(value, path);
  return items.map((item, index) =>
    nonEmptyString(item, `${path}[${index}]`),
  );
}

function nonEmptyString(value: unknown, path: string): string {
  if (typeof value !== "string" || value.length === 0 || value.trim() !== value) {
    fail("INVALID_FIELD", path, "expected a non-empty canonical string");
  }
  return value;
}

function enumString(value: unknown, path: string): string {
  return nonEmptyString(value, path);
}

function oneOf<const Value extends string>(
  value: unknown,
  path: string,
  allowed: readonly Value[],
): Value {
  const selected = nonEmptyString(value, path);
  if (!allowed.includes(selected as Value)) {
    fail(
      "INVALID_FIELD",
      path,
      `expected one of ${allowed.join(", ")}`,
    );
  }
  return selected as Value;
}

function timestamp(value: unknown, path: string): string {
  const selected = nonEmptyString(value, path);
  if (!/(?:Z|[+-]\d{2}:\d{2})$/.test(selected) || Number.isNaN(Date.parse(selected))) {
    fail("INVALID_FIELD", path, "expected a timezone-aware ISO timestamp");
  }
  return selected;
}

function booleanValue(value: unknown, path: string): boolean {
  if (typeof value !== "boolean") fail("INVALID_FIELD", path, "expected a boolean");
  return value;
}

function triState(
  value: unknown,
  path: string,
  whenTrue: string,
  whenFalse: string,
): string {
  if (value === null) return "UNKNOWN";
  return booleanValue(value, path) ? whenTrue : whenFalse;
}

function finiteNumber(value: unknown, path: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    fail("INVALID_FIELD", path, "expected a finite number");
  }
  return value;
}

function optionalFiniteNumber(value: unknown, path: string): number | null {
  if (value === null) return null;
  return finiteNumber(value, path);
}

function positiveNumber(value: unknown, path: string): number {
  const selected = finiteNumber(value, path);
  if (selected <= 0) fail("INVALID_FIELD", path, "expected a positive number");
  return selected;
}

function nonNegativeNumber(value: unknown, path: string): number {
  const selected = finiteNumber(value, path);
  if (selected < 0) fail("INVALID_FIELD", path, "expected a non-negative number");
  return selected;
}

function boundedNumber(
  value: unknown,
  path: string,
  minimum: number,
  maximum: number,
): number {
  const selected = finiteNumber(value, path);
  if (selected < minimum || selected > maximum) {
    fail("INVALID_FIELD", path, `expected a number within ${minimum}..${maximum}`);
  }
  return selected;
}

function fraction(value: unknown, path: string): number {
  const selected = finiteNumber(value, path);
  if (selected <= 0 || selected > 1) {
    fail("INVALID_FIELD", path, "expected a fraction greater than 0 and at most 1");
  }
  return selected;
}

function integerValue(value: unknown, path: string): number {
  const selected = finiteNumber(value, path);
  if (!Number.isSafeInteger(selected)) {
    fail("INVALID_FIELD", path, "expected a safe integer");
  }
  return selected;
}

function optionalIntegerValue(value: unknown, path: string): number | null {
  if (value === null) return null;
  return integerValue(value, path);
}

function positiveInteger(value: unknown, path: string): number {
  const selected = integerValue(value, path);
  if (selected <= 0) fail("INVALID_FIELD", path, "expected a positive integer");
  return selected;
}

function nonNegativeInteger(value: unknown, path: string): number {
  const selected = integerValue(value, path);
  if (selected < 0) fail("INVALID_FIELD", path, "expected a non-negative integer");
  return selected;
}

function decimal(value: unknown, path: string): ParsedDecimal {
  if (typeof value !== "string" && typeof value !== "number") {
    fail("INVALID_FIELD", path, "expected a decimal string or number");
  }
  const text = typeof value === "number" ? String(value) : value;
  if (text.length === 0 || text.trim() !== text) {
    fail("INVALID_FIELD", path, "expected a canonical decimal value");
  }
  if (!/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(text)) {
    fail("INVALID_FIELD", path, "expected a decimal value");
  }
  const number = Number(text);
  if (!Number.isFinite(number)) {
    fail("INVALID_FIELD", path, "expected a finite decimal value");
  }
  return { number, text };
}

function positiveDecimal(value: unknown, path: string): ParsedDecimal {
  const selected = decimal(value, path);
  if (selected.number <= 0) fail("INVALID_FIELD", path, "expected a positive decimal");
  return selected;
}

function nonNegativeDecimal(value: unknown, path: string): ParsedDecimal {
  const selected = decimal(value, path);
  if (selected.number < 0) {
    fail("INVALID_FIELD", path, "expected a non-negative decimal");
  }
  return selected;
}

function optionalPositiveDecimal(
  value: unknown,
  path: string,
): ParsedDecimal | null {
  return value === null ? null : positiveDecimal(value, path);
}

function optionalNonNegativeDecimal(
  value: unknown,
  path: string,
): ParsedDecimal | null {
  return value === null ? null : nonNegativeDecimal(value, path);
}

function optionSide(value: unknown, path: string): "CE" | "PE" {
  if (value !== "CE" && value !== "PE") {
    fail("INVALID_FIELD", path, "expected CE or PE");
  }
  return value;
}

function marketKindValue(
  value: unknown,
  path: string,
): MarketDefinition["marketKind"] {
  if (value !== "INDEX" && value !== "STOCK" && value !== "COMMODITY") {
    fail("INVALID_FIELD", path, "unsupported market kind");
  }
  return value;
}

function trendValue(
  value: unknown,
  path: string,
): MarketSnapshot["analytics"]["trend"] {
  if (value !== "BULLISH" && value !== "BEARISH" && value !== "MIXED") {
    fail("INVALID_FIELD", path, "unsupported trend direction");
  }
  return value;
}

function decisionValue(value: unknown, path: string): Decision {
  const mapping: Readonly<Record<string, Decision>> = {
    BUY_CALL: "BUY CALL",
    BUY_PUT: "BUY PUT",
    WAIT: "WAIT",
    NO_TRADE: "NO TRADE",
    INSUFFICIENT_DATA: "INSUFFICIENT DATA",
  };
  const selected = typeof value === "string" ? mapping[value] : undefined;
  if (selected === undefined) fail("INVALID_FIELD", path, "unsupported decision");
  return selected;
}

function sameNumber(left: number, right: number): boolean {
  const scale = Math.max(1, Math.abs(left), Math.abs(right));
  return Math.abs(left - right) <= Number.EPSILON * scale * 16;
}

/*
 * Derived market values such as ask - bid and spread / ask are calculated
 * after decimal values cross the JSON -> JavaScript number boundary.
 *
 * A tolerance of 1e-6 is tiny for quoted option prices/ratios and prevents
 * false rejections caused solely by IEEE-754 arithmetic. Material
 * inconsistencies (for example a 0.01 price mismatch) still fail closed.
 *
 * Do NOT use this helper for contract identity, strikes, expiry, ranking
 * identity, or other canonical equality checks.
 */
function sameDerivedNumber(left: number, right: number): boolean {
  const scale = Math.max(1, Math.abs(left), Math.abs(right));
  const tolerance = Math.max(1e-6, Number.EPSILON * scale * 64);
  return Math.abs(left - right) <= tolerance;
}

function mismatch(path: string, message: string): never {
  return fail("INCOHERENT_WORKSPACE", path, message);
}

function fail(
  code: BackendSnapshotAdapterErrorCode,
  path: string,
  message: string,
): never {
  throw new BackendSnapshotAdapterError(code, path, message);
}
