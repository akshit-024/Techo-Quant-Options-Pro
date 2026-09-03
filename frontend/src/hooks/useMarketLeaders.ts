import { useCallback, useEffect, useMemo, useReducer, useState } from "react";

import {
  BackendApiError,
  configuredApiBaseUrl,
  DEFAULT_REQUEST_TIMEOUT_MS,
  fetchMarketLeaders,
  normalizeApiBaseUrl,
} from "../api/client";
import type { MarketLeadersResponse } from "../api/contracts";
import type { MarketId } from "../domain/types";

export type MarketLeadersConnection =
  | "DISABLED"
  | "NOT_CONFIGURED"
  | "LOADING"
  | "LIVE"
  | "STALE"
  | "UNAVAILABLE"
  | "ERROR";

export interface MarketLeadersState {
  connection: MarketLeadersConnection;
  response: MarketLeadersResponse | null;
  isLoading: boolean;
  error: string | null;
  lastSuccessAt: string | null;
  refresh: () => void;
}

export interface UseMarketLeadersOptions {
  baseUrl?: string | null;
  enabled?: boolean;
  requestTimeoutMs?: number;
  pollIntervalMs?: number;
  fetchImpl?: typeof fetch;
}

interface InternalMarketLeadersState
  extends Omit<MarketLeadersState, "refresh"> {
  requestedMarket: MarketId | null;
}

const DEFAULT_POLL_INTERVAL_MS = 5_000;

/**
 * Polls the backend's cached market-leader read model. This endpoint does not
 * make one provider request per row; Dhan acquisition remains batched in the
 * backend. Each market change owns a new AbortController so an old response can
 * never overwrite the newly selected bracket.
 */
export function useMarketLeaders(
  market: MarketId,
  options: UseMarketLeadersOptions = {},
): MarketLeadersState {
  const {
    baseUrl: requestedBaseUrl,
    enabled = true,
    requestTimeoutMs = DEFAULT_REQUEST_TIMEOUT_MS,
    pollIntervalMs = DEFAULT_POLL_INTERVAL_MS,
    fetchImpl,
  } = options;
  validatePositiveDuration(requestTimeoutMs, "requestTimeoutMs");
  validatePositiveDuration(pollIntervalMs, "pollIntervalMs");

  const resolution = useMemo(
    () => resolveBaseUrl(requestedBaseUrl),
    [requestedBaseUrl],
  );
  const [refreshSequence, refresh] = useReducer((value: number) => value + 1, 0);
  const [state, setState] = useState<InternalMarketLeadersState>(() =>
    initialState(enabled, resolution),
  );

  useEffect(() => {
    const controller = new AbortController();
    if (!enabled || resolution.baseUrl === null || resolution.error !== null) {
      setState(initialState(enabled, resolution));
      return () => controller.abort();
    }

    setState((current) => ({
      ...current,
      requestedMarket: market,
      response:
        current.response?.market_id === market ? current.response : null,
      connection: "LOADING",
      isLoading: true,
      error: null,
    }));

    void runLeadersLoop({
      baseUrl: resolution.baseUrl,
      controller,
      market,
      pollIntervalMs,
      requestTimeoutMs,
      fetchImpl,
      setState,
    });

    return () => controller.abort();
  }, [
    enabled,
    fetchImpl,
    market,
    pollIntervalMs,
    refreshSequence,
    requestTimeoutMs,
    resolution,
  ]);

  const requestRefresh = useCallback(() => refresh(), []);
  const response =
    state.requestedMarket === market && state.response?.market_id === market
      ? state.response
      : null;
  const changingMarket = enabled && state.requestedMarket !== market;

  return {
    connection: changingMarket ? "LOADING" : state.connection,
    response,
    isLoading: changingMarket || state.isLoading,
    error: changingMarket ? null : state.error,
    lastSuccessAt: changingMarket ? null : state.lastSuccessAt,
    refresh: requestRefresh,
  };
}

interface LeadersLoopArguments {
  baseUrl: string;
  controller: AbortController;
  market: MarketId;
  pollIntervalMs: number;
  requestTimeoutMs: number;
  fetchImpl?: typeof fetch;
  setState: React.Dispatch<React.SetStateAction<InternalMarketLeadersState>>;
}

async function runLeadersLoop({
  baseUrl,
  controller,
  market,
  pollIntervalMs,
  requestTimeoutMs,
  fetchImpl,
  setState,
}: LeadersLoopArguments): Promise<void> {
  while (!controller.signal.aborted) {
    try {
      const response = await fetchMarketLeaders({
        baseUrl,
        marketId: market,
        signal: controller.signal,
        timeoutMs: requestTimeoutMs,
        fetchImpl,
      });
      if (controller.signal.aborted) return;
      if (response.market_id !== market) {
        throw new BackendApiError(
          "INVALID_RESPONSE",
          "The backend returned leaders for a different market bracket.",
          { route: "/market/leaders" },
        );
      }

      setState({
        requestedMarket: market,
        connection: response.market_state,
        response,
        isLoading: false,
        error: null,
        lastSuccessAt: new Date().toISOString(),
      });
    } catch (error) {
      if (controller.signal.aborted || isCancelled(error)) return;
      setState((current) => {
        const retained =
          current.requestedMarket === market &&
          current.response?.market_id === market &&
          current.response.leaders.length > 0
            ? current.response
            : null;
        return {
          ...current,
          requestedMarket: market,
          connection: retained === null ? "ERROR" : "STALE",
          response: retained,
          isLoading: false,
          error: marketErrorMessage(error),
        };
      });
    }

    await abortableDelay(pollIntervalMs, controller.signal);
  }
}

interface BaseResolution {
  baseUrl: string | null;
  configured: boolean;
  error: string | null;
}

function resolveBaseUrl(requested: string | null | undefined): BaseResolution {
  try {
    const value =
      requested === undefined
        ? configuredApiBaseUrl()
        : normalizeApiBaseUrl(requested);
    return { baseUrl: value, configured: value !== null, error: null };
  } catch (error) {
    return {
      baseUrl: null,
      configured: true,
      error: error instanceof Error ? error.message : "API base URL is invalid",
    };
  }
}

function initialState(
  enabled: boolean,
  resolution: BaseResolution,
): InternalMarketLeadersState {
  const connection: MarketLeadersConnection = !enabled
    ? "DISABLED"
    : !resolution.configured
      ? "NOT_CONFIGURED"
      : resolution.error === null
        ? "LOADING"
        : "ERROR";
  return {
    requestedMarket: null,
    connection,
    response: null,
    isLoading: connection === "LOADING",
    error: resolution.error,
    lastSuccessAt: null,
  };
}

function marketErrorMessage(error: unknown): string {
  return error instanceof BackendApiError
    ? error.message
    : "Live market-leader request failed";
}

function isCancelled(error: unknown): boolean {
  return error instanceof BackendApiError && error.code === "ABORTED";
}

function abortableDelay(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) {
      resolve();
      return;
    }
    const timeout = globalThis.setTimeout(finish, milliseconds);
    signal.addEventListener("abort", finish, { once: true });
    function finish(): void {
      globalThis.clearTimeout(timeout);
      signal.removeEventListener("abort", finish);
      resolve();
    }
  });
}

function validatePositiveDuration(value: number, name: string): void {
  if (!Number.isFinite(value) || value <= 0) {
    throw new RangeError(`${name} must be finite and positive`);
  }
}
