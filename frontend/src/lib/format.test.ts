import { formatExpiry } from "./format";

describe("formatExpiry", () => {
  it("formats both date-only and backend ISO expiries in the market timezone", () => {
    const dateOnly = formatExpiry("2026-09-05");
    expect(formatExpiry("2026-09-05T15:30:00+05:30")).toBe(dateOnly);
    expect(dateOnly).toMatch(/05.*Sept.*2026/);
  });

  it("does not crash the workspace on an invalid expiry label", () => {
    expect(formatExpiry("not-a-date")).toBe("Invalid expiry");
  });
});
