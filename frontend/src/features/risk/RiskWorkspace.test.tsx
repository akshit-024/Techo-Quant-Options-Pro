import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { MARKET_DEFINITIONS } from "../../data/marketDefinitions";
import { buildDemoSnapshot } from "../../data/demoSnapshot";
import {
  buildRiskTradePlan,
  rankedLegKey,
  snapshotRiskDefaults,
} from "../../domain/risk";
import type { MarketSnapshot } from "../../domain/types";
import { RiskPlanSummary } from "./RiskPlanSummary";
import { RiskWorkspace } from "./RiskWorkspace";

function snapshot(): MarketSnapshot {
  return buildDemoSnapshot({
    market: "NIFTY",
    symbol: MARKET_DEFINITIONS.NIFTY.symbols[0],
    expiry: MARKET_DEFINITIONS.NIFTY.expiries[0],
  });
}

describe("RiskWorkspace", () => {
  it("renders a charge-aware ask-based plan with no execution control", () => {
    const demo = snapshot();
    render(
      <RiskWorkspace
        onLegSelect={() => undefined}
        selectedLegKey={rankedLegKey(demo.ranking[0])}
        snapshot={demo}
      />,
    );

    expect(
      screen.getByRole("heading", { name: "Position sizing and trade plan" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Position sizer" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Trade plan" })).toBeInTheDocument();
    expect(screen.getByText("2% hard cap")).toBeInTheDocument();
    expect(screen.getByText("Executable entry reference")).toBeInTheDocument();
    expect(screen.getByText("Estimated charges")).toBeInTheDocument();
    expect(screen.getByText("Total risk incl. charges")).toBeInTheDocument();
    expect(screen.getByText("Plan locked")).toBeInTheDocument();
    expect(screen.getByText(/current data mode is DEMO/)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /place|execute|submit|buy|sell/i }),
    ).not.toBeInTheDocument();
  });

  it("validates the risk cap and stop interactively", async () => {
    const user = userEvent.setup();
    const demo = snapshot();
    render(
      <RiskWorkspace
        onLegSelect={() => undefined}
        selectedLegKey={rankedLegKey(demo.ranking[0])}
        snapshot={demo}
      />,
    );

    const risk = screen.getByLabelText(/^Risk per trade/);
    await user.clear(risk);
    await user.type(risk, "2.01");
    expect(
      within(screen.getByRole("alert")).getByText(
        "Risk per trade cannot exceed the 2.00% hard cap.",
      ),
    ).toBeInTheDocument();

    await user.clear(risk);
    await user.type(risk, "1");
    const stop = screen.getByLabelText(/^Long-option stop/);
    const entryText = demo.ranking[0].askEntry.toString();
    await user.clear(stop);
    await user.type(stop, entryText);
    expect(
      within(screen.getByRole("alert")).getByText(
        "A long-option stop must be below the ask entry price.",
      ),
    ).toBeInTheDocument();
  });

  it("shows the exact affordability explanation when no complete lot fits", async () => {
    const user = userEvent.setup();
    const demo = snapshot();
    render(
      <RiskWorkspace
        onLegSelect={() => undefined}
        selectedLegKey={rankedLegKey(demo.ranking[0])}
        snapshot={demo}
      />,
    );

    const capital = screen.getByLabelText("Account capital");
    await user.clear(capital);
    await user.type(capital, "100");

    const affordabilityTitle = screen.getByText(
      "TRADE NOT AFFORDABLE WITH CURRENT RISK LIMIT",
    );
    const affordability = affordabilityTitle.closest("div");
    if (!(affordability instanceof HTMLElement)) {
      throw new Error("affordability explanation missing");
    }
    const explanation = within(affordability).getByText(/^0 lots:/);
    expect(explanation).toHaveTextContent("one lot requires INR");
    expect(explanation).toHaveTextContent("available INR");
  });

  it("notifies the parent when the ranked contract selection changes", async () => {
    const user = userEvent.setup();
    const demo = snapshot();
    const onLegSelect = vi.fn();
    const secondKey = rankedLegKey(demo.ranking[1]);
    render(
      <RiskWorkspace
        onLegSelect={onLegSelect}
        selectedLegKey={rankedLegKey(demo.ranking[0])}
        snapshot={demo}
      />,
    );

    await user.selectOptions(screen.getByLabelText("Ranked contract"), secondKey);
    expect(onLegSelect).toHaveBeenCalledWith(secondKey);
  });

  it("shows 1R, 2R, and 3R targets when all analytical gates are clear", () => {
    const demo = snapshot();
    const selected = demo.ranking[0];
    const live: MarketSnapshot = {
      ...demo,
      dataMode: "LIVE",
      analytics: {
        ...demo.analytics,
        decision: selected.side === "CE" ? "BUY CALL" : "BUY PUT",
      },
    };
    render(
      <RiskWorkspace
        onLegSelect={() => undefined}
        selectedLegKey={rankedLegKey(selected)}
        snapshot={live}
      />,
    );

    expect(screen.getByText("Actionable analytical plan")).toBeInTheDocument();
    expect(screen.getByText("1R target")).toBeInTheDocument();
    expect(screen.getByText("2R target")).toBeInTheDocument();
    expect(screen.getByText("3R target")).toBeInTheDocument();
    expect(
      screen.getByText(/Execution remains outside this interface/),
    ).toBeInTheDocument();
  });

  it("renders a compact read-only summary from the same evaluated plan", () => {
    const demo = snapshot();
    const selectedKey = rankedLegKey(demo.ranking[0]);
    const evaluation = buildRiskTradePlan(
      demo,
      selectedKey,
      snapshotRiskDefaults(demo, selectedKey),
    );

    render(<RiskPlanSummary evaluation={evaluation} />);

    const summary = screen.getByRole("region", { name: "Risk plan summary" });
    expect(within(summary).getByText("Locked")).toBeInTheDocument();
    expect(within(summary).getByText("Ask entry")).toBeInTheDocument();
    expect(within(summary).getByText("Size")).toBeInTheDocument();
    expect(within(summary).getByText("Max loss")).toBeInTheDocument();
    expect(within(summary).getByText("1R target")).toBeInTheDocument();
    expect(within(summary).getByText(/current data mode is DEMO/)).toBeInTheDocument();
  });
});
