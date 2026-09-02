import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { BackendReadSnapshot } from "../../api/contracts";
import { buildDemoSnapshot } from "../../data/demoSnapshot";
import { MARKET_DEFINITIONS } from "../../data/marketDefinitions";
import type { BackendStatusState } from "../../hooks/useBackendStatus";
import { ApiStatusView } from "./ApiStatusView";

const SNAPSHOT = buildDemoSnapshot({
  market: "NIFTY",
  symbol: MARKET_DEFINITIONS.NIFTY.symbols[0],
  expiry: MARKET_DEFINITIONS.NIFTY.expiries[0],
});

const PAYLOAD: BackendReadSnapshot = {
  fetchedAt: "2026-08-21T06:15:00.000Z",
  health: { status: "ok", mode: "DATA_ONLY", live_locked: true },
  status: {
    mode: "DATA_ONLY",
    live_enabled: false,
    live_gateway_configured: false,
    kill_switch: {
      active: false,
      reason: null,
      actor: null,
      changed_at: "2026-08-21T06:00:00+00:00",
    },
    counts: { signals: 0, approvals: 0, orders: 0, fills: 0, positions: 0 },
  },
  latestSignal: { signal: null },
  paperPositions: { positions: [] },
  journalSummary: {
    journal_entries: 3,
    orders: 0,
    closed_positions: 0,
    realized_pnl: "0",
  },
};

function hookState(overrides: Partial<BackendStatusState>): BackendStatusState {
  return {
    connection: "NOT_CONFIGURED",
    payload: null,
    lastAttemptAt: null,
    lastSuccessAt: null,
    error: null,
    isLoading: false,
    refresh: vi.fn(),
    ...overrides,
  };
}

describe("ApiStatusView", () => {
  it("explains unconfigured and demo states without offering a network action", () => {
    render(
      <ApiStatusView
        now={new Date("2026-08-21T06:20:00.000Z")}
        snapshot={{ ...SNAPSHOT, chain: SNAPSHOT.chain.slice(0, 4) }}
        state={hookState({})}
      />,
    );

    expect(
      screen.getByRole("heading", { name: "API and data status" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Not configured");
    expect(screen.getByText(/No network request has been made/)).toBeInTheDocument();
    expect(screen.getByText(/deterministic demo data/)).toBeInTheDocument();
    expect(screen.getByText("ATM+2")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /refresh/i })).not.toBeInTheDocument();
  });

  it("shows stale retained data, execution security, and the fetch error", () => {
    render(
      <ApiStatusView
        now={new Date("2026-08-21T06:20:00.000Z")}
        snapshot={SNAPSHOT}
        state={hookState({
          connection: "STALE",
          payload: PAYLOAD,
          lastAttemptAt: "2026-08-21T06:19:55.000Z",
          lastSuccessAt: PAYLOAD.fetchedAt,
          error: "GET /status returned HTTP 503",
        })}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent("Stale");
    expect(screen.getByText("Live locked · kill switch clear")).toBeInTheDocument();
    expect(screen.getByText("3 entries · 0 orders")).toBeInTheDocument();
    expect(screen.getByText("None")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "GET /status returned HTTP 503",
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "last successful read-only payload",
    );
  });
});
