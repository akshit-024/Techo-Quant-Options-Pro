import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { MarketSelection } from "../api/contracts";
import { useLiveMarketData } from "./useLiveMarketData";

const SELECTION: MarketSelection = {
  market_id: "NIFTY",
  symbol: "NIFTY",
  expiry: "2026-08-27T15:30:00+05:30",
};

const READ_MODEL = {
  snapshot_id: "snapshot-7",
  contract_key: "NIFTY:2026-08-27",
  sequence: 7,
  source: "DHAN_REST",
  captured_at: "2026-08-25T10:00:00+00:00",
  received_at: "2026-08-25T10:00:01+00:00",
  data_mode: "LIVE",
  complete: true,
  fresh: true,
  actionable: false,
  operational_decision: "WAIT",
  blockers: [],
  warnings: [],
  freshness: {
    evaluated_at: "2026-08-25T10:00:02+00:00",
    oldest_component_age_seconds: 2,
    newest_component_age_seconds: 1,
    maximum_age_seconds: 30,
    future_clock_skew_seconds: 2,
  },
};

const STATUS = {
  mode: "DATA_ONLY",
  live_enabled: false,
  live_gateway_configured: false,
  kill_switch: {
    active: false,
    reason: null,
    actor: null,
    changed_at: "2026-08-25T09:00:00+00:00",
  },
  counts: { signals: 0, approvals: 0, orders: 0, fills: 0, positions: 0 },
  market_data: {
    read_model_configured: true,
    revision: 7,
    feed: {
      configured: true,
      state: "HEALTHY",
      connected: true,
      healthy: true,
      transport_healthy: true,
      data_healthy: true,
      decision_inputs_configured: false,
      actionable_ready: false,
    },
  },
};

const CATALOG = {
  generated_at: "2026-08-25T10:00:02+00:00",
  markets: [
    {
      market_id: "NIFTY",
      symbols: [
        { symbol: "NIFTY", expiries: [SELECTION.expiry], latest: READ_MODEL },
      ],
    },
  ],
};

const WORKSPACE = {
  read_model: READ_MODEL,
  selection: SELECTION,
  contract: {
    contract_key: "NIFTY:2026-08-27",
    underlying: { security_id: "13", symbol: "NIFTY" },
    market_kind: "INDEX",
    pricing_model: "BLACK_SCHOLES",
    option_expiry: SELECTION.expiry,
    lot_size: 75,
    strike_interval: "50",
    tick_size: "0.05",
    master: { batch_id: "master-1" },
    futures: null,
    option_contracts: [],
  },
  market: { spot: "25005" },
  technicals: { ema_9: "25000" },
  context: { event_risk_active: false },
  chain: {
    atm_strike: "25000",
    strike_interval: "50",
    leg_count: 2,
    strikes: [
      {
        strike: "25000",
        moneyness: "ATM",
        call: { security_id: "101", strike: "25000", option_type: "CE" },
        put: { security_id: "102", strike: "25000", option_type: "PE" },
      },
    ],
    missing_legs: [],
  },
  analytics: {
    pricing_underlying: "25005",
    expected_move: "150",
    expected_low: "24855",
    expected_high: "25155",
    synthetic_futures: "25010",
    put_call_ratio: 1.1,
    change_oi_put_call_ratio: 0.9,
    support: "24900",
    resistance: "25100",
    atm_iv_decimal: 0.15,
    trend: { direction: "BULLISH", strength: 100 },
    call_score: 7,
    put_score: 2,
    score_gap: 5,
    decision: "WAIT",
    decision_reason: "CONFIRMATION_REQUIRED",
    ranked_strikes: [],
    trade_plan: null,
    generated_at: "2026-08-25T10:00:02+00:00",
  },
  validation: {
    accepted: true,
    snapshot_id: "snapshot-7",
    snapshot_hash: "a".repeat(64),
    issues: [],
  },
};

afterEach(() => {
  vi.restoreAllMocks();
});

