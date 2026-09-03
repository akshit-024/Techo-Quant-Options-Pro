import { MARKET_ORDER, MARKET_DEFINITIONS } from "../../data/marketDefinitions";
import type { MarketId, ViewId } from "../../domain/types";
import { Icon, type IconName } from "../ui/Icon";

interface SidebarProps {
  activeView: ViewId;
  activeMarket: MarketId;
  open: boolean;
  onClose: () => void;
  onViewChange: (view: ViewId) => void;
  onMarketChange: (market: MarketId) => void;
}

const primaryItems: readonly { id: ViewId; label: string; icon: IconName }[] = [
  { id: "start", label: "Start here", icon: "spark" },
  { id: "dashboard", label: "Dashboard", icon: "grid" },
];

const engineItems: readonly { id: ViewId; label: string; icon: IconName }[] = [
  { id: "market_leaders", label: "Market leaders", icon: "activity" },
  { id: "greeks", label: "Greeks engine", icon: "greeks" },
  { id: "ranking", label: "Strike ranking", icon: "ranking" },
];

const riskItems: readonly { id: ViewId; label: string; icon: IconName }[] = [
  { id: "position_sizer", label: "Position sizer", icon: "target" },
  { id: "trade_plan", label: "Trade plan", icon: "activity" },
];

const operationsItems: readonly { id: ViewId; label: string; icon: IconName }[] = [
  { id: "api_status", label: "API status", icon: "pulse" },
  { id: "signals", label: "Signal history", icon: "spark" },
  { id: "journal", label: "Trade journal", icon: "journal" },
  { id: "backtests", label: "Backtest report", icon: "flask" },
];

const referenceItems: readonly { id: ViewId; label: string; icon: IconName }[] = [
  { id: "contract_master", label: "Contract master", icon: "database" },
  { id: "settings", label: "Settings", icon: "settings" },
  { id: "guide", label: "User guide", icon: "book" },
  { id: "audit", label: "Formula audit", icon: "shield" },
];

export function Sidebar({
  activeView,
  activeMarket,
  open,
  onClose,
  onViewChange,
  onMarketChange,
}: SidebarProps) {
  const chooseView = (view: ViewId) => {
    onViewChange(view);
    onClose();
  };

  const chooseMarket = (market: MarketId) => {
    onMarketChange(market);
    onClose();
  };

  return (
    <>
      <button
        aria-label="Close navigation"
        className={`sidebar-backdrop ${open ? "is-open" : ""}`}
        onClick={onClose}
        type="button"
      />
      <aside className={`sidebar ${open ? "is-open" : ""}`} aria-label="Primary navigation" id="primary-navigation">
        <div className="sidebar__brand">
          <div className="brand-mark" aria-hidden="true">
            <span />
            <span />
            <span />
          </div>
          <div>
            <p className="brand-name">TECO QUANT</p>
            <p className="brand-subtitle">Options intelligence</p>
          </div>
          <button className="icon-button sidebar__close" onClick={onClose} type="button" aria-label="Close navigation">
            <Icon name="x" />
          </button>
        </div>

        <nav className="sidebar__nav">
          <div className="nav-group">
            <p className="nav-group__label">Workspace</p>
            {primaryItems.map((item) => (
              <button
                className={`nav-item ${activeView === item.id ? "is-active" : ""}`}
                aria-current={activeView === item.id ? "page" : undefined}
                key={item.id}
                onClick={() => chooseView(item.id)}
                type="button"
              >
                <Icon name={item.icon} />
                <span>{item.label}</span>
              </button>
            ))}
          </div>

          <div className="nav-group">
            <div className="nav-group__heading">
              <p className="nav-group__label">Calculators</p>
              <span>5 markets</span>
            </div>
            {MARKET_ORDER.map((market) => (
              <button
                className={`nav-item nav-item--market ${
                  activeView === "calculator" && activeMarket === market ? "is-active" : ""
                }`}
                aria-current={activeView === "calculator" && activeMarket === market ? "page" : undefined}
                key={market}
                onClick={() => chooseMarket(market)}
                type="button"
              >
                <span className="market-monogram">{MARKET_DEFINITIONS[market].shortLabel.slice(0, 2)}</span>
                <span>{MARKET_DEFINITIONS[market].shortLabel}</span>
              </button>
            ))}
          </div>

          <div className="nav-group">
            <p className="nav-group__label">Analysis engines</p>
            {engineItems.map((item) => (
              <button
                className={`nav-item ${activeView === item.id ? "is-active" : ""}`}
                aria-current={activeView === item.id ? "page" : undefined}
                key={item.id}
                onClick={() => chooseView(item.id)}
                type="button"
              >
                <Icon name={item.icon} />
                <span>{item.label}</span>
              </button>
            ))}
          </div>

          <div className="nav-group">
            <p className="nav-group__label">Risk & planning</p>
            {riskItems.map((item) => (
              <button
                aria-current={activeView === item.id ? "page" : undefined}
                className={`nav-item ${activeView === item.id ? "is-active" : ""}`}
                key={item.id}
                onClick={() => chooseView(item.id)}
                type="button"
              >
                <Icon name={item.icon} />
                <span>{item.label}</span>
              </button>
            ))}
          </div>

          <div className="nav-group">
            <p className="nav-group__label">Operations</p>
            {operationsItems.map((item) => (
              <button
                aria-current={activeView === item.id ? "page" : undefined}
                className={`nav-item ${activeView === item.id ? "is-active" : ""}`}
                key={item.id}
                onClick={() => chooseView(item.id)}
                type="button"
              >
                <Icon name={item.icon} />
                <span>{item.label}</span>
              </button>
            ))}
          </div>

          <div className="nav-group">
            <p className="nav-group__label">System & reference</p>
            {referenceItems.map((item) => (
              <button
                aria-current={activeView === item.id ? "page" : undefined}
                className={`nav-item ${activeView === item.id ? "is-active" : ""}`}
                key={item.id}
                onClick={() => chooseView(item.id)}
                type="button"
              >
                <Icon name={item.icon} />
                <span>{item.label}</span>
              </button>
            ))}
          </div>
        </nav>

        <div className="sidebar__safety">
          <div className="safety-icon"><Icon name="shield" size={17} /></div>
          <div>
            <strong>Execution locked</strong>
            <span>Analysis only · no orders</span>
          </div>
        </div>
      </aside>
    </>
  );
}
