import type {
  MarketChainLeg,
  MarketJsonRecord,
  MarketWorkspaceResponse,
} from "../api/contracts";
import {
  adaptBackendSnapshot,
  BackendSnapshotAdapterError,
  type BackendSnapshotAdapterErrorCode,
} from "./backendSnapshot";

const CAPTURED_AT = "2026-08-29T10:00:00+00:00";
const RECEIVED_AT = "2026-08-29T10:00:01+00:00";
const EXPIRY = "2026-09-05T10:00:00+00:00";
const SNAPSHOT_ID = "snapshot-live-1";
const CONTRACT_KEY = "contract:live-1";
const STRIKES = [24700, 24750, 24800, 24850, 24900] as const;
const MONEYNESS = ["ATM-2", "ATM-1", "ATM", "ATM+1", "ATM+2"] as const;

interface FixtureLeg {
  readonly leg: MarketChainLeg;
  readonly ranking: MarketJsonRecord;
  readonly master: MarketJsonRecord;
}

function fixtureLeg(
  strike: number,
  side: "CE" | "PE",
  rank: number,
): FixtureLeg {
  const securityId = `${side}-${strike}`;
  const bid = 100 + rank;
  const ask = bid + 1;
  const ranking: MarketJsonRecord = {
    rank,
    security_id: securityId,
    strike: String(strike),
    option_type: side,
    entry_ask: String(ask),
    score: 90 - rank,
    evidence: { factors: {}, points: {}, total: 90 - rank },
    liquidity_score: 95 - rank,
    eligible: true,
    rejection_reasons: [],
  };
  return {
    leg: {
      security_id: securityId,
      strike: String(strike),
      option_type: side,
      expiry: EXPIRY,
      bid: String(bid),
      ask: String(ask),
      ltp: String(bid + 0.5),
      volume: 2_000 + rank,
      open_interest: 5_000 + rank,
      previous_open_interest: 4_900 + rank,
      change_open_interest: 100,
      implied_volatility: 0.18 + rank / 10_000,
      greeks: {
        delta: side === "CE" ? 0.55 : -0.45,
        gamma: 0.001,
        theta: -10,
        vega: 12,
        theoretical_price: null,
      },
      observed_at: CAPTURED_AT,
      spread: "1",
      spread_ratio: 1 / ask,
      ranking,
    },
    ranking,
    master: {
      instrument: {
        exchange: "NSE",
        segment: "NSE_FNO",
        security_id: securityId,
        symbol: `NIFTY-${strike}-${side}`,
      },
      display_name: `NIFTY ${strike} ${side}`,
      instrument_type: "OPTIDX",
      underlying_security_id: "13",
      expiry: EXPIRY,
      strike: String(strike),
      option_type: side,
      lot_size: 75,
      tick_size: "0.05",
    },
  };
}

