import { describe, expect, it, vi } from "vitest";
import { startCredentialForm } from "./form";

class FakeElement {
  value = "";
  textContent = "";
  disabled = false;
  readonly listeners = new Map<
    string,
    (event: { preventDefault(): void }) => void
  >();

  addEventListener(
    event: string,
    listener: (event: { preventDefault(): void }) => void
  ): void {
    this.listeners.set(event, listener);
  }

  removeEventListener(event: string): void {
    this.listeners.delete(event);
  }

  emit(event: string): void {
    this.listeners.get(event)?.({
      preventDefault: vi.fn()
    });
  }
}

function documentHarness(): {
  document: {
    querySelector(selector: string): FakeElement | null;
  };
  elements: Record<string, FakeElement>;
} {
  const elements = Object.fromEntries(
    [
      "#credential-form",
      "#credential-value",
      "#credential-cancel",
      "#credential-submit",
      "#provider-label",
      "#input-label",
      "#credential-status"
    ].map((selector) => [selector, new FakeElement()])
  );
  return {
    document: {
      querySelector(selector) {
        return elements[selector] ?? null;
      }
    },
    elements
  };
}

describe("credential form renderer", () => {
  it("renders only metadata, submits through the credential bridge, and clears raw input", async () => {
    const { document, elements } = documentHarness();
    const submitted: string[] = [];
    const bridge = Object.freeze({
      getContext: vi.fn(async () => ({
        schema: "kestrel.credential.context.v1" as const,
        providerId: "openai" as const,
        providerLabel: "OpenAI",
        inputLabel: "OpenAI API key",
        maxUtf8Bytes: 16_384 as const
      })),
      submit: vi.fn(async (value: string) => {
        submitted.push(value);
        return { status: "stored" as const };
      }),
      cancel: vi.fn(async () => ({
        status: "cancelled" as const
      }))
    });
    const controller = startCredentialForm({
      document,
      bridge
    });
    await controller.ready;

    expect(elements["#provider-label"]?.textContent).toBe(
      "OpenAI"
    );
    expect(elements["#input-label"]?.textContent).toBe(
      "OpenAI API key"
    );
    const input = elements["#credential-value"]!;
    input.value = "form-private-sentinel";
    elements["#credential-form"]?.emit("submit");
    await controller.idle();

    expect(submitted).toEqual(["form-private-sentinel"]);
    expect(input.value).toBe("");
    expect(
      elements["#credential-status"]?.textContent
    ).toBe("Credential stored.");
    expect(
      Object.values(elements)
        .map((element) => element.textContent)
        .join(" ")
    ).not.toContain("form-private-sentinel");
  });

  it("clears input and shows only a fixed error when submission fails", async () => {
    const { document, elements } = documentHarness();
    const consoleError = vi
      .spyOn(console, "error")
      .mockImplementation(() => undefined);
    const bridge = Object.freeze({
      getContext: async () => ({
        schema: "kestrel.credential.context.v1" as const,
        providerId: "openai" as const,
        providerLabel: "OpenAI",
        inputLabel: "OpenAI API key",
        maxUtf8Bytes: 16_384 as const
      }),
      submit: async () => {
        throw new Error("form-error-private-sentinel");
      },
      cancel: async () => ({
        status: "cancelled" as const
      })
    });
    const controller = startCredentialForm({
      document,
      bridge
    });
    await controller.ready;
    elements["#credential-value"]!.value =
      "form-input-private-sentinel";

    elements["#credential-form"]?.emit("submit");
    await controller.idle();

    expect(elements["#credential-value"]?.value).toBe("");
    expect(
      elements["#credential-status"]?.textContent
    ).toBe("Credential could not be stored.");
    expect(document).not.toHaveProperty("localStorage");
    expect(document).not.toHaveProperty("sessionStorage");
    expect(consoleError).not.toHaveBeenCalled();
  });

  it("cancels through the bridge once and disposes listeners idempotently", async () => {
    const { document, elements } = documentHarness();
    const cancel = vi.fn(async () => ({
      status: "cancelled" as const
    }));
    const controller = startCredentialForm({
      document,
      bridge: Object.freeze({
        getContext: async () => ({
          schema: "kestrel.credential.context.v1" as const,
          providerId: "openai" as const,
          providerLabel: "OpenAI",
          inputLabel: "OpenAI API key",
          maxUtf8Bytes: 16_384 as const
        }),
        submit: async () => ({ status: "stored" as const }),
        cancel
      })
    });
    await controller.ready;

    elements["#credential-cancel"]?.emit("click");
    await controller.idle();
    controller.dispose();
    controller.dispose();
    elements["#credential-cancel"]?.emit("click");

    expect(cancel).toHaveBeenCalledOnce();
  });
});
