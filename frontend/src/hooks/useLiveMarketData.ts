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
        connection: keepWorkspace ? current.connection : "LOADING",
        workspace: keepWorkspace ? current.workspace : null,
        stale: keepWorkspace ? current.stale : false,
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
  const currentSelection =
    marketId === null || symbol === null || expiry === null
      ? null
      : { market_id: marketId, symbol, expiry };
  const visibleWorkspace = selectionsEqual(
    state.workspace?.selection ?? null,
    currentSelection,
  )
    ? state.workspace
    : null;
  const hidingObsoleteWorkspace =
    state.workspace !== null && visibleWorkspace === null;

  return {
    ...state,
    connection: hidingObsoleteWorkspace ? "LOADING" : state.connection,
    workspace: visibleWorkspace,
    isLoading: hidingObsoleteWorkspace || state.isLoading,
    stale: hidingObsoleteWorkspace ? false : state.stale,
    error: hidingObsoleteWorkspace ? null : state.error,
    refresh: requestRefresh,
  };
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
  while (!controller.signal.aborted) {
    try {
      // Refresh status and catalog on every bounded cycle. A backend can begin with
      // no published workspaces and recover later, and expiries can roll while the
      // browser remains open. Neither condition is terminal.
      const status = await fetchBackendStatus({
        baseUrl,
        signal: controller.signal,
        timeoutMs: requestTimeoutMs,
        fetchImpl,
      });
      if (controller.signal.aborted) return;
      revision = Math.max(revision, status.market_data?.revision ?? 0);

      if (status.market_data?.feed.state === "CONFIG_REQUIRED") {
        setState((current) => ({
          ...current,
          connection: "CONFIG_REQUIRED",
          isLoading: false,
          stale: current.workspace !== null,
          error: "Dhan credentials are required before live market data can start.",
          revision,
        }));
        await abortableDelay(retryDelayMs, controller.signal);
        continue;
      }

      const catalog = await fetchMarketCatalog({
        baseUrl,
        signal: controller.signal,
        timeoutMs: requestTimeoutMs,
        fetchImpl,
      });
      if (controller.signal.aborted) return;

      // Publish discovery independently of workspace resolution so App can adopt a
      // newly rolled expiry. A partial catalog must never terminate the refresh loop.
      setState((current) => ({ ...current, catalog, revision }));

      if (selection === null) {
        if (catalog.markets.length === 0) {
          setState((current) => ({
            ...current,
            connection: "EMPTY",
            catalog,
            workspace: null,
            isLoading: false,
            stale: false,
            error: "The backend has not published any validated market workspace yet.",
            revision,
          }));
          await abortableDelay(retryDelayMs, controller.signal);
          continue;
        }
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
      } else {
        // Always ask for the selected workspace, even before it appears in /markets.
        // The backend uses this read request as an idempotent acquisition-priority hint.
        const workspace = await loadWorkspace({ ...request, selection });
        if (controller.signal.aborted) return;
        if (!selectionsEqual(workspace.selection, selection)) {
          throw new BackendApiError(
            "INVALID_RESPONSE",
            "The backend returned a workspace for a different market selection.",
            { route: "/market/workspace" },
          );
        }
        publishSuccess(setState, catalog, workspace, revision);
      }

      // A bounded long poll avoids aggressive REST polling. On an event or timeout,
      // loop through status/catalog/workspace again so freshness and rollovers are
      // re-evaluated even when no selected-workspace event was emitted.
      const update = await waitForMarketUpdates({
        baseUrl,
        signal: controller.signal,
        fetchImpl,
        after: revision,
        timeoutSeconds: updateTimeoutSeconds,
        timeoutMs: (updateTimeoutSeconds + 5) * 1_000,
      });
      if (controller.signal.aborted) return;
      revision = Math.max(revision, update.revision);
      setState((current) => ({ ...current, revision }));
      // Feed ticks can advance the global revision continuously. Bound local
      // workspace reads so they cannot spin while still guaranteeing that a
      // coalesced WORKSPACE publication is observed promptly.
      await abortableDelay(Math.min(retryDelayMs, 1_000), controller.signal);
    } catch (error) {
      if (controller.signal.aborted || isCancelled(error)) return;
      if (selection !== null && isSelectionPending(error)) {
        setState((current) => ({
          ...current,
          connection: current.workspace === null ? "LOADING" : "STALE",
          isLoading: current.workspace === null,
          stale: current.workspace !== null,
          error: `${selection.symbol} ${selection.expiry.slice(0, 10)} is being acquired from the live backend.`,
          revision,
        }));
      } else {
        publishFailure(setState, error, revision);
      }
      await abortableDelay(retryDelayMs, controller.signal);
    }
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

function isSelectionPending(error: unknown): boolean {
  return (
    error instanceof BackendApiError &&
    error.code === "HTTP_ERROR" &&
    error.status === 404
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
