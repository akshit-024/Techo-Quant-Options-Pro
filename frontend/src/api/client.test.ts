import { describe, expect, it, vi } from "vitest";

import {
  BackendApiError,
  fetchBackendSnapshot,
  fetchMarketAnalytics,
  fetchMarketCatalog,
  fetchMarketChain,
  fetchMarketContract,
  fetchMarketWorkspace,
  normalizeApiBaseUrl,
  waitForMarketUpdates,
} from "./client";
import type { MarketSelection } from "./contracts";

const RESPONSES: Readonly<Record<string, unknown>> = {
  "/health": { status: "ok", mode: "DATA_ONLY", live_locked: true },
  "/status": {
    mode: "DATA_ONLY",
    live_enabled: false,
    live_gateway_configured: false,
    kill_switch: {
      active: false,
      reason: null,
      actor: null,
      changed_at: "2026-08-21T06:00:00+00:00",
    },
    counts: { signals: 1, approvals: 0, orders: 0, fills: 0, positions: 0 },
  },
  "/signals/latest": { signal: null },
  "/paper/positions": { positions: [] },
  "/journal/summary": {
    journal_entries: 0,
    orders: 0,
    closed_positions: 0,
    realized_pnl: "0",
  },
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
} as const;

const SELECTION: MarketSelection = {
  market_id: "NIFTY",
  symbol: "NIFTY",
  expiry: "2026-08-27T15:30:00+05:30",
};

const CONTRACT = {
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
} as const;

const CHAIN = {
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
} as const;

const ANALYTICS = {
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
} as const;

const WORKSPACE = {
  read_model: READ_MODEL,
  selection: SELECTION,
  contract: CONTRACT,
  market: { spot: "25005" },
  technicals: { ema_9: "25000" },
  context: { event_risk_active: false },
  chain: CHAIN,
  analytics: ANALYTICS,
  validation: {
    accepted: true,
    snapshot_id: "snapshot-7",
    snapshot_hash: "a".repeat(64),
    issues: [],
  },
} as const;

