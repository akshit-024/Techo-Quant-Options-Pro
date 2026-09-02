import { useCallback, useEffect, useMemo, useReducer, useState } from "react";

import {
  BackendApiError,
  configuredApiBaseUrl,
  DEFAULT_MARKET_UPDATE_TIMEOUT_SECONDS,
  DEFAULT_REQUEST_TIMEOUT_MS,
  fetchBackendStatus,
  fetchMarketCatalog,
  fetchMarketWorkspace,
  normalizeApiBaseUrl,
  waitForMarketUpdates,
} from "../api/client";
import type {
  MarketCatalogResponse,
  MarketSelection,
  MarketWorkspaceResponse,
} from "../api/contracts";

export type LiveMarketConnection =
  | "DISABLED"
  | "NOT_CONFIGURED"
  | "LOADING"
  | "CONFIG_REQUIRED"
  | "EMPTY"
  | "LIVE"
  | "STALE"
  | "ERROR";

export interface LiveMarketDataState {
  connection: LiveMarketConnection;
  catalog: MarketCatalogResponse | null;
  workspace: MarketWorkspaceResponse | null;
  isLoading: boolean;
  stale: boolean;
  error: string | null;
  lastSuccessAt: string | null;
  revision: number;
  refresh: () => void;
}

export interface UseLiveMarketDataOptions {
  /** Override VITE_API_BASE_URL; null explicitly disables backend discovery. */
  baseUrl?: string | null;
  /** Explicit demo mode. When false, this hook performs no network requests. */
  enabled?: boolean;
  requestTimeoutMs?: number;
  updateTimeoutSeconds?: number;
  retryDelayMs?: number;
  fetchImpl?: typeof fetch;
}

interface InternalLiveMarketState extends Omit<LiveMarketDataState, "refresh"> {}

const DEFAULT_RETRY_DELAY_MS = 2_000;

/**
 * Loads the market catalog and one coherent workspace, then refreshes it through
 * bounded, sequential long polls. The caller owns selection so navigation stays
 * deterministic; pass null while choosing from the returned catalog.
 */
export function useLiveMarketData(
  selection: MarketSelection | null,
  options: UseLiveMarketDataOptions = {},
): LiveMarketDataState {
  const {
    baseUrl: requestedBaseUrl,
    enabled = true,
    requestTimeoutMs = DEFAULT_REQUEST_TIMEOUT_MS,
    updateTimeoutSeconds = DEFAULT_MARKET_UPDATE_TIMEOUT_SECONDS,
    retryDelayMs = DEFAULT_RETRY_DELAY_MS,
    fetchImpl,
  } = options;
  validatePositiveDuration(requestTimeoutMs, "requestTimeoutMs");
  validatePositiveDuration(retryDelayMs, "retryDelayMs");
  if (
    !Number.isFinite(updateTimeoutSeconds) ||
    updateTimeoutSeconds < 0 ||
    updateTimeoutSeconds > 30
  ) {
    throw new RangeError("updateTimeoutSeconds must be between 0 and 30");
  }

  const resolution = useMemo(
    () => resolveBaseUrl(requestedBaseUrl),
    [requestedBaseUrl],
  );
  const [refreshSequence, refresh] = useReducer((value: number) => value + 1, 0);
  const [state, setState] = useState<InternalLiveMarketState>(() =>
    initialState(enabled, resolution),
  );
  const marketId = selection?.market_id ?? null;
  const symbol = selection?.symbol ?? null;
  const expiry = selection?.expiry ?? null;

  useEffect(() => {
    const controller = new AbortController();
    if (!enabled || resolution.baseUrl === null || resolution.error !== null) {
      setState(initialState(enabled, resolution));
      return () => controller.abort();
    }

    const selected =
      marketId === null || symbol === null || expiry === null
        ? null
        : { market_id: marketId, symbol, expiry };
    setState((current) => {
      const keepWorkspace =
        selected !== null && selectionsEqual(current.workspace?.selection ?? null, selected);
      return {
        ...current,
        connection: "LOADING",
        workspace: keepWorkspace ? current.workspace : null,
        stale: keepWorkspace,
        isLoading: true,
        error: null,
      };
    });

    void runMarketLoop({
      baseUrl: resolution.baseUrl,
      controller,
      selection: selected,
      requestTimeoutMs,
      updateTimeoutSeconds,
      retryDelayMs,
      fetchImpl,
      setState,
    });

    return () => controller.abort();
  }, [
    enabled,
    expiry,
    fetchImpl,
    marketId,
    refreshSequence,
    requestTimeoutMs,
    resolution,
    retryDelayMs,
    symbol,
    updateTimeoutSeconds,
  ]);

  const requestRefresh = useCallback(() => refresh(), []);
  return { ...state, refresh: requestRefresh };
}

