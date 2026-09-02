import { render, screen, within } from "@testing-library/react";

import { SettingsView } from "./SettingsView";

const PROFILE_LABELS = [
  "Account capital",
  "Risk per trade",
  "Maximum premium allocation",
  "Event risk",
  "Expected holding hours",
  "Trading style",
  "Operating mode",
  "Price-action confirmation",
] as const;

describe("SettingsView operator decision profile", () => {
  it("fails closed and lists server-owned inputs without values or controls", () => {
    render(<SettingsView marketDataState="CONFIG_REQUIRED" />);

    const profile = screen.getByRole("region", { name: "Operator decision profile" });
    for (const label of PROFILE_LABELS) {
      expect(within(profile).getByText(label)).toBeInTheDocument();
    }
    expect(within(profile).getAllByText("REQUIRED")).toHaveLength(9);
    expect(within(profile).getByText("OPTIONAL")).toBeInTheDocument();
    expect(within(profile).getByText("CONFIG_REQUIRED")).toBeInTheDocument();
    expect(within(profile).getByText("backend/.env")).toBeInTheDocument();
    expect(within(profile).getByText(/restart the backend/i)).toBeInTheDocument();
    expect(within(profile).queryByRole("button")).not.toBeInTheDocument();
    expect(within(profile).queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("shows configured status while preserving the live-execution lock", () => {
    render(<SettingsView decisionInputsConfigured marketDataState="READY" />);

    const profile = screen.getByRole("region", { name: "Operator decision profile" });
    expect(within(profile).getAllByText("CONFIGURED")).toHaveLength(9);
    expect(within(profile).getByText("READY")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /Live automatic/i })).toBeDisabled();
    expect(screen.getByText("LIVE_AUTOMATIC remains locked")).toBeInTheDocument();
  });
});
