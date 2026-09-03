import { useEffect, useMemo, useState } from "react";

import { Sidebar } from "./components/layout/Sidebar";
import { WorkspaceHeader } from "./components/layout/WorkspaceHeader";
import { MARKET_DEFINITIONS, MARKET_ORDER } from "./data/marketDefinitions";
import {
  adaptBackendSnapshot,
  BackendSnapshotAdapterError,
} from "./data/backendSnapshot";
import { buildDemoSnapshot, type ManualOverrides } from "./data/demoSnapshot";
import type {
  MarketId,
  MarketSnapshot,
  ViewId,
  WorkspaceSelection,
} from "./domain/types";
import { evaluateOperationalGate } from "./domain/gate";
import { buildRiskTradePlan, snapshotRiskDefaults } from "./domain/risk";
import { GreeksView } from "./features/analytics/GreeksView";
import { CalculatorView } from "./features/calculator/CalculatorView";
import { DashboardView } from "./features/dashboard/DashboardView";
import { MarketLeadersView } from "./features/leaders/MarketLeadersView";
import { RankingView } from "./features/ranking/RankingView";
import { StartView } from "./features/start/StartView";
import { RiskWorkspace } from "./features/risk";
import { ApiStatusView } from "./features/status/ApiStatusView";
import { useBackendStatus } from "./hooks/useBackendStatus";
import { useLiveMarketData } from "./hooks/useLiveMarketData";
import { useMarketLeaders } from "./hooks/useMarketLeaders";
import type {
  MarketCatalogResponse,
  MarketCatalogSymbol,
} from "./api/contracts";
import {
  BacktestReportView,
  ContractMasterView,
  FormulaAuditView,
  SettingsView,
  SignalHistoryView,
  TradeJournalView,
  UserGuideView,
} from "./features/operations";
import "./styles/global.css";
import "./styles/sprint4.css";

const initialSelection: WorkspaceSelection = {
  market: "NIFTY",
  symbol: MARKET_DEFINITIONS.NIFTY.symbols[0],
  expiry: MARKET_DEFINITIONS.NIFTY.expiries[0],
};

const guideDestinations: Readonly<Record<string, ViewId>> = {
  Dashboard: "dashboard",
  Calculator: "calculator",
  "Strike ranking": "ranking",
  Settings: "settings",
};

function catalogSymbol(
  catalog: MarketCatalogResponse | null,
  market: MarketId,
  symbol: string,
): MarketCatalogSymbol | null {
  const marketEntry = catalog?.markets.find((item) => item.market_id === market);
  return marketEntry?.symbols.find((item) => item.symbol === symbol) ?? null;
}

function selectionForCatalog(
  current: WorkspaceSelection,
  catalog: MarketCatalogResponse | null,
): WorkspaceSelection {
  // A catalog is a publication snapshot, not the configured universe. If one
  // market is temporarily absent, never jump the user's selection to whichever
  // market happened to publish first.
  const marketEntry = catalog?.markets.find(
    (item) => item.market_id === current.market,
  );
  const selectedSymbol = marketEntry?.symbols.find(
    (item) => item.symbol === current.symbol,
  );
  if (selectedSymbol === undefined) return current;
  const matchedExpiry = selectedSymbol.expiries.find(
    (expiry) =>
      expiry === current.expiry || expiry.slice(0, 10) === current.expiry.slice(0, 10),
  );
  return {
    market: current.market,
    symbol: selectedSymbol.symbol,
    expiry: matchedExpiry ?? selectedSymbol.expiries[0] ?? current.expiry,
  };
}

function uniqueOptions(values: readonly string[]): readonly string[] {
  return [...new Set(values)];
}

function snapshotMatchesSelection(
  snapshot: MarketSnapshot,
  selection: WorkspaceSelection,
): boolean {
  return (
    snapshot.selection.market === selection.market &&
    snapshot.selection.symbol === selection.symbol &&
    snapshot.selection.expiry === selection.expiry
  );
}

function retainedAsStale(
  snapshot: MarketSnapshot,
  reason: string,
): MarketSnapshot {
  const authority = snapshot.backendAuthority;
  return {
    ...snapshot,
    dataMode: "STALE",
    analytics: {
      ...snapshot.analytics,
      decision: "NO TRADE",
      decisionReason: reason,
    },
    backendAuthority:
      authority === undefined
        ? undefined
        : {
            ...authority,
            fresh: false,
            actionable: false,
            blockers: [...new Set([...authority.blockers, "LIVE_REFRESH_UNAVAILABLE"])],
          },
  };
}