function validWorkspace(): MarketWorkspaceResponse {
  const rankings: MarketJsonRecord[] = [];
  const optionContracts: MarketJsonRecord[] = [];
  let rank = 0;
  const strikes = STRIKES.map((strike, index) => {
    const call = fixtureLeg(strike, "CE", ++rank);
    const put = fixtureLeg(strike, "PE", ++rank);
    rankings.push(call.ranking, put.ranking);
    optionContracts.push(call.master, put.master);
    return {
      strike: String(strike),
      moneyness: MONEYNESS[index],
      call: call.leg,
      put: put.leg,
    };
  });

  return {
    read_model: {
      snapshot_id: SNAPSHOT_ID,
      contract_key: CONTRACT_KEY,
      sequence: 2,
      source: "DHAN_REST",
      captured_at: CAPTURED_AT,
      received_at: RECEIVED_AT,
      data_mode: "LIVE",
      complete: true,
      fresh: true,
      actionable: false,
      operational_decision: "WAIT",
      blockers: [],
      warnings: ["WIDE_SPREAD"],
      freshness: {
        evaluated_at: RECEIVED_AT,
        oldest_component_age_seconds: 1,
        newest_component_age_seconds: 1,
        maximum_age_seconds: 30,
        future_clock_skew_seconds: 2,
      },
    },
    selection: {
      market_id: "NIFTY",
      symbol: "NIFTY",
      expiry: EXPIRY,
    },
    contract: {
      contract_key: CONTRACT_KEY,
      underlying: {
        exchange: "NSE",
        segment: "IDX_I",
        security_id: "13",
        symbol: "NIFTY",
      },
      market_kind: "INDEX",
      pricing_model: "BLACK_SCHOLES",
      option_expiry: EXPIRY,
      lot_size: 75,
      strike_interval: "50",
      tick_size: "0.05",
      master: {
        batch_id: "DHAN:master-1",
        provider: "DHAN",
        source_url: "https://images.dhan.co/master.csv",
        content_hash: "f".repeat(64),
        schema_version: "dhan-detailed-v1",
        fetched_at: "2026-08-29T00:00:00+00:00",
        row_count: 50_000,
      },
      futures: {
        instrument: {
          exchange: "NSE",
          segment: "NSE_FNO",
          security_id: "FUT-1",
          symbol: "NIFTY-AUG-FUT",
        },
      },
      option_contracts: optionContracts,
    },
    market: {
      observed_at: CAPTURED_AT,
      spot_price: "24800",
      futures_price: "24835",
      previous_close: "24700",
      day_open: "24750",
      day_high: "24900",
      day_low: "24650",
      vwap: "24810",
      futures_open_interest: 100_000,
    },
    technicals: {
      observed_at: CAPTURED_AT,
      ema_9: "24820",
      ema_21: "24780",
      wma_44: "24760",
      previous_wma_44: "24740",
      rsi_14: 60,
      atr_14: "180",
      reference_volatility: 0.12,
      timeframe: "15m",
      completed_candle: true,
    },
    context: {
      operating_mode: "PRO",
      trading_style: "INTRADAY",
      account_capital: "500000",
      risk_per_trade: 0.01,
      maximum_premium_allocation: 0.25,
      event_risk_active: false,
      price_action_confirmed: null,
      signal_candle_high: "24850",
      signal_candle_low: "24750",
      expected_holding_hours: 6,
    },
    chain: {
      atm_strike: "24800",
      strike_interval: "50",
      leg_count: 10,
      strikes,
      missing_legs: [],
    },
    analytics: {
      pricing_underlying: "24800",
      expected_move: "200",
      expected_low: "24600",
      expected_high: "25000",
      synthetic_futures: "24801",
      put_call_ratio: 1.04,
      change_oi_put_call_ratio: 0.96,
      support: "24750",
      resistance: "24850",
      atm_iv_decimal: 0.185,
      trend: { direction: "BULLISH", strength: 75 },
      call_score: 89,
      put_score: 88,
      score_gap: 1,
      decision: "WAIT",
      decision_reason: "CONFLICTING_SCORES",
      ranked_strikes: rankings,
      trade_plan: null,
      generated_at: RECEIVED_AT,
    },
    validation: {
      accepted: true,
      snapshot_id: SNAPSHOT_ID,
      snapshot_hash: "a".repeat(64),
      issues: [],
    },
  };
}

function expectAdapterError(
  action: () => unknown,
  code: BackendSnapshotAdapterErrorCode,
  path: string,
): void {
  try {
    action();
    throw new Error("expected adapter to fail closed");
  } catch (error) {
    expect(error).toBeInstanceOf(BackendSnapshotAdapterError);
    const adapterError = error as BackendSnapshotAdapterError;
    expect(adapterError.code).toBe(code);
    expect(adapterError.path).toBe(path);
  }
}

