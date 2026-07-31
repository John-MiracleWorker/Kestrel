import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";
import { createPortal } from "react-dom";
import { Search } from "lucide-react";
import { Button } from "../design/Button";
import {
  DESTINATIONS,
  type AppDestination,
} from "./destinations";

export type CommandBarProps = {
  onNavigate: (destination: AppDestination) => void;
  onOpenChange?: (open: boolean) => void;
};

export function CommandBar({
  onNavigate,
  onOpenChange,
}: CommandBarProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const triggerRef = useRef<HTMLButtonElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const restoreFocusAfterClose = useRef(false);
  const paletteOpenRef = useRef(false);
  const headingId = `${useId()}-heading`;
  const matches = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    if (!normalized) return DESTINATIONS;
    return DESTINATIONS.filter((destination) =>
      `${destination.label} ${destination.id} ${destination.defaultSubroute}`
        .toLocaleLowerCase()
        .includes(normalized),
    );
  }, [query]);

  useEffect(() => {
    const openFromShortcut = (event: KeyboardEvent) => {
      if (
        event.key.toLocaleLowerCase() === "k" &&
        (event.ctrlKey || event.metaKey)
      ) {
        event.preventDefault();
        if (paletteOpenRef.current) {
          inputRef.current?.focus();
          return;
        }
        returnFocusRef.current = activeElementOrTrigger(triggerRef.current);
        restoreFocusAfterClose.current = false;
        paletteOpenRef.current = true;
        setOpen(true);
        onOpenChange?.(true);
      }
    };
    window.addEventListener("keydown", openFromShortcut);
    return () => window.removeEventListener("keydown", openFromShortcut);
  }, [onOpenChange]);

  useEffect(() => {
    if (open) {
      inputRef.current?.focus();
    } else if (restoreFocusAfterClose.current) {
      restoreFocusAfterClose.current = false;
      const returnTarget = returnFocusRef.current ?? triggerRef.current;
      returnFocusRef.current = null;
      returnTarget?.focus();
    }
  }, [open]);

  const closeAndRestoreFocus = () => {
    restoreFocusAfterClose.current = true;
    paletteOpenRef.current = false;
    setOpen(false);
    setQuery("");
    onOpenChange?.(false);
  };

  return (
    <header className="workbench-command-bar">
      <div>
        <p>Mission Command</p>
        <span>Ask, inspect, tune, and approve from one workbench.</span>
      </div>
      <Button
        ref={triggerRef}
        className="workbench-command-trigger"
        variant="secondary"
        size="small"
        aria-label="Open command palette"
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => {
          returnFocusRef.current = triggerRef.current;
          restoreFocusAfterClose.current = false;
          paletteOpenRef.current = true;
          setOpen(true);
          onOpenChange?.(true);
        }}
      >
        <Search size={16} aria-hidden="true" />
        <span>Search Kestrel</span>
        <kbd>⌘K / Ctrl K</kbd>
      </Button>
      {open && typeof document !== "undefined"
        ? createPortal(
            <div
              className="workbench-command-palette-layer"
              onMouseDown={(event) => {
                if (event.target === event.currentTarget) {
                  closeAndRestoreFocus();
                }
              }}
            >
              <div
                className="workbench-command-palette"
                role="dialog"
                aria-modal="true"
                aria-labelledby={headingId}
                onKeyDown={(event) => {
                  if (event.key === "Escape") {
                    event.preventDefault();
                    closeAndRestoreFocus();
                  } else if (event.key === "Tab") {
                    keepPaletteFocus(event);
                  }
                }}
              >
                <div className="workbench-command-palette-heading">
                  <Search size={19} aria-hidden="true" />
                  <div>
                    <h2 id={headingId}>Open a Kestrel destination</h2>
                    <p>
                      Navigation only. Commands never execute tools or
                      approvals.
                    </p>
                  </div>
                </div>
                <label className="workbench-command-search">
                  <span className="sr-only">Search destinations</span>
                  <input
                    ref={inputRef}
                    type="search"
                    aria-label="Search destinations"
                    value={query}
                    placeholder="Mission, Flock, settings…"
                    onChange={(event) => setQuery(event.target.value)}
                  />
                </label>
                <div
                  className="workbench-command-results"
                  aria-label="Destination results"
                >
                  {matches.length ? (
                    matches.map((destination) => {
                      const Icon = destination.icon;
                      return (
                        <button
                          type="button"
                          aria-label={destination.label}
                          key={destination.id}
                          onClick={() => {
                            returnFocusRef.current = triggerRef.current;
                            restoreFocusAfterClose.current = true;
                            paletteOpenRef.current = false;
                            setOpen(false);
                            setQuery("");
                            onOpenChange?.(false);
                            onNavigate(destination.id);
                          }}
                        >
                          <Icon size={18} aria-hidden="true" />
                          <span>
                            <strong>{destination.label}</strong>
                            <small>
                              {destination.id} ·{" "}
                              {destination.defaultSubroute}
                            </small>
                          </span>
                        </button>
                      );
                    })
                  ) : (
                    <p role="status">No matching destination.</p>
                  )}
                </div>
                <p className="workbench-command-hint">
                  <kbd>Esc</kbd> closes · opening a destination never starts a
                  run
                </p>
              </div>
            </div>,
            document.body,
          )
        : null}
    </header>
  );
}

function activeElementOrTrigger(
  trigger: HTMLButtonElement | null,
): HTMLElement | null {
  const active = document.activeElement;
  return active instanceof HTMLElement && active !== document.body
    ? active
    : trigger;
}

function keepPaletteFocus(event: ReactKeyboardEvent<HTMLDivElement>) {
  const focusable = Array.from(
    event.currentTarget.querySelectorAll<HTMLElement>(
      'input:not([disabled]), button:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ),
  );
  const first = focusable[0];
  const last = focusable.at(-1);
  if (!first || !last) return;
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}
