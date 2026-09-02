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
import { RankingView } from "./features/ranking/RankingView";
import { StartView } from "./features/start/StartView";
import { RiskWorkspace } from "./features/risk";
import { ApiStatusView } from "./features/status/ApiStatusView";
import { useBackendStatus } from "./hooks/useBackendStatus";
import { useLiveMarketData } from "./hooks/useLiveMarketData";
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

function isMarketId(value: string): value is MarketId {
  return Object.hasOwn(MARKET_DEFINITIONS, value);
}

function catalogMarketIds(catalog: MarketCatalogResponse | null): readonly MarketId[] {
  if (catalog === null) return [];
  return catalog.markets
    .map((market) => market.market_id)
    .filter(isMarketId);
}

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
  const marketIds = catalogMarketIds(catalog);
  if (marketIds.length === 0) return current;
  const market = marketIds.includes(current.market) ? current.market : marketIds[0];
  const marketEntry = catalog?.markets.find((item) => item.market_id === market);
  const symbols = marketEntry?.symbols ?? [];
  if (symbols.length === 0) return current;
  const selectedSymbol =
    symbols.find((item) => item.symbol === current.symbol) ?? symbols[0];
  const matchedExpiry = selectedSymbol.expiries.find(
    (expiry) =>
      expiry === current.expiry || expiry.slice(0, 10) === current.expiry.slice(0, 10),
  );
  return {
    market,
    symbol: selectedSymbol.symbol,
    expiry: matchedExpiry ?? selectedSymbol.expiries[0] ?? current.expiry,
  };
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
  }, [liveMarket.error, liveMarket.stale, liveMarket.workspace]);
  const snapshot =
    dataSourceMode === "LIVE" && liveAdaptation.snapshot !== null
      ? liveAdaptation.snapshot
      : demoSnapshot;
  const liveWorkspaceUnavailable =
    dataSourceMode === "LIVE" && liveAdaptation.snapshot === null;
  const gateConnection =
    dataSourceMode === "LIVE" && liveMarket.connection !== "LIVE"
      ? liveMarket.workspace === null
        ? "DISCONNECTED"
        : "STALE"
      : backendStatus.connection;
  const operationalGate = useMemo(
    () => evaluateOperationalGate(snapshot, { connectionState: gateConnection }),
    [gateConnection, snapshot],
  );
  const fallbackLegKey = `${snapshot.ranking[0].strike}:${snapshot.ranking[0].side}`;
  const effectiveSelectedLegKey = snapshot.ranking.some(
    (entry) => `${entry.strike}:${entry.side}` === selectedLegKey,
  )
    ? selectedLegKey
    : fallbackLegKey;
  const defaultRiskEvaluation = useMemo(() => {
    const defaults = snapshotRiskDefaults(snapshot, effectiveSelectedLegKey);
    return buildRiskTradePlan(snapshot, effectiveSelectedLegKey, {
      capital: defaults.capital,
      riskPercent: defaults.riskPercent,
      allocationPercent: defaults.allocationPercent,
      stop: defaults.stop,
    });
  }, [effectiveSelectedLegKey, snapshot]);

  const activeCatalog = dataSourceMode === "LIVE" ? liveMarket.catalog : null;
  const liveMarketOptions = catalogMarketIds(activeCatalog);
  const marketOptions =
    liveMarketOptions.length > 0
      ? liveMarketOptions
      : MARKET_ORDER;
  const catalogMarket = activeCatalog?.markets.find(
    (item) => item.market_id === selection.market,
  );
  const symbolOptions =
    catalogMarket !== undefined && catalogMarket.symbols.length > 0
      ? catalogMarket.symbols.map((item) => item.symbol)
      : MARKET_DEFINITIONS[selection.market].symbols;
  const selectedCatalogSymbol = catalogSymbol(
    activeCatalog,
    selection.market,
    selection.symbol,
  );
  const expiryOptions =
    selectedCatalogSymbol !== null && selectedCatalogSymbol.expiries.length > 0
      ? selectedCatalogSymbol.expiries
      : MARKET_DEFINITIONS[selection.market].expiries;

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
    if (snapshot.dataMode !== "DEMO") return;
    setManualOverrides((current) => {
      const next = { ...current };
      if (value.trim()) next[id] = value.trim();
      else delete next[id];
      return next;
    });
  };
  const feedStatus = backendStatus.payload?.status.market_data?.feed;
  const liveUnavailableReason =
    liveAdaptation.error ??
    liveMarket.error ??
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
          capturedAt={snapshot.capturedAt}
          backendConnection={backendStatus.connection}
          dataMode={snapshot.dataMode}
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
          {liveWorkspaceUnavailable ? (
            <aside className="live-data-notice" aria-label="Live market data status">
              <div>
                <strong>Live workspace unavailable</strong>
                <p>{liveUnavailableReason} A non-actionable demo preview is shown below.</p>
              </div>
              <div className="live-data-notice__actions">
                <button onClick={liveMarket.refresh} type="button">Retry live data</button>
                <button onClick={() => setDataSourceMode("DEMO")} type="button">Use demo workspace</button>
              </div>
            </aside>
          ) : null}
          {activeView === "start" ? <StartView onNavigate={setActiveView} /> : null}
          {activeView === "dashboard" ? (
            <DashboardView
              onLegSelect={setSelectedLegKey}
              onNavigate={setActiveView}
              operationalGate={operationalGate}
              riskEvaluation={defaultRiskEvaluation}
              selectedLegKey={effectiveSelectedLegKey}
              snapshot={snapshot}
            />
          ) : null}
          {activeView === "calculator" ? (
            <CalculatorView
              onLegSelect={setSelectedLegKey}
              onOverride={setOverride}
              presentationMode={presentationMode}
              selectedLegKey={effectiveSelectedLegKey}
              snapshot={snapshot}
            />
          ) : null}
          {activeView === "greeks" ? (
            <GreeksView
              onLegSelect={setSelectedLegKey}
              selectedLegKey={effectiveSelectedLegKey}
              snapshot={snapshot}
            />
          ) : null}
          {activeView === "ranking" ? (
            <RankingView
              onLegSelect={setSelectedLegKey}
              selectedLegKey={effectiveSelectedLegKey}
              snapshot={snapshot}
            />
          ) : null}
          {activeView === "position_sizer" || activeView === "trade_plan" ? (
            <RiskWorkspace
              onLegSelect={setSelectedLegKey}
              selectedLegKey={effectiveSelectedLegKey}
              snapshot={snapshot}
            />
          ) : null}
          {activeView === "api_status" ? (
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
          {activeView === "audit" ? <FormulaAuditView snapshot={snapshot} /> : null}
        </main>
        <footer className="app-footer">
          <p>For education and analytical support only. Not investment advice. Live, stale, and demo data are explicitly labelled.</p>
          <span>TECO QUANT PRO · Sprint 4</span>
        </footer>
      </div>
    </div>
  );
}
