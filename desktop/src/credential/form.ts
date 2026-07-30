import type {
  CredentialBridge
} from "./preload.js";

interface CredentialElement {
  value: string;
  textContent: string | null;
  disabled: boolean;
  addEventListener(
    event: string,
    listener: (event: { preventDefault(): void }) => void
  ): void;
  removeEventListener(
    event: string,
    listener: (event: { preventDefault(): void }) => void
  ): void;
}

interface CredentialDocument {
  querySelector(selector: string): CredentialElement | null;
}

export interface CredentialFormController {
  readonly ready: Promise<void>;
  idle(): Promise<void>;
  dispose(): void;
}

function requiredElement(
  document: CredentialDocument,
  selector: string
): CredentialElement {
  const element = document.querySelector(selector);
  if (element === null) {
    throw new Error("credential_form_unavailable");
  }
  return element;
}

export function startCredentialForm(options: {
  document: CredentialDocument;
  bridge: CredentialBridge;
}): CredentialFormController {
  const form = requiredElement(
    options.document,
    "#credential-form"
  );
  const input = requiredElement(
    options.document,
    "#credential-value"
  );
  const cancelButton = requiredElement(
    options.document,
    "#credential-cancel"
  );
  const submitButton = requiredElement(
    options.document,
    "#credential-submit"
  );
  const providerLabel = requiredElement(
    options.document,
    "#provider-label"
  );
  const inputLabel = requiredElement(
    options.document,
    "#input-label"
  );
  const status = requiredElement(
    options.document,
    "#credential-status"
  );
  let disposed = false;
  let terminal = false;
  let active: Promise<void> = Promise.resolve();

  const setBusy = (busy: boolean): void => {
    submitButton.disabled = busy;
    cancelButton.disabled = busy;
    input.disabled = busy;
  };

  const ready = options.bridge
    .getContext()
    .then((context) => {
      if (disposed) {
        return;
      }
      providerLabel.textContent = context.providerLabel;
      inputLabel.textContent = context.inputLabel;
      status.textContent = "";
    })
    .catch(() => {
      if (!disposed) {
        terminal = true;
        setBusy(true);
        status.textContent = "Credential entry is unavailable.";
      }
    });
  active = ready;

  const submit = async (
    event: { preventDefault(): void }
  ): Promise<void> => {
    event.preventDefault();
    if (disposed || terminal || submitButton.disabled) {
      return;
    }
    setBusy(true);
    status.textContent = "Storing credential…";
    let value = input.value;
    input.value = "";
    try {
      await options.bridge.submit(value);
      terminal = true;
      status.textContent = "Credential stored.";
    } catch {
      status.textContent = "Credential could not be stored.";
      if (!disposed) {
        setBusy(false);
      }
    } finally {
      value = "";
      input.value = "";
    }
  };

  const onSubmit = (
    event: { preventDefault(): void }
  ): void => {
    active = submit(event);
  };

  const cancel = async (
    event: { preventDefault(): void }
  ): Promise<void> => {
    event.preventDefault();
    if (disposed || terminal) {
      return;
    }
    terminal = true;
    setBusy(true);
    input.value = "";
    try {
      await options.bridge.cancel();
    } catch {
      status.textContent = "Credential dialog could not close.";
    }
  };

  const onCancel = (
    event: { preventDefault(): void }
  ): void => {
    active = cancel(event);
  };

  form.addEventListener("submit", onSubmit);
  cancelButton.addEventListener("click", onCancel);

  return {
    ready,
    idle: () => active,
    dispose: () => {
      if (disposed) {
        return;
      }
      disposed = true;
      input.value = "";
      form.removeEventListener("submit", onSubmit);
      cancelButton.removeEventListener("click", onCancel);
    }
  };
}

declare global {
  var kestrelCredential: CredentialBridge | undefined;
}

if (
  typeof document !== "undefined" &&
  globalThis.kestrelCredential !== undefined
) {
  startCredentialForm({
    document,
    bridge: globalThis.kestrelCredential
  });
}
