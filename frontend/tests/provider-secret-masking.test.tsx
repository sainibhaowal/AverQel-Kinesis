import { render, screen } from "@testing-library/react";

import ProviderSecretField from "../app/components/providers/ProviderSecretField";

describe("provider secret masking", () => {
  it("shows only masked saved secret summaries", () => {
    render(<ProviderSecretField value="" onChange={() => {}} maskedSummary="sk-...abcd" />);

    const secretInput = screen.getByDisplayValue("");
    expect(secretInput).toBeInTheDocument();
    expect(secretInput).toHaveClass(
      "provider-api-key-input",
      "border-slate-400",
      "dark:border-slate-500",
      "bg-white",
    );
    expect(screen.getByText(/saved secret: sk-\.\.\.abcd/i)).toBeInTheDocument();
    expect(screen.queryByDisplayValue("sk-live-raw-secret")).not.toBeInTheDocument();
  });
});