export default function App() {
  const [activeView, setActiveView] = useState<ViewId>("dashboard");
  const [selection, setSelection] = useState<WorkspaceSelection>(initialSelection);
  const [dataSourceMode, setDataSourceMode] = useState<"LIVE" | "DEMO">("LIVE");
  const [presentationMode, setPresentationMode] = useState<"QUICK" | "PRO">("PRO");
  const [manualOverrides, setManualOverrides] = useState<ManualOverrides>({});
  const [selectedLegKey, setSelectedLegKey] = useState("");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const backendStatus = useBackendStatus();
  const apiSelection = useMemo(
    () => ({
      market_id: selection.market,
      symbol: selection.symbol,
      expiry: selection.expiry,
    }),
    [selection],
  );
  const liveMarket = useLiveMarketData(apiSelection, {
    enabled: dataSourceMode === "LIVE",
  });
  const marketLeaders = useMarketLeaders(selection.market, {
    enabled: dataSourceMode === "LIVE" && activeView === "market_leaders",
  });

  useEffect(() => {
    if (dataSourceMode !== "LIVE" || liveMarket.catalog === null) return;
    const next = selectionForCatalog(selection, liveMarket.catalog);
    if (
      next.market !== selection.market ||
      next.symbol !== selection.symbol ||
      next.expiry !== selection.expiry
    ) {
      setSelection(next);
      setManualOverrides({});
      setSelectedLegKey("");
    }
  }, [dataSourceMode, liveMarket.catalog, selection]);

  const demoSnapshot = useMemo(
    () => buildDemoSnapshot(selection, manualOverrides),
    [selection, manualOverrides],
  );
  const liveAdaptation = useMemo(() => {
    if (liveMarket.workspace === null) {
      return { snapshot: null, error: null };
    }
    try {
      const adapted = adaptBackendSnapshot(liveMarket.workspace);
      if (!snapshotMatchesSelection(adapted, selection)) {
        return {
          snapshot: null,
          error: "An obsolete workspace response was discarded after the market selection changed.",
        };
      }
      return {
        snapshot:
          liveMarket.stale && adapted.dataMode === "LIVE"
            ? retainedAsStale(
                adapted,
                liveMarket.error ?? "The live refresh stream is unavailable.",
              )
            : adapted,
        error: null,
      };
    } catch (error) {
      return {
        snapshot: null,
        error:
          error instanceof BackendSnapshotAdapterError
            ? `${error.path}: ${error.message}`
            : "The backend workspace could not be represented safely.",
      };
    }
  }, [liveMarket.error, liveMarket.stale, liveMarket.workspace, selection]);
  // LIVE mode must never silently fall back to demo data.
  // Demo data is used only when the user explicitly selects DEMO.
  const snapshot: MarketSnapshot | null =
    dataSourceMode === "LIVE" ? liveAdaptation.snapshot : demoSnapshot;

  const liveWorkspaceUnavailable =
    dataSourceMode === "LIVE" && snapshot === null;
  const liveWorkspaceStale =
    dataSourceMode === "LIVE" && snapshot?.dataMode === "STALE";

  const gateConnection =
    dataSourceMode === "LIVE" && liveMarket.connection !== "LIVE"
      ? liveMarket.workspace === null
        ? "DISCONNECTED"
        : "STALE"
      : backendStatus.connection;

  const operationalGate = useMemo(
    () =>
      snapshot === null
        ? null
        : evaluateOperationalGate(snapshot, {
            connectionState: gateConnection,
          }),
    [gateConnection, snapshot],
  );

  const firstRankedLeg =
    snapshot?.ranking.find((entry) => entry.rejectionReasons.length === 0) ??
    snapshot?.ranking[0] ??
    null;

  const fallbackLegKey =
    firstRankedLeg === null
      ? ""
      : `${firstRankedLeg.strike}:${firstRankedLeg.side}`;

  const effectiveSelectedLegKey =
    snapshot !== null &&
    snapshot.ranking.some(
      (entry) => `${entry.strike}:${entry.side}` === selectedLegKey,
    )
      ? selectedLegKey
      : fallbackLegKey;

  const defaultRiskEvaluation = useMemo(() => {
    if (snapshot === null || effectiveSelectedLegKey === "") {
      return null;
    }

    const defaults = snapshotRiskDefaults(
      snapshot,
      effectiveSelectedLegKey,
    );

    return buildRiskTradePlan(
      snapshot,
      effectiveSelectedLegKey,
      {
        capital: defaults.capital,
        riskPercent: defaults.riskPercent,
        allocationPercent: defaults.allocationPercent,
        stop: defaults.stop,
      },
    );
  }, [effectiveSelectedLegKey, snapshot]);

  const activeCatalog = dataSourceMode === "LIVE" ? liveMarket.catalog : null;
  // Keep every configured bracket selectable even while backend publication is
  // partial. The catalog only supplies currently published symbols/expiries.
  const marketOptions = MARKET_ORDER;
  const catalogMarket = activeCatalog?.markets.find(
    (item) => item.market_id === selection.market,
  );
  const symbolOptions = uniqueOptions([
    ...MARKET_DEFINITIONS[selection.market].symbols,
    ...(catalogMarket?.symbols.map((item) => item.symbol) ?? []),
    selection.symbol,
  ]);
  const selectedCatalogSymbol = catalogSymbol(
    activeCatalog,
    selection.market,
    selection.symbol,
  );
  const expiryOptions =
    dataSourceMode === "LIVE"
      ? uniqueOptions([
          ...(selectedCatalogSymbol?.expiries ?? []),
          selection.expiry,
        ])
      : uniqueOptions([
          ...MARKET_DEFINITIONS[selection.market].expiries,
          selection.expiry,
        ]);

  const selectMarket = (market: MarketId, navigateToCalculator = false) => {
    const definition = MARKET_DEFINITIONS[market];
    const catalogEntry = activeCatalog?.markets.find(
      (item) => item.market_id === market,
    );
    const liveSymbol = catalogEntry?.symbols[0];
    setSelection({
      market,
      symbol: liveSymbol?.symbol ?? definition.symbols[0],
      expiry: liveSymbol?.expiries[0] ?? definition.expiries[0],
    });
    setManualOverrides({});
    setSelectedLegKey("");
    if (navigateToCalculator) setActiveView("calculator");
  };

  const selectSymbol = (symbol: string) => {
    const liveSymbol = catalogSymbol(activeCatalog, selection.market, symbol);
    setSelection((current) => ({
      ...current,
      symbol,
      expiry: liveSymbol?.expiries[0] ?? current.expiry,
    }));
    setManualOverrides({});
    setSelectedLegKey("");
  };

  const selectExpiry = (expiry: string) => {
    setSelection((current) => ({ ...current, expiry }));
    setManualOverrides({});
    setSelectedLegKey("");
  };

  const setOverride = (id: string, value: string) => {
    if (snapshot === null || snapshot.dataMode !== "DEMO") return;
    setManualOverrides((current) => {
      const next = { ...current };
      if (value.trim()) next[id] = value.trim();
      else delete next[id];
      return next;
    });
  };
  const feedStatus = backendStatus.payload?.status.market_data?.feed;
  const selectedFeedStatus = feedStatus?.markets?.[selection.symbol];
  const selectedFeedFailure =
    selectedFeedStatus?.error_code == null
      ? null
      : `${selection.symbol} acquisition failed safely (${selectedFeedStatus.error_code}).`;
  const authorityBlockers = snapshot?.backendAuthority?.blockers;
  const liveUnavailableReason =
    liveAdaptation.error ??
    selectedFeedFailure ??
    liveMarket.error ??
    (authorityBlockers !== undefined && authorityBlockers.length > 0
      ? authorityBlockers.join(", ")
      : null) ??
    (liveMarket.connection === "LOADING"
      ? "Waiting for the backend market catalog and first validated snapshot."
      : liveMarket.connection === "EMPTY"
        ? "The backend is connected but has not published a market snapshot yet."
        : liveMarket.connection === "NOT_CONFIGURED"
          ? "VITE_API_BASE_URL is not configured."
          : "No validated live workspace is available for this selection.");

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">Skip to content</a>
      <Sidebar
        activeMarket={selection.market}
        activeView={activeView}
        onClose={() => setSidebarOpen(false)}
        onMarketChange={(market) => selectMarket(market, true)}
        onViewChange={setActiveView}
        open={sidebarOpen}
      />
      <div className="workspace">
        <WorkspaceHeader
          capturedAt={snapshot?.capturedAt ?? ""}
          backendConnection={backendStatus.connection}
          dataMode={snapshot?.dataMode ?? "UNAVAILABLE"}
          dataSourceMode={dataSourceMode}
          expiryOptions={expiryOptions}
          marketOptions={marketOptions}
          navigationOpen={sidebarOpen}
          onDataSourceModeChange={(mode) => {
            setDataSourceMode(mode);
            setManualOverrides({});
            setSelectedLegKey("");
          }}
          onExpiryChange={selectExpiry}
          onMarketChange={(market) => selectMarket(market)}
          onMenuOpen={() => setSidebarOpen(true)}
          onPresentationModeChange={setPresentationMode}
          onSymbolChange={selectSymbol}
          presentationMode={presentationMode}
          selection={selection}
          symbolOptions={symbolOptions}
        />
        <main className="main-content" id="main-content">
          {liveWorkspaceUnavailable || liveWorkspaceStale ? (
            <aside className="live-data-notice" aria-label="Live market data status">
              <div>
                <strong>
                  {liveWorkspaceStale
                    ? "Live workspace stale"
                    : "Live workspace unavailable"}
                </strong>
                <p>
                  {liveUnavailableReason} Demo data will not be substituted while LIVE is selected.
                </p>
              </div>
              <div className="live-data-notice__actions">
                <button onClick={liveMarket.refresh} type="button">Retry live data</button>
              </div>
            </aside>
          ) : null}
          {activeView === "start" ? <StartView onNavigate={setActiveView} /> : null}
          {activeView === "dashboard" &&
          snapshot !== null &&
          operationalGate !== null &&
          defaultRiskEvaluation !== null ? (
            <DashboardView
              onLegSelect={setSelectedLegKey}
              onNavigate={setActiveView}
              operationalGate={operationalGate}
              riskEvaluation={defaultRiskEvaluation}
              selectedLegKey={effectiveSelectedLegKey}
              snapshot={snapshot}
            />
          ) : null}
          {activeView === "calculator" && snapshot !== null ? (
            <CalculatorView
              onLegSelect={setSelectedLegKey}
              onOverride={setOverride}
              presentationMode={presentationMode}
              selectedLegKey={effectiveSelectedLegKey}
              snapshot={snapshot}
            />
          ) : null}
          {activeView === "greeks" && snapshot !== null ? (
            <GreeksView
              onLegSelect={setSelectedLegKey}
              selectedLegKey={effectiveSelectedLegKey}
              snapshot={snapshot}
            />
          ) : null}
          {activeView === "market_leaders" ? (
            <MarketLeadersView
              key={selection.market}
              connection={marketLeaders.connection}
              dataSourceMode={dataSourceMode}
              error={marketLeaders.error}
              market={selection.market}
              onMarketChange={(market) => selectMarket(market)}
              onRefresh={marketLeaders.refresh}
              response={marketLeaders.response}
            />
          ) : null}
          {activeView === "ranking" && snapshot !== null ? (
            <RankingView
              onLegSelect={setSelectedLegKey}
              selectedLegKey={effectiveSelectedLegKey}
              snapshot={snapshot}
            />
          ) : null}
          {(activeView === "position_sizer" || activeView === "trade_plan") &&
          snapshot !== null ? (
            <RiskWorkspace
              onLegSelect={setSelectedLegKey}
              selectedLegKey={effectiveSelectedLegKey}
              snapshot={snapshot}
            />
          ) : null}
          {activeView === "api_status" && snapshot !== null ? (
            <ApiStatusView snapshot={snapshot} state={backendStatus} />
          ) : null}
          {activeView === "signals" ? <SignalHistoryView /> : null}
          {activeView === "journal" ? <TradeJournalView /> : null}
          {activeView === "backtests" ? <BacktestReportView /> : null}
          {activeView === "contract_master" ? <ContractMasterView /> : null}
          {activeView === "settings" ? (
            <SettingsView
              decisionInputsConfigured={feedStatus?.decision_inputs_configured === true}
              marketDataState={feedStatus?.state ?? liveMarket.connection}
            />
          ) : null}
          {activeView === "guide" ? (
            <UserGuideView
              onNavigate={(destination) => {
                const view = guideDestinations[destination];
                if (view !== undefined) setActiveView(view);
              }}
            />
          ) : null}
          {activeView === "audit" && snapshot !== null ? (
            <FormulaAuditView snapshot={snapshot} />
          ) : null}
        </main>
        <footer className="app-footer">
          <p>For education and analytical support only. Not investment advice. Live, stale, and demo data are explicitly labelled.</p>
          <span>TECO QUANT PRO · Sprint 4</span>
        </footer>
      </div>
    </div>
  );
}