describe("useLiveMarketData", () => {
  it("performs zero requests when explicit demo mode disables live data", () => {
    const fetchMock = vi.fn();
    const { result } = renderHook(() =>
      useLiveMarketData(SELECTION, {
        enabled: false,
        baseUrl: "https://backend.example",
        fetchImpl: fetchMock as typeof fetch,
      }),
    );

    expect(result.current.connection).toBe("DISABLED");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("reports missing Dhan configuration before requesting the catalog", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({
        ...STATUS,
        market_data: {
          ...STATUS.market_data,
          feed: {
            ...STATUS.market_data.feed,
            state: "CONFIG_REQUIRED",
            connected: false,
            healthy: false,
          },
        },
      }),
    );
    const { result } = renderHook(() =>
      useLiveMarketData(SELECTION, {
        baseUrl: "https://backend.example",
        fetchImpl: fetchMock as typeof fetch,
      }),
    );

    await waitFor(() => expect(result.current.connection).toBe("CONFIG_REQUIRED"));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(result.current.error).toMatch(/Dhan credentials/i);
  });

  it("publishes the catalog and still attempts the requested workspace for an expired startup selection", async () => {
    const fetchMock = routeFetch({
      "/status": STATUS,
      "/markets": CATALOG,
    });
    const expired: MarketSelection = { ...SELECTION, expiry: "2020-01-01" };
    const { result } = renderHook(() =>
      useLiveMarketData(expired, {
        baseUrl: "https://backend.example",
        fetchImpl: fetchMock as typeof fetch,
      }),
    );

    await waitFor(() => expect(result.current.connection).toBe("ERROR"));
    expect(result.current.catalog?.markets[0]?.symbols[0]?.expiries).toEqual([
      SELECTION.expiry,
    ]);
    expect(result.current.workspace).toBeNull();
    expect(fetchMock.mock.calls.map(([input]) => new URL(String(input)).pathname)).toEqual([
      "/status",
      "/markets",
      "/market/workspace",
    ]);
  });

  it("loads one coherent workspace, keeps it stale on refresh failure, and never overlaps", async () => {
    let workspaceCalls = 0;
    let activeRequests = 0;
    let maximumActiveRequests = 0;
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      activeRequests += 1;
      maximumActiveRequests = Math.max(maximumActiveRequests, activeRequests);
      const path = new URL(String(input)).pathname;
      if (path === "/market/updates") {
        return new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener(
            "abort",
            () => {
              activeRequests -= 1;
              reject(new DOMException("aborted", "AbortError"));
            },
            { once: true },
          );
        });
      }
      const body =
        path === "/status"
          ? STATUS
          : path === "/markets"
            ? CATALOG
            : path === "/market/workspace"
              ? WORKSPACE
              : undefined;
      workspaceCalls += Number(path === "/market/workspace");
      activeRequests -= 1;
      if (path === "/market/workspace" && workspaceCalls > 1) {
        return Promise.reject(new TypeError("offline"));
      }
      return Promise.resolve(jsonResponse(body));
    });
    const { result, unmount } = renderHook(() =>
      useLiveMarketData(SELECTION, {
        baseUrl: "https://backend.example",
        retryDelayMs: 60_000,
        fetchImpl: fetchMock as typeof fetch,
      }),
    );

    await waitFor(() => expect(result.current.connection).toBe("LIVE"));
    const lastGood = result.current.workspace;
    expect(lastGood?.selection).toEqual(SELECTION);
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          ([input]) => new URL(String(input)).pathname === "/market/updates",
        ),
      ).toBe(true),
    );

    act(() => result.current.refresh());
    await waitFor(() => expect(result.current.connection).toBe("STALE"));
    expect(result.current.workspace).toBe(lastGood);
    expect(result.current.error).toMatch(/failed/i);
    expect(maximumActiveRequests).toBe(1);

    unmount();
  });
});

function routeFetch(responses: Readonly<Record<string, unknown>>): ReturnType<typeof vi.fn> {
  return vi.fn(async (input: RequestInfo | URL) => {
    const path = new URL(String(input)).pathname;
    return jsonResponse(responses[path]);
  });
}

function jsonResponse(value: unknown): Response {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}