function successfulFetch(): ReturnType<typeof vi.fn> {
  return vi.fn(async (input: RequestInfo | URL) => {
    const path = new URL(String(input)).pathname;
    return new Response(JSON.stringify(RESPONSES[path]), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  });
}

describe("read-only backend API client", () => {
  it("calls exactly the five approved GET routes without credentials or an API key", async () => {
    const fetchMock = successfulFetch();
    const snapshot = await fetchBackendSnapshot({
      baseUrl: "https://backend.example/",
      fetchImpl: fetchMock as typeof fetch,
      now: () => new Date("2026-08-21T06:15:00.000Z"),
    });

    expect(snapshot.fetchedAt).toBe("2026-08-21T06:15:00.000Z");
    expect(snapshot.health.live_locked).toBe(true);
    expect(fetchMock.mock.calls.map(([url]) => String(url))).toEqual([
      "https://backend.example/health",
      "https://backend.example/status",
      "https://backend.example/signals/latest",
      "https://backend.example/paper/positions",
      "https://backend.example/journal/summary",
    ]);
    for (const [, init] of fetchMock.mock.calls) {
      expect(init).toMatchObject({
        method: "GET",
        credentials: "omit",
        cache: "no-store",
      });
      expect(new Headers((init as RequestInit).headers).has("X-API-Key")).toBe(false);
      expect((init as RequestInit).body).toBeUndefined();
    }
  });

  it("rejects unsafe base URLs and invalid response contracts", async () => {
    expect(() => normalizeApiBaseUrl("https://user:secret@example.test")).toThrow(
      BackendApiError,
    );
    expect(normalizeApiBaseUrl("  ")).toBeNull();

    const fetchMock = successfulFetch();
    fetchMock.mockImplementationOnce(async () =>
      new Response(JSON.stringify({ status: "maybe" }), { status: 200 }),
    );
    await expect(
      fetchBackendSnapshot({
        baseUrl: "https://backend.example",
        fetchImpl: fetchMock as typeof fetch,
      }),
    ).rejects.toMatchObject({ code: "INVALID_RESPONSE", route: "/health" });
  });

  it("aborts the batch when its timeout elapses", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(
      (_input: RequestInfo | URL, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener(
            "abort",
            () => reject(new DOMException("aborted", "AbortError")),
            { once: true },
          );
        }),
    );
    const request = fetchBackendSnapshot({
      baseUrl: "https://backend.example",
      fetchImpl: fetchMock as typeof fetch,
      timeoutMs: 50,
    });
    const rejection = expect(request).rejects.toMatchObject({ code: "TIMEOUT" });
    await vi.advanceTimersByTimeAsync(51);
    await rejection;
    expect(fetchMock).toHaveBeenCalledTimes(5);
    vi.useRealTimers();
  });

  it("validates and loads every market read route with encoded GET-only queries", async () => {
    const marketResponses: Readonly<Record<string, unknown>> = {
      "/markets": {
        generated_at: "2026-08-25T10:00:02+00:00",
        markets: [
          {
            market_id: "NIFTY",
            symbols: [
              { symbol: "NIFTY", expiries: [SELECTION.expiry], latest: READ_MODEL },
            ],
          },
        ],
      },
      "/contracts": {
        read_model: READ_MODEL,
        selection: SELECTION,
        contract: CONTRACT,
      },
      "/market/workspace": WORKSPACE,
      "/market/chain": {
        read_model: READ_MODEL,
        selection: SELECTION,
        chain: CHAIN,
      },
      "/market/analytics": {
        read_model: READ_MODEL,
        selection: SELECTION,
        analytics: ANALYTICS,
      },
      "/market/updates": {
        after: 6,
        revision: 7,
        changed: true,
        reset_required: false,
        event: {
          revision: 7,
          event_type: "WORKSPACE",
          occurred_at: "2026-08-25T10:00:01+00:00",
          market_id: "NIFTY",
          symbol: "NIFTY",
          expiry: SELECTION.expiry,
          snapshot_id: "snapshot-7",
          security_id: null,
        },
      },
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
      const url = new URL(String(input));
      return new Response(JSON.stringify(marketResponses[url.pathname]), { status: 200 });
    });
    const options = {
      baseUrl: "https://backend.example",
      fetchImpl: fetchMock as typeof fetch,
      selection: SELECTION,
    };

    const [catalog, contract, workspace, chain, analytics, update] = await Promise.all([
      fetchMarketCatalog(options),
      fetchMarketContract(options),
      fetchMarketWorkspace(options),
      fetchMarketChain(options),
      fetchMarketAnalytics(options),
      waitForMarketUpdates({
        baseUrl: options.baseUrl,
        fetchImpl: options.fetchImpl,
        after: 6,
        timeoutSeconds: 0,
      }),
    ]);

    expect(catalog.markets[0]?.symbols[0]?.latest.data_mode).toBe("LIVE");
    expect(contract.contract.lot_size).toBe(75);
    expect(workspace.selection).toEqual(SELECTION);
    expect(chain.chain.leg_count).toBe(2);
    expect(analytics.analytics.trend.direction).toBe("BULLISH");
    expect(update.event?.snapshot_id).toBe("snapshot-7");
    const urls = fetchMock.mock.calls.map(([input]) => new URL(String(input)));
    for (const url of urls.filter((item) => item.pathname !== "/markets")) {
      if (url.pathname === "/market/updates") continue;
      expect(url.searchParams.get("market")).toBe("NIFTY");
      expect(url.searchParams.get("symbol")).toBe("NIFTY");
      expect(url.searchParams.get("expiry")).toBe(SELECTION.expiry);
    }
    for (const [, init] of fetchMock.mock.calls) {
      expect(init).toMatchObject({ method: "GET", credentials: "omit", cache: "no-store" });
      expect(new Headers((init as RequestInit).headers).has("X-API-Key")).toBe(false);
      expect((init as RequestInit).body).toBeUndefined();
    }
  });

  it("rejects incoherent chain counts and unsafe update bounds", async () => {
    const invalidWorkspace = structuredClone(WORKSPACE) as Record<string, unknown>;
    (invalidWorkspace.chain as Record<string, unknown>).leg_count = 99;
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify(invalidWorkspace), { status: 200 }),
    );
    await expect(
      fetchMarketWorkspace({
        baseUrl: "https://backend.example",
        fetchImpl: fetchMock as typeof fetch,
        selection: SELECTION,
      }),
    ).rejects.toMatchObject({ code: "INVALID_RESPONSE", route: "/market/workspace" });

    expect(() =>
      waitForMarketUpdates({
        baseUrl: "https://backend.example",
        after: -1,
      }),
    ).toThrow(RangeError);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
