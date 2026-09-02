import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import App from "./App";

function navigation(): HTMLElement {
  return screen.getByRole("complementary", { name: "Primary navigation" });
}

async function openView(user: ReturnType<typeof userEvent.setup>, name: string): Promise<void> {
  await user.click(within(navigation()).getByRole("button", { name }));
}

describe("Sprint 4 integrated workflows", () => {
  it("keeps the operational decision fail-closed on demo data", () => {
    render(<App />);
    const gate = screen.getByRole("complementary", { name: "Operational decision gate" });
    expect(gate).toHaveTextContent("NO TRADE");
    expect(gate).toHaveTextContent("Demo data cannot generate an operational signal");
    expect(screen.getByText("DEMO ANALYTICAL DECISION")).toBeInTheDocument();
  });

  it("makes no backend request when the API base URL is not configured", async () => {
    vi.stubEnv("VITE_API_BASE_URL", "");
    try {
      const fetchSpy = vi.spyOn(globalThis, "fetch");
      const user = userEvent.setup();
      render(<App />);

      await openView(user, "API status");
      expect(screen.getByRole("heading", { name: "API and data status" })).toBeInTheDocument();
      expect(screen.getByRole("status")).toHaveTextContent("Not configured");
      expect(screen.getByText(/No network request has been made/)).toBeInTheDocument();
      expect(fetchSpy).not.toHaveBeenCalled();
    } finally {
      vi.unstubAllEnvs();
    }
  });

  it("calculates whole-lot affordability and shows the mandated zero-lot state", async () => {
    const user = userEvent.setup();
    render(<App />);
    await openView(user, "Position sizer");

    expect(screen.getByRole("heading", { name: "Position sizing and trade plan" })).toBeInTheDocument();
    expect(screen.getByText("2% hard cap")).toBeInTheDocument();
    expect(screen.getByText("Plan locked")).toBeInTheDocument();

    const capital = screen.getByLabelText("Account capital");
    await user.clear(capital);
    await user.type(capital, "100");
    expect(
      screen.getByText("TRADE NOT AFFORDABLE WITH CURRENT RISK LIMIT"),
    ).toBeInTheDocument();
    expect(screen.getAllByText(/0 lots:/).length).toBeGreaterThanOrEqual(1);
  });

  it("renders every operational record and reference view", async () => {
    const user = userEvent.setup();
    render(<App />);
    const destinations = [
      ["Signal history", "Signal history"],
      ["Trade journal", "Trade journal"],
      ["Backtest report", "Backtest report"],
      ["Contract master", "Contract master"],
      ["User guide", "User guide"],
      ["Formula audit", "Formula audit"],
    ] as const;

    for (const [buttonName, headingName] of destinations) {
      await openView(user, buttonName);
      expect(screen.getByRole("heading", { name: headingName })).toBeInTheDocument();
    }
  });

  it("shows execution modes without allowing live activation or exposing a browser secret", async () => {
    const user = userEvent.setup();
    render(<App />);
    await openView(user, "Settings");

    expect(screen.getByRole("heading", { name: "Settings and guardrails" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /Live automatic/i })).toBeDisabled();
    expect(screen.getByText("LIVE_AUTOMATIC remains locked")).toBeInTheDocument();
    expect(
      screen.queryByRole("textbox", { name: /api key|token|secret/i }),
    ).not.toBeInTheDocument();
  });
});
