import type { MarketDefinition, MarketId } from "../domain/types";

export const MARKET_DEFINITIONS: Record<MarketId, MarketDefinition> = {
  NIFTY: {
    id: "NIFTY",
    label: "NIFTY 50",
    shortLabel: "NIFTY",
    symbols: ["NIFTY"],
    expiries: ["2026-08-27", "2026-09-03", "2026-09-24"],
    baseSpot: 24_836.45,
    strikeStep: 50,
    lotSize: 75,
    marketKind: "INDEX",
  },
  BANKNIFTY: {
    id: "BANKNIFTY",
    label: "NIFTY Bank",
    shortLabel: "BANKNIFTY",
    symbols: ["BANKNIFTY"],
    expiries: ["2026-08-27", "2026-09-24", "2026-10-29"],
    baseSpot: 53_418.8,
    strikeStep: 100,
    lotSize: 30,
    marketKind: "INDEX",
  },
  SENSEX: {
    id: "SENSEX",
    label: "BSE SENSEX",
    shortLabel: "SENSEX",
    symbols: ["SENSEX"],
    expiries: ["2026-08-27", "2026-09-24", "2026-10-29"],
    baseSpot: 81_264.2,
    strikeStep: 100,
    lotSize: 20,
    marketKind: "INDEX",
  },
  STOCK_FNO: {
    id: "STOCK_FNO",
    label: "Stock F&O",
    shortLabel: "STOCK F&O",
    symbols: ["RELIANCE", "TCS", "INFY"],
    expiries: ["2026-08-27", "2026-09-24", "2026-10-29"],
    baseSpot: 1_412.6,
    strikeStep: 20,
    lotSize: 500,
    marketKind: "STOCK",
  },
  MCX: {
    id: "MCX",
    label: "MCX Commodities",
    shortLabel: "MCX",
    symbols: ["GOLD", "CRUDEOIL", "SILVER"],
    expiries: ["2026-09-04", "2026-10-05", "2026-11-05"],
    baseSpot: 102_480,
    strikeStep: 500,
    lotSize: 1,
    marketKind: "COMMODITY",
  },
};

export const MARKET_ORDER: readonly MarketId[] = [
  "NIFTY",
  "BANKNIFTY",
  "SENSEX",
  "STOCK_FNO",
  "MCX",
];

export function definitionFor(market: MarketId): MarketDefinition {
  return MARKET_DEFINITIONS[market];
}
