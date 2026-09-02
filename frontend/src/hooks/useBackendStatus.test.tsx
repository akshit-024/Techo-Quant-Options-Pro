import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { BACKEND_STATUS_POLL_MS, useBackendStatus } from "./useBackendStatus";

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
    counts: { signals: 0, approvals: 0, orders: 0, fills: 0, positions: 0 },
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

function successfulFetch() {
  return vi.fn(async (input: RequestInfo | URL) => {
    const path = new URL(String(input)).pathname;
    return new Response(JSON.stringify(RESPONSES[path]), { status: 200 });
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("useBackendStatus", () => {
  it("uses the required ten-second poll interval", () => {
    expect(BACKEND_STATUS_POLL_MS).toBe(10_000);
  });

  it("makes no request when the API base URL is absent", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useBackendStatus({ baseUrl: null }));

    expect(result.current.connection).toBe("NOT_CONFIGURED");
    expect(result.current.payload).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("connects through GET polling and keeps the last success after an error", async () => {
    const fetchMock = successfulFetch();
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() =>
      useBackendStatus({
        baseUrl: "https://backend.example",
        pollIntervalMs: 60_000,
        staleAfterMs: 60_000,
      }),
    );

    await waitFor(() => expect(result.current.connection).toBe("CONNECTED"));
    const lastPayload = result.current.payload;
    expect(lastPayload).not.toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(5);

    fetchMock.mockRejectedValue(new TypeError("offline"));
    act(() => result.current.refresh());
    await waitFor(() => expect(result.current.connection).toBe("STALE"));

    expect(result.current.payload).toBe(lastPayload);
    expect(result.current.error).toMatch(/failed/i);
    expect(result.current.lastSuccessAt).toBe(lastPayload?.fetchedAt);
  });
});
