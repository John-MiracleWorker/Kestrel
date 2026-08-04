import "@testing-library/jest-dom/vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ConversationPanel } from "./ConversationPanel";

describe("ConversationPanel", () => {
  afterEach(cleanup);

  it("owns composer state and submission without nesting an application shell", async () => {
    const onSubmitMessage = vi.fn(async () => undefined);
    render(
      <ConversationPanel
        agentDisplayName="Kestrel"
        hasActiveThread={false}
        chatStatusDetail="Ready when you are."
        status={{ label: "Ready", detail: "Ready when you are." }}
        activeRun={null}
        runs={[]}
        events={[]}
        streamedAssistant=""
        approvals={[]}
        autonomyMode="background"
        autonomyOptions={[{ value: "background", label: "Safe Auto" }]}
        autonomousSchedulerEnabled={false}
        notice={null}
        error={null}
        onAutonomyModeChange={() => undefined}
        onOpenSetup={() => undefined}
        onOpenSettings={() => undefined}
        onRefresh={async () => undefined}
        onSubmitMessage={onSubmitMessage}
        onError={() => undefined}
        onDismissError={() => undefined}
        onDecideApproval={() => undefined}
        onContainer={() => undefined}
        renderInspector={() => null}
      />,
    );

    const composer = screen.getByLabelText("Ask Kestrel");
    fireEvent.change(composer, { target: { value: "Inspect this project" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => {
      expect(onSubmitMessage).toHaveBeenCalledWith("Inspect this project");
    });
    expect(composer).toHaveValue("");
    expect(screen.queryByRole("main")).not.toBeInTheDocument();
  });
});
