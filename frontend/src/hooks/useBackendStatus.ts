import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  BackendApiError,
  configuredApiBaseUrl,
  DEFAULT_REQUEST_TIMEOUT_MS,
  fetchBackendSnapshot,
  normalizeApiBaseUrl,
} from "../api/client";
import type { BackendReadSnapshot } from "../api/contracts";

export const BACKEND_STATUS_POLL_MS = 10_000;
export const BACKEND_STATUS_STALE_AFTER_MS = 30_000;

export type BackendConnection =
  | "CONNECTED"
  | "DISCONNECTED"
  | "STALE"
  | "NOT_CONFIGURED";

export interface BackendStatusState {
  connection: BackendConnection;
  payload: BackendReadSnapshot | null;
  lastAttemptAt: string | null;
  lastSuccessAt: string | null;
  error: string | null;
  isLoading: boolean;
  refresh: () => void;
}

export interface UseBackendStatusOptions {
  /** Override the environment for tests/embedding; null explicitly disables requests. */
  baseUrl?: string | null;
  pollIntervalMs?: number;
  requestTimeoutMs?: number;
  staleAfterMs?: number;
}

interface BaseResolution {
  baseUrl: string | null;
  configured: boolean;
  error: string | null;
}

interface InternalState extends Omit<BackendStatusState, "refresh"> {}

export function useBackendStatus(
  options: UseBackendStatusOptions = {},
): BackendStatusState {
  const {
    baseUrl: requestedBaseUrl,
    pollIntervalMs = BACKEND_STATUS_POLL_MS,
    requestTimeoutMs = DEFAULT_REQUEST_TIMEOUT_MS,
    staleAfterMs = BACKEND_STATUS_STALE_AFTER_MS,
  } = options;
  validateDuration(pollIntervalMs, "pollIntervalMs");
  validateDuration(requestTimeoutMs, "requestTimeoutMs");
  validateDuration(staleAfterMs, "staleAfterMs");

  const resolution = useMemo(
    () => resolveBaseUrl(requestedBaseUrl),
    [requestedBaseUrl],
  );
  const [state, setState] = useState<InternalState>(() =>
    initialState(resolution),
  );
  const inFlight = useRef<AbortController | null>(null);
  const requestSequence = useRef(0);
  const staleTimer = useRef<ReturnType<typeof globalThis.setTimeout> | null>(null);

  const clearStaleTimer = useCallback(() => {
    if (staleTimer.current !== null) {
      globalThis.clearTimeout(staleTimer.current);
      staleTimer.current = null;
    }
  }, []);

  const load = useCallback(async () => {
    if (resolution.baseUrl === null || resolution.error !== null) return;
    const sequence = ++requestSequence.current;
    inFlight.current?.abort();
    const controller = new AbortController();
    inFlight.current = controller;
    const attemptedAt = new Date().toISOString();
    setState((current) => ({
      ...current,
      lastAttemptAt: attemptedAt,
      isLoading: true,
    }));

    try {
      const payload = await fetchBackendSnapshot({
        baseUrl: resolution.baseUrl,
        signal: controller.signal,
        timeoutMs: requestTimeoutMs,
      });
      if (controller.signal.aborted || sequence !== requestSequence.current) return;
      clearStaleTimer();
      setState({
        connection: "CONNECTED",
        payload,
        lastAttemptAt: attemptedAt,
        lastSuccessAt: payload.fetchedAt,
        error: null,
        isLoading: false,
      });
      staleTimer.current = globalThis.setTimeout(() => {
        setState((current) =>
          current.lastSuccessAt === payload.fetchedAt
            ? { ...current, connection: "STALE" }
            : current,
        );
      }, staleAfterMs);
    } catch (error) {
      if (controller.signal.aborted || sequence !== requestSequence.current) return;
      clearStaleTimer();
      setState((current) => ({
        ...current,
        connection: current.payload === null ? "DISCONNECTED" : "STALE",
        error: statusErrorMessage(error),
        isLoading: false,
      }));
    }
  }, [clearStaleTimer, requestTimeoutMs, resolution, staleAfterMs]);

  useEffect(() => {
    inFlight.current?.abort();
    clearStaleTimer();
    requestSequence.current += 1;
    setState(initialState(resolution));
    if (resolution.baseUrl === null || resolution.error !== null) return undefined;

    void load();
    const interval = globalThis.setInterval(() => void load(), pollIntervalMs);
    return () => {
      globalThis.clearInterval(interval);
      clearStaleTimer();
      requestSequence.current += 1;
      inFlight.current?.abort();
      inFlight.current = null;
    };
  }, [clearStaleTimer, load, pollIntervalMs, resolution]);

  const refresh = useCallback(() => void load(), [load]);
  return { ...state, refresh };
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
      error:
        error instanceof BackendApiError
          ? error.message
          : "API base URL configuration is invalid",
    };
  }
}

function initialState(resolution: BaseResolution): InternalState {
  if (!resolution.configured) {
    return {
      connection: "NOT_CONFIGURED",
      payload: null,
      lastAttemptAt: null,
      lastSuccessAt: null,
      error: null,
      isLoading: false,
    };
  }
  return {
    connection: "DISCONNECTED",
    payload: null,
    lastAttemptAt: null,
    lastSuccessAt: null,
    error: resolution.error,
    isLoading: false,
  };
}

function statusErrorMessage(error: unknown): string {
  if (error instanceof BackendApiError) return error.message;
  return "Backend status request failed";
}

function validateDuration(value: number, name: string): void {
  if (!Number.isFinite(value) || value <= 0) {
    throw new RangeError(`${name} must be finite and positive`);
  }
}
