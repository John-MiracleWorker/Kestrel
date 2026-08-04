import type { SetupPresentationState } from "./types";

const PRESENTATION_KEY = "kestrel.setup.presentation.v1";

const emptyPresentation: SetupPresentationState = {
  seen: false,
};

export function readSetupPresentation(): SetupPresentationState {
  try {
    const storage = globalThis.localStorage;
    const raw = storage.getItem(PRESENTATION_KEY);
    if (!raw) return emptyPresentation;
    const parsed = JSON.parse(raw) as unknown;
    if (typeof parsed !== "object" || parsed === null) {
      return emptyPresentation;
    }
    const record = parsed as Record<string, unknown>;
    return {
      seen: record.seen === true,
    };
  } catch {
    return emptyPresentation;
  }
}

export function updateSetupPresentation(
  patch: Partial<SetupPresentationState>,
): SetupPresentationState {
  const next = {
    ...readSetupPresentation(),
    ...patch,
  };
  try {
    globalThis.localStorage.setItem(
      PRESENTATION_KEY,
      JSON.stringify(next),
    );
  } catch {
    // Presentation hints must never gate setup or startup.
  }
  return next;
}

export function hasVisitedSetupCenter(): boolean {
  return readSetupPresentation().seen;
}
