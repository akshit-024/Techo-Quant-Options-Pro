import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import App from "./App";
import { MARKET_DEFINITIONS } from "./data/marketDefinitions";
import { buildDemoSnapshot } from "./data/demoSnapshot";
import { formatNumber } from "./lib/format";

function navigation(): HTMLElement {
  return screen.getByRole("complementary", { name: "Primary navigation" });
}

async function openNiftyCalculator(
  user: ReturnType<typeof userEvent.setup>,
): Promise<void> {
  await user.click(
    within(navigation()).getByRole("button", { name: "NI NIFTY" }),
  );
  expect(
    await screen.findByRole("heading", { name: "NIFTY evidence workspace" }),
  ).toBeInTheDocument();
}

function containingSection(element: HTMLElement): HTMLElement {
  const section = element.closest("section");
  if (!(section instanceof HTMLElement)) {
    throw new Error("expected the rendered element to be inside a section");
  }
  return section;
}

describe("Sprint 3 and Sprint 4 application workflows", () => {
  it("renders the decision workspace with explicit demo and execution locks", () => {
    render(<App />);

    expect(screen.getByText("Live locked")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Execution is locked; this screen cannot place an order.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Live, stale, and demo data are explicitly labelled/),
    ).toBeInTheDocument();
    expect(
      within(navigation()).getByRole("button", { name: "Position sizer" }),
    ).toBeEnabled();
    expect(
      screen.queryByRole("button", {
        name: /place order|execute order|buy now|sell now/i,
      }),
    ).not.toBeInTheDocument();
  });

  it("keeps market, symbol, and expiry options synchronized", async () => {
    const user = userEvent.setup();
    render(<App />);

    const market = screen.getByLabelText("Market") as HTMLSelectElement;
    const symbol = screen.getByLabelText("Symbol") as HTMLSelectElement;
    const expiry = screen.getByLabelText("Option expiry") as HTMLSelectElement;

    await user.selectOptions(market, "STOCK_FNO");
    expect(symbol).toHaveValue("RELIANCE");
    expect(
      within(symbol)
        .getAllByRole("option")
        .map((option) => (option as HTMLOptionElement).value),
    ).toEqual(["RELIANCE", "TCS", "INFY"]);
    expect(expiry).toHaveValue(MARKET_DEFINITIONS.STOCK_FNO.expiries[0]);

    await user.selectOptions(symbol, "TCS");
    await user.selectOptions(expiry, MARKET_DEFINITIONS.STOCK_FNO.expiries[1]);
    expect(symbol).toHaveValue("TCS");
    expect(expiry).toHaveValue(MARKET_DEFINITIONS.STOCK_FNO.expiries[1]);
    expect(screen.getByRole("heading", { name: "TCS" })).toBeInTheDocument();

    await user.selectOptions(market, "MCX");
    expect(symbol).toHaveValue("GOLD");
    expect(expiry).toHaveValue(MARKET_DEFINITIONS.MCX.expiries[0]);
    expect(
      within(symbol)
        .getAllByRole("option")
        .map((option) => (option as HTMLOptionElement).value),
    ).toEqual(["GOLD", "CRUDEOIL", "SILVER"]);

    await user.selectOptions(symbol, "SILVER");
    await user.selectOptions(expiry, MARKET_DEFINITIONS.MCX.expiries[2]);
    expect(symbol).toHaveValue("SILVER");
    expect(expiry).toHaveValue(MARKET_DEFINITIONS.MCX.expiries[2]);
    expect(screen.getByRole("heading", { name: "SILVER" })).toBeInTheDocument();
  });

  it("navigates to the calculator and keeps imported, manual, and effective values separate", async () => {
    const user = userEvent.setup();
    render(<App />);
    await openNiftyCalculator(user);

    expect(
      screen.getAllByRole("columnheader", { name: "Imported value" }),
    ).toHaveLength(4);
    expect(
      screen.getAllByRole("columnheader", { name: "Manual override" }),
    ).toHaveLength(4);
    expect(
      screen.getAllByRole("columnheader", { name: "Effective value" }),
    ).toHaveLength(4);
    expect(screen.getByText("No overrides")).toBeInTheDocument();

    let spotRow = screen.getByRole("rowheader", { name: /Spot price/ }).closest("tr");
    if (!(spotRow instanceof HTMLTableRowElement)) throw new Error("spot row missing");
    let cells = within(spotRow).getAllByRole("cell");
    const importedSpot = cells[0].textContent?.trim();
    expect(importedSpot).toBeTruthy();
    expect(cells[1]).toHaveTextContent("Add override");
    expect(cells[2]).toHaveTextContent(importedSpot ?? "");

    await user.click(within(spotRow).getByRole("button", { name: /Add override/ }));
    const editor = screen.getByRole("textbox", {
      name: "Manual override for Spot price",
    });
    await user.type(editor, "25000.50");
    await user.tab();

    spotRow = screen.getByRole("rowheader", { name: /Spot price/ }).closest("tr");
    if (!(spotRow instanceof HTMLTableRowElement)) throw new Error("spot row missing");
    cells = within(spotRow).getAllByRole("cell");
    expect(cells[0]).toHaveTextContent(importedSpot ?? "");
    expect(cells[1]).toHaveTextContent("25000.50");
    expect(cells[2]).toHaveTextContent("25000.50");
    expect(screen.getByText("1 override active")).toBeInTheDocument();

    const atmRow = screen.getByRole("rowheader", { name: /ATM strike/ }).closest("tr");
    if (!(atmRow instanceof HTMLTableRowElement)) throw new Error("ATM row missing");
    expect(within(atmRow).getByText("Not applicable")).toBeInTheDocument();

    await user.click(
      within(spotRow).getByRole("button", { name: "Clear override for Spot price" }),
    );
    spotRow = screen.getByRole("rowheader", { name: /Spot price/ }).closest("tr");
    if (!(spotRow instanceof HTMLTableRowElement)) throw new Error("spot row missing");
    cells = within(spotRow).getAllByRole("cell");
    expect(cells[1]).toHaveTextContent("Add override");
    expect(cells[2]).toHaveTextContent(importedSpot ?? "");
    expect(screen.getByText("No overrides")).toBeInTheDocument();
  });

  it("renders exactly five strike rows and ten independently selectable legs", async () => {
    const user = userEvent.setup();
    render(<App />);
    await openNiftyCalculator(user);

    const section = containingSection(
      screen.getByRole("heading", { name: "Five-strike option chain" }),
    );
    expect(section.querySelectorAll("tbody tr")).toHaveLength(5);
    expect(
      within(section).getAllByRole("button", {
        name: /^Select \d+ (CE|PE)$/,
      }),
    ).toHaveLength(10);

    const selected = within(section).getAllByRole("button", {
      name: /^Select \d+ PE$/,
    })[2];
    await user.click(selected);
    expect(selected).toHaveAttribute("aria-pressed", "true");
  });

  it("shows the best and second-best contracts ahead of the full ranking", async () => {
    const user = userEvent.setup();
    const defaultSnapshot = buildDemoSnapshot({
      market: "NIFTY",
      symbol: MARKET_DEFINITIONS.NIFTY.symbols[0],
      expiry: MARKET_DEFINITIONS.NIFTY.expiries[0],
    });
    const best = defaultSnapshot.ranking[0];
    const second = defaultSnapshot.ranking[1];
    render(<App />);

    await user.click(screen.getByRole("button", { name: /Inspect ranking/ }));
    expect(
      screen.getByRole("heading", { name: "Executable strike selection" }),
    ).toBeInTheDocument();

    const bestButton = screen.getByRole("button", { name: /BEST STRIKE/ });
    const secondButton = screen.getByRole("button", { name: /SECOND BEST/ });
    expect(bestButton).toHaveTextContent(formatNumber(best.strike, 0));
    expect(bestButton).toHaveTextContent(best.side);
    expect(secondButton).toHaveTextContent(formatNumber(second.strike, 0));
    expect(secondButton).toHaveTextContent(second.side);

    const rankingSection = containingSection(
      screen.getByRole("heading", {
        name: `${defaultSnapshot.selection.symbol} strike ranking`,
      }),
    );
    expect(rankingSection.querySelectorAll("tbody tr")).toHaveLength(10);
    expect(within(rankingSection).getByText("BEST")).toBeInTheDocument();
    expect(within(rankingSection).getByText("SECOND")).toBeInTheDocument();

    await user.click(secondButton);
    const selectedCard = screen.getByText("INSPECTING").closest("article");
    if (!(selectedCard instanceof HTMLElement)) throw new Error("inspection card missing");
    expect(selectedCard).toHaveTextContent(
      `${formatNumber(second.strike, 0)} ${second.side}`,
    );
  });

  it("preserves the selected option leg when navigating into the Greeks engine", async () => {
    const user = userEvent.setup();
    const defaultSnapshot = buildDemoSnapshot({
      market: "NIFTY",
      symbol: MARKET_DEFINITIONS.NIFTY.symbols[0],
      expiry: MARKET_DEFINITIONS.NIFTY.expiries[0],
    });
    const selectedRow = defaultSnapshot.chain[4];
    render(<App />);
    await openNiftyCalculator(user);

    await user.click(
      screen.getByRole("button", {
        name: `Select ${selectedRow.strike} PE`,
      }),
    );
    await user.click(
      within(navigation()).getByRole("button", { name: "Greeks engine" }),
    );

    expect(
      screen.getByRole("heading", { name: "Risk sensitivity, leg by leg" }),
    ).toBeInTheDocument();
    const selectedContract = screen.getByText("SELECTED CONTRACT").closest("div");
    if (!(selectedContract instanceof HTMLElement)) {
      throw new Error("selected Greeks contract missing");
    }
    expect(selectedContract).toHaveTextContent(formatNumber(selectedRow.strike, 0));
    expect(selectedContract).toHaveTextContent("PE");

    const surface = containingSection(
      screen.getByRole("heading", { name: "Call and put sensitivities" }),
    );
    expect(surface.querySelectorAll("tbody tr")).toHaveLength(10);
    expect(screen.getByText("Theta / day")).toBeInTheDocument();
    expect(screen.getByText("Per IV-point move")).toBeInTheDocument();
  });
});