interface MarketLoopArguments {
  baseUrl: string;
  controller: AbortController;
  selection: MarketSelection | null;
  requestTimeoutMs: number;
  updateTimeoutSeconds: number;
  retryDelayMs: number;
  fetchImpl?: typeof fetch;
  setState: React.Dispatch<React.SetStateAction<InternalLiveMarketState>>;
}

async function runMarketLoop({
  baseUrl,
  controller,
  selection,
  requestTimeoutMs,
  updateTimeoutSeconds,
  retryDelayMs,
  fetchImpl,
  setState,
}: MarketLoopArguments): Promise<void> {
  const request = { baseUrl, signal: controller.signal, requestTimeoutMs, fetchImpl };
  let revision = 0;
  try {
    // Status is intentionally fetched first: missing Dhan credentials should not be
    // misreported as an empty market catalog.
    const status = await fetchBackendStatus({
      baseUrl,
      signal: controller.signal,
      timeoutMs: requestTimeoutMs,
      fetchImpl,
    });
    if (controller.signal.aborted) return;
    revision = status.market_data?.revision ?? 0;
    if (status.market_data?.feed.state === "CONFIG_REQUIRED") {
      setState((current) => ({
        ...current,
        connection: "CONFIG_REQUIRED",
        isLoading: false,
        stale: current.workspace !== null,
        error: "Dhan credentials are required before live market data can start.",
        revision,
      }));
      return;
    }

    const catalog = await fetchMarketCatalog({
      baseUrl,
      signal: controller.signal,
      timeoutMs: requestTimeoutMs,
      fetchImpl,
    });
    if (controller.signal.aborted) return;
    if (catalog.markets.length === 0) {
      setState((current) => ({
        ...current,
        connection: "EMPTY",
        catalog,
        isLoading: false,
        stale: current.workspace !== null,
        error: null,
        revision,
      }));
      return;
    }
    // Publish choices before resolving the workspace. This lets the caller repair an
    // expired startup selection even when the selected lookup would return 404.
    setState((current) => ({ ...current, catalog, revision }));
    if (selection === null) {
      setState((current) => ({
        ...current,
        connection: "LIVE",
        catalog,
        workspace: null,
        isLoading: false,
        stale: false,
        error: null,
        revision,
      }));
      return;
    }
    if (!catalogContainsSelection(catalog, selection)) {
      setState((current) => ({
        ...current,
        connection: "ERROR",
        catalog,
        workspace: null,
        isLoading: false,
        stale: false,
        error: "The selected market contract is not available in the current catalog.",
        revision,
      }));
      return;
    }

    let workspace = await loadWorkspace({ ...request, selection });
    if (controller.signal.aborted) return;
    publishSuccess(setState, catalog, workspace, revision);

    while (!controller.signal.aborted) {
      try {
        const update = await waitForMarketUpdates({
          baseUrl,
          signal: controller.signal,
          fetchImpl,
          after: revision,
          timeoutSeconds: updateTimeoutSeconds,
          timeoutMs: (updateTimeoutSeconds + 5) * 1_000,
        });
        if (controller.signal.aborted) return;
        revision = update.revision;
        const relevantWorkspace =
          update.reset_required ||
          !update.changed ||
          (update.event?.event_type === "WORKSPACE" &&
            eventMatchesSelection(update.event, selection));
        if (relevantWorkspace) {
          workspace = await loadWorkspace({ ...request, selection });
          if (controller.signal.aborted) return;
          publishSuccess(setState, catalog, workspace, revision);
        } else {
          setState((current) => ({ ...current, revision }));
          await abortableDelay(Math.min(retryDelayMs, 1_000), controller.signal);
        }
      } catch (error) {
        if (controller.signal.aborted || isCancelled(error)) return;
        publishFailure(setState, error);
        await abortableDelay(retryDelayMs, controller.signal);
      }
    }
  } catch (error) {
    if (controller.signal.aborted || isCancelled(error)) return;
    publishFailure(setState, error, revision);
  }
}