describe("adaptBackendSnapshot", () => {
  it("maps one coherent live workspace without demo or synthetic market values", () => {
    const snapshot = adaptBackendSnapshot(validWorkspace());

    expect(snapshot.dataMode).toBe("LIVE");
    expect(snapshot.selection).toEqual({
      market: "NIFTY",
      symbol: "NIFTY",
      expiry: EXPIRY,
    });
    expect(snapshot.definition).toMatchObject({
      id: "NIFTY",
      baseSpot: 24_800,
      strikeStep: 50,
      lotSize: 75,
      marketKind: "INDEX",
    });
    expect(snapshot.chain).toHaveLength(5);
    expect(snapshot.chain.map((row) => row.moneyness)).toEqual([
      "ATM−2",
      "ATM−1",
      "ATM",
      "ATM+1",
      "ATM+2",
    ]);
    expect(snapshot.chain[0].call).toMatchObject({
      securityId: "CE-24700",
      bid: 101,
      ask: 102,
      liquidityScore: 94,
      strikeScore: 89,
    });
    expect(snapshot.chain[0].call.iv).toBeCloseTo(18.01);
    expect(snapshot.chain[0].call.greeks.theoreticalPrice).toBeNull();
    expect(snapshot.chain[0].call.spreadPercent).toBeCloseTo((1 / 102) * 100);
    expect(snapshot.ranking).toHaveLength(10);
    expect(snapshot.ranking[0]).toMatchObject({
      rank: 1,
      side: "CE",
      strike: 24_700,
      score: 89,
      band: "STRONG",
      askEntry: 102,
      bidExit: 101,
    });
    expect(snapshot.analytics).toMatchObject({
      expectedMove: 200,
      trend: "BULLISH",
      trendStrength: 75,
      atmIv: 18.5,
      decision: "WAIT",
      decisionReason: "CONFLICTING_SCORES",
      spotSeries: [24_800],
    });
    expect(snapshot.inputs.find((input) => input.id === "spot")).toMatchObject({
      importedValue: "24800",
      effectiveValue: "24800",
      source: "LIVE FEED",
    });
    expect(snapshot.inputs.find((input) => input.id === "atm")?.source).toBe(
      "COMPUTED",
    );
    expect(snapshot.backendAuthority).toEqual({
      snapshotId: SNAPSHOT_ID,
      contractKey: CONTRACT_KEY,
      source: "DHAN_REST",
      receivedAt: RECEIVED_AT,
      complete: true,
      fresh: true,
      actionable: false,
      blockers: [],
      warnings: ["WIDE_SPREAD"],
      validationAccepted: true,
    });
  });

  it("preserves an accepted stale snapshot but enforces the backend no-trade gate", () => {
    const workspace = validWorkspace();
    workspace.read_model.data_mode = "STALE";
    workspace.read_model.fresh = false;
    workspace.read_model.actionable = false;
    workspace.read_model.operational_decision = "NO_TRADE";
    workspace.read_model.blockers = ["STALE_MARKET_DATA"];
    workspace.read_model.freshness.oldest_component_age_seconds = 45;

    const snapshot = adaptBackendSnapshot(workspace);

    expect(snapshot.dataMode).toBe("STALE");
    expect(snapshot.analytics.decision).toBe("NO TRADE");
    expect(snapshot.analytics.decisionReason).toBe(
      "Backend operational blockers: STALE_MARKET_DATA",
    );
    expect(snapshot.backendAuthority).toMatchObject({
      fresh: false,
      actionable: false,
      blockers: ["STALE_MARKET_DATA"],
    });
  });

  it("rejects incomplete, rejected, and non-live workspaces", () => {
    const incomplete = validWorkspace();
    incomplete.read_model.complete = false;
    expectAdapterError(
      () => adaptBackendSnapshot(incomplete),
      "INCOMPLETE_WORKSPACE",
      "read_model.complete",
    );

    const rejected = validWorkspace();
    rejected.validation.accepted = false;
    expectAdapterError(
      () => adaptBackendSnapshot(rejected),
      "INCOMPLETE_WORKSPACE",
      "validation.accepted",
    );

    const nonLive = validWorkspace();
    nonLive.read_model.source = "MANUAL";
    expectAdapterError(
      () => adaptBackendSnapshot(nonLive),
      "NON_LIVE_WORKSPACE",
      "read_model.source",
    );
  });

  it("rejects missing legs instead of filling them with demo values", () => {
    const workspace = validWorkspace();
    workspace.chain.strikes[0].call = null;

    expectAdapterError(
      () => adaptBackendSnapshot(workspace),
      "INCOMPLETE_WORKSPACE",
      "chain.strikes[0].call",
    );
  });

  it("rejects cross-component identity and ranking mismatches", () => {
    const wrongSnapshot = validWorkspace();
    wrongSnapshot.validation.snapshot_id = "different-snapshot";
    expectAdapterError(
      () => adaptBackendSnapshot(wrongSnapshot),
      "INCOHERENT_WORKSPACE",
      "validation.snapshot_id",
    );

    const wrongRanking = validWorkspace();
    const ranking = wrongRanking.analytics.ranked_strikes[0] as Record<
      string,
      unknown
    >;
    ranking.entry_ask = "999";
    expectAdapterError(
      () => adaptBackendSnapshot(wrongRanking),
      "INCOHERENT_WORKSPACE",
      "chain.strikes[0].call.ranking",
    );
  });

  it("rejects malformed decimals and never permits stale actionability", () => {
    const malformed = validWorkspace();
    const call = malformed.chain.strikes[0].call as MarketChainLeg;
    call.bid = "NaN";
    expectAdapterError(
      () => adaptBackendSnapshot(malformed),
      "INVALID_FIELD",
      "chain.strikes[0].call.bid",
    );

    const unsafe = validWorkspace();
    unsafe.read_model.data_mode = "STALE";
    unsafe.read_model.fresh = false;
    unsafe.read_model.actionable = true;
    unsafe.read_model.blockers = ["STALE_MARKET_DATA"];
    expectAdapterError(
      () => adaptBackendSnapshot(unsafe),
      "UNSAFE_WORKSPACE",
      "read_model.actionable",
    );
  });
});
