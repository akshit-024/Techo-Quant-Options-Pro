import { MARKET_DEFINITIONS, MARKET_ORDER } from "../../data/marketDefinitions";
import type { MarketId, MarketSnapshot, WorkspaceSelection } from "../../domain/types";
import type { BackendConnection } from "../../hooks/useBackendStatus";
import { formatExpiry } from "../../lib/format";
import { Badge } from "../ui/Badge";
import { Icon } from "../ui/Icon";

interface WorkspaceHeaderProps {
  selection: WorkspaceSelection;
  marketOptions?: readonly MarketId[];
  symbolOptions?: readonly string[];
  expiryOptions?: readonly string[];
  capturedAt: string;
  backendConnection: BackendConnection;
  dataMode: MarketSnapshot["dataMode"];
  dataSourceMode: "LIVE" | "DEMO";
  navigationOpen: boolean;
  presentationMode: "QUICK" | "PRO";
  onMenuOpen: () => void;
  onMarketChange: (market: MarketId) => void;
  onSymbolChange: (symbol: string) => void;
  onExpiryChange: (expiry: string) => void;
  onPresentationModeChange: (mode: "QUICK" | "PRO") => void;
  onDataSourceModeChange: (mode: "LIVE" | "DEMO") => void;
}

export function WorkspaceHeader({
  selection,
  marketOptions = MARKET_ORDER,
  symbolOptions,
  expiryOptions,
  capturedAt,
  backendConnection,
  dataMode,
  dataSourceMode,
  navigationOpen,
  presentationMode,
  onMenuOpen,
  onMarketChange,
  onSymbolChange,
  onExpiryChange,
  onPresentationModeChange,
  onDataSourceModeChange,
}: WorkspaceHeaderProps) {
  const definition = MARKET_DEFINITIONS[selection.market];
  const symbols = symbolOptions ?? definition.symbols;
  const expiries = expiryOptions ?? definition.expiries;
  const snapshotTime = new Intl.DateTimeFormat("en-IN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
    timeZone: "Asia/Kolkata",
  }).format(new Date(capturedAt));

  return (
    <header className="workspace-header">
      <div className="workspace-header__topline">
        <button
          aria-controls="primary-navigation"
          aria-expanded={navigationOpen}
          aria-label="Open navigation"
          className="icon-button mobile-menu"
          onClick={onMenuOpen}
          type="button"
        >
          <Icon name="menu" />
        </button>
        <div className="workspace-header__context">
          <span className="workspace-header__kicker">Market workspace</span>
          <strong>{definition.label}</strong>
        </div>
        <div className="workspace-header__status">
          <Badge
            tone={backendConnection === "CONNECTED" ? "positive" : backendConnection === "STALE" ? "warning" : "danger"}
            dot
          >
            API {backendConnection.toLowerCase().replace("_", " ")}
          </Badge>
          <Badge tone={dataMode === "DEMO" ? "purple" : dataMode === "STALE" ? "warning" : "positive"} dot>{dataMode.toLowerCase()} snapshot</Badge>
          <Badge tone="danger"><Icon name="lock" size={12} /> Live locked</Badge>
        </div>
      </div>

      <div className="selection-bar" aria-label="Market selection">
        <label className="select-field">
          <span>Market</span>
          <select
            aria-label="Market"
            onChange={(event) => onMarketChange(event.target.value as MarketId)}
            value={selection.market}
          >
            {marketOptions.map((market) => (
              <option key={market} value={market}>{MARKET_DEFINITIONS[market].shortLabel}</option>
            ))}
          </select>
        </label>

        <label className="select-field">
          <span>Symbol</span>
          <select aria-label="Symbol" onChange={(event) => onSymbolChange(event.target.value)} value={selection.symbol}>
            {symbols.map((symbol) => <option key={symbol}>{symbol}</option>)}
          </select>
        </label>

        <label className="select-field select-field--expiry">
          <span>Option expiry</span>
          <select aria-label="Option expiry" onChange={(event) => onExpiryChange(event.target.value)} value={selection.expiry}>
            {expiries.map((expiry) => <option key={expiry} value={expiry}>{formatExpiry(expiry)}</option>)}
          </select>
        </label>

        <div className="mode-switch" role="group" aria-label="Data source">
          <span>Source</span>
          <div>
            {(["LIVE", "DEMO"] as const).map((mode) => (
              <button
                aria-pressed={dataSourceMode === mode}
                className={dataSourceMode === mode ? "is-active" : ""}
                key={mode}
                onClick={() => onDataSourceModeChange(mode)}
                type="button"
              >
                {mode === "LIVE" ? "Live" : "Demo"}
              </button>
            ))}
          </div>
        </div>

        <div className="mode-switch" role="group" aria-label="Presentation mode">
          <span>View</span>
          <div>
            {(["QUICK", "PRO"] as const).map((mode) => (
              <button
                aria-pressed={presentationMode === mode}
                className={presentationMode === mode ? "is-active" : ""}
                key={mode}
                onClick={() => onPresentationModeChange(mode)}
                type="button"
              >
                {mode === "QUICK" ? "Quick" : "Pro"}
              </button>
            ))}
          </div>
        </div>

        <div className="selection-bar__timestamp">
          <span className="status-pulse" />
          <div>
            <span>Snapshot time</span>
            <strong>{snapshotTime} IST</strong>
          </div>
        </div>
      </div>
    </header>
  );
}
