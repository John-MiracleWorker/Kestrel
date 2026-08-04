import "@testing-library/jest-dom/vitest";
import {
  cleanup,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Approval, Run, RuntimeConfig, Session } from "../types";
import {
  type ApplicationDataLoader,
  useApplicationData,
} from "./useApplicationData";

function Probe({ load }: { load: ApplicationDataLoader }) {
  const snapshot = useApplicationData({ load });
  return (
    <>
      <output aria-label="runtime">{snapshot.runtime.status}</output>
      <output aria-label="mission">{snapshot.mission.status}</output>
      <output aria-label="setup">{snapshot.setup.status}</output>
      <output aria-label="run count">
        {snapshot.mission.data?.runs.length ?? 0}
      </output>
    </>
  );
}

describe("useApplicationData", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("keeps Mission usable when an optional startup slice fails", async () => {
    const load: ApplicationDataLoader = async (path) => {
      if (path === "/api/runtime/config") {
        return { name: "Kestrel" } as RuntimeConfig;
      }
      if (path === "/api/runs") return [{ run_id: "run_1" }] as Run[];
      if (path === "/api/sessions") return [] as Session[];
      if (path === "/api/approvals?status=pending") {
        return [] as Approval[];
      }
      if (path === "/api/product/setup") {
        throw new Error("setup probe unavailable");
      }
      throw new Error(`unexpected:${path}`);
    };

    render(<Probe load={load} />);

    await waitFor(() =>
      expect(screen.getByLabelText("runtime")).toHaveTextContent("ready"),
    );
    expect(screen.getByLabelText("mission")).toHaveTextContent("ready");
    expect(screen.getByLabelText("run count")).toHaveTextContent("1");
    expect(screen.getByLabelText("setup")).toHaveTextContent("error");
  });

  it("aborts every in-flight slice when its owner unmounts", () => {
    const signals: AbortSignal[] = [];
    const load: ApplicationDataLoader = (_path, options) => {
      signals.push(options.signal);
      return new Promise(() => undefined);
    };

    const { unmount } = render(<Probe load={load} />);
    expect(signals).toHaveLength(5);
    unmount();
    expect(signals.every((signal) => signal.aborted)).toBe(true);
  });
});
