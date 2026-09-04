import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";

const hookState = vi.hoisted(() => ({
  backend: {} as Record<string, unknown>,
  live: {} as Record<string, unknown>,
}));

vi.mock("./hooks/useBackendStatus", () => ({
  useBackendStatus: () => hookState.backend,
}));

vi.mock("./hooks/useLiveMarketData", () => ({
  useLiveMarketData: () => hookState.live,
}));

vi.mock("./data/backendSnapshot", () => {
  class BackendSnapshotAdapterError extends Error {}

  return {
    BackendSnapshotAdapterError,
    adaptBackendSnapshot: (workspace: unknown) => workspace,
  };
});

import App from "./App";
import { buildDemoSnapshot } from "./data/demoSnapshot";
import type { MarketSnapshot } from "./domain/types";

describe("live application wiring", () => {
  it("uses a backend-owned snapshot and switches to demo only by explicit choice", async () => {
    const user = userEvent.setup();
    const expiry = "2099-09-05T15:30:00+05:30";

    const demo = buildDemoSnapshot({
      market: "NIFTY",
      symbol: "NIFTY",
      expiry,
    });

    const liveSnapshot: MarketSnapshot = {
      ...demo,
      capturedAt: new Date().toISOString(),
      dataMode: "LIVE",
      inputs: demo.inputs.map((input) => ({
        ...input,
        source: input.source === "COMPUTED" ? "COMPUTED" : "LIVE FEED",
      })),
      analytics: {
        ...demo.analytics,
        decision: "NO TRADE",
        decisionReason: "The operator profile is required.",
      },
      backendAuthority: {
        snapshotId: "snapshot-live-1",
        contractKey: "contract-live-1",
        source: "DHAN_REST",
        receivedAt: new Date().toISOString(),
        complete: true,
        fresh: true,
        actionable: false,
        blockers: ["OPERATOR_PROFILE_REQUIRED"],
        warnings: [],
        validationAccepted: true,
      },
    };

    hookState.backend = {
      connection: "CONNECTED",
      payload: null,
      lastAttemptAt: liveSnapshot.capturedAt,
      lastSuccessAt: liveSnapshot.capturedAt,
      error: null,
      isLoading: false,
      refresh: vi.fn(),
    };

    hookState.live = {
      connection: "LIVE",
      catalog: {
        generated_at: liveSnapshot.capturedAt,
        markets: [
          {
            market_id: "NIFTY",
            symbols: [{ symbol: "NIFTY", expiries: [expiry] }],
          },
        ],
      },
      workspace: liveSnapshot,
      isLoading: false,
      stale: false,
      error: null,
      lastSuccessAt: liveSnapshot.capturedAt,
      revision: 1,
      refresh: vi.fn(),
    };

    render(<App />);

    const source = screen.getByRole("group", { name: "Data source" });

    expect(
      within(source).getByRole("button", { name: "Live" }),
    ).toHaveAttribute("aria-pressed", "true");

    expect(screen.getByText("live snapshot")).toBeInTheDocument();
    expect(screen.queryByLabelText("Live market data status")).not.toBeInTheDocument();
    expect(screen.getByText("OPERATOR_PROFILE_REQUIRED")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Inspect ranking/ }));

    const rankingHeading = screen.getByRole("heading", {
      name: "NIFTY strike ranking",
    });
    const rankingSection = rankingHeading.closest("section");

    if (!(rankingSection instanceof HTMLElement)) {
      throw new Error("live ranking section missing");
    }

    expect(within(rankingSection).getByText("LIVE snapshot")).toBeInTheDocument();
    expect(within(rankingSection).getByText("10 option legs")).toBeInTheDocument();
    expect(rankingSection.querySelectorAll("tbody tr")).toHaveLength(10);
    expect(within(rankingSection).getByText("BEST")).toBeInTheDocument();
    expect(within(rankingSection).getByText("SECOND")).toBeInTheDocument();

    await user.click(
      within(
        screen.getByRole("complementary", { name: "Primary navigation" }),
      ).getByRole("button", { name: "Dashboard" }),
    );

    await user.click(within(source).getByRole("button", { name: "Demo" }));

    expect(
      within(source).getByRole("button", { name: "Demo" }),
    ).toHaveAttribute("aria-pressed", "true");

    expect(screen.getByText("demo snapshot")).toBeInTheDocument();
    expect(screen.getByText("DEMO DATA")).toBeInTheDocument();
  });

  it("replaces an expired expiry only within the selected market and symbol", async () => {
    const currentExpiry = "2099-09-12T15:30:00+05:30";
    hookState.backend = {
      connection: "CONNECTED",
      payload: null,
      lastAttemptAt: null,
      lastSuccessAt: null,
      error: null,
      isLoading: false,
      refresh: vi.fn(),
    };
    hookState.live = {
      connection: "LOADING",
      catalog: {
        generated_at: new Date().toISOString(),
        markets: [
          {
            market_id: "NIFTY",
            symbols: [
              {
                symbol: "NIFTY",
                expiries: [currentExpiry],
                latest: {},
              },
            ],
          },
          {
            market_id: "MCX",
            symbols: [
              {
                symbol: "CRUDEOIL",
                expiries: ["2099-09-15T23:30:00+05:30"],
                latest: {},
              },
            ],
          },
        ],
      },
      workspace: null,
      isLoading: true,
      stale: false,
      error: null,
      lastSuccessAt: null,
      revision: 2,
      refresh: vi.fn(),
    };

    render(<App />);

    expect(await screen.findByLabelText("Market")).toHaveValue("NIFTY");
    expect(screen.getByLabelText("Symbol")).toHaveValue("NIFTY");
    const expiry = screen.getByLabelText("Option expiry");
    await waitFor(() => expect(expiry).toHaveValue(currentExpiry));
    expect(
      within(screen.getByLabelText("Symbol")).queryByRole("option", {
        name: "CRUDEOIL",
      }),
    ).not.toBeInTheDocument();
    expect(
      within(expiry)
        .getAllByRole("option")
        .map((option) => (option as HTMLOptionElement).value),
    ).toEqual([currentExpiry]);
  });
});