function loadWorkspace({
  baseUrl,
  signal,
  requestTimeoutMs,
  fetchImpl,
  selection,
}: {
  baseUrl: string;
  signal: AbortSignal;
  requestTimeoutMs: number;
  fetchImpl?: typeof fetch;
  selection: MarketSelection;
}): Promise<MarketWorkspaceResponse> {
  return fetchMarketWorkspace({
    baseUrl,
    signal,
    timeoutMs: requestTimeoutMs,
    fetchImpl,
    selection,
  });
}

function publishSuccess(
  setState: MarketLoopArguments["setState"],
  catalog: MarketCatalogResponse,
  workspace: MarketWorkspaceResponse,
  revision: number,
): void {
  const live = workspace.read_model.data_mode === "LIVE" && workspace.read_model.fresh;
  setState({
    connection: live ? "LIVE" : "STALE",
    catalog,
    workspace,
    isLoading: false,
    stale: !live,
    error: null,
    lastSuccessAt: new Date().toISOString(),
    revision,
  });
}

function publishFailure(
  setState: MarketLoopArguments["setState"],
  error: unknown,
  revision?: number,
): void {
  setState((current) => ({
    ...current,
    connection: current.workspace === null ? "ERROR" : "STALE",
    isLoading: false,
    stale: current.workspace !== null,
    error: marketErrorMessage(error),
    revision: revision ?? current.revision,
  }));
}

function initialState(
  enabled: boolean,
  resolution: BaseResolution,
): InternalLiveMarketState {
  const connection: LiveMarketConnection = !enabled
    ? "DISABLED"
    : !resolution.configured
      ? "NOT_CONFIGURED"
      : resolution.error === null
        ? "LOADING"
        : "ERROR";
  return {
    connection,
    catalog: null,
    workspace: null,
    isLoading: connection === "LOADING",
    stale: false,
    error: resolution.error,
    lastSuccessAt: null,
    revision: 0,
  };
}

interface BaseResolution {
  baseUrl: string | null;
  configured: boolean;
  error: string | null;
}

function resolveBaseUrl(requested: string | null | undefined): BaseResolution {
  try {
    const value =
      requested === undefined ? configuredApiBaseUrl() : normalizeApiBaseUrl(requested);
    return { baseUrl: value, configured: value !== null, error: null };
  } catch (error) {
    return {
      baseUrl: null,
      configured: true,
      error: error instanceof Error ? error.message : "API base URL is invalid",
    };
  }
}

function selectionsEqual(
  left: MarketSelection | null,
  right: MarketSelection | null,
): boolean {
  return (
    left !== null &&
    right !== null &&
    left.market_id === right.market_id &&
    left.symbol === right.symbol &&
    left.expiry === right.expiry
  );
}

function eventMatchesSelection(
  event: { market_id: string | null; symbol: string | null; expiry: string | null },
  selection: MarketSelection,
): boolean {
  return (
    event.market_id === selection.market_id &&
    event.symbol === selection.symbol &&
    event.expiry === selection.expiry
  );
}

function catalogContainsSelection(
  catalog: MarketCatalogResponse,
  selection: MarketSelection,
): boolean {
  return catalog.markets.some(
    (market) =>
      market.market_id === selection.market_id &&
      market.symbols.some(
        (candidate) =>
          candidate.symbol === selection.symbol &&
          candidate.expiries.includes(selection.expiry),
      ),
  );
}

function marketErrorMessage(error: unknown): string {
  return error instanceof BackendApiError ? error.message : "Live market request failed";
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
