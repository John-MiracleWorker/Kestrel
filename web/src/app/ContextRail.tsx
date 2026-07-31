import {
  useEffect,
  useId,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { PanelRightClose, PanelRightOpen } from "lucide-react";
import { Button } from "../design/Button";

export type ContextRailProps = {
  label: string;
  defaultOpen?: boolean;
  onOpenChange?: (open: boolean) => void;
  children: ReactNode;
};

export function ContextRail({
  label,
  defaultOpen = true,
  onOpenChange,
  children,
}: ContextRailProps) {
  const [open, setOpen] = useState(defaultOpen);
  const railId = `${useId()}-context`;
  const toggleRef = useRef<HTMLButtonElement>(null);
  const railRef = useRef<HTMLElement>(null);
  const focusRailOnOpen = useRef(false);
  const narrowViewport = useRef(
    typeof window !== "undefined" && window.innerWidth <= 1000,
  );
  const controlLabel = label.toLocaleLowerCase();

  useEffect(() => {
    if (open && focusRailOnOpen.current) {
      focusRailOnOpen.current = false;
      railRef.current?.focus();
    }
  }, [open]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const synchronizeViewport = () => {
      const nextNarrow = window.innerWidth <= 1000;
      if (nextNarrow === narrowViewport.current) return;
      narrowViewport.current = nextNarrow;
      focusRailOnOpen.current = false;
      if (
        nextNarrow &&
        railRef.current?.contains(document.activeElement)
      ) {
        toggleRef.current?.focus();
      }
      setOpen(!nextNarrow);
      onOpenChange?.(!nextNarrow);
    };
    window.addEventListener("resize", synchronizeViewport);
    return () => window.removeEventListener("resize", synchronizeViewport);
  }, [onOpenChange]);

  const updateOpen = (next: boolean, focusRail = false) => {
    focusRailOnOpen.current = focusRail && next;
    setOpen(next);
    onOpenChange?.(next);
  };

  const closeAndRestoreFocus = () => {
    focusRailOnOpen.current = false;
    setOpen(false);
    onOpenChange?.(false);
    toggleRef.current?.focus();
  };

  return (
    <div
      className="workbench-context-dock"
      data-open={open ? "true" : "false"}
    >
      <Button
        ref={toggleRef}
        className="workbench-context-toggle"
        variant="quiet"
        size="small"
        aria-controls={railId}
        aria-expanded={open}
        aria-label={`${open ? "Hide" : "Show"} ${controlLabel}`}
        title={`${open ? "Hide" : "Show"} ${controlLabel}`}
        onClick={() => {
          if (open) {
            closeAndRestoreFocus();
          } else {
            updateOpen(true, true);
          }
        }}
      >
        {open ? (
          <PanelRightClose size={17} aria-hidden="true" />
        ) : (
          <PanelRightOpen size={17} aria-hidden="true" />
        )}
        <span className="workbench-context-toggle-label">
          {open ? "Hide context" : "Show context"}
        </span>
      </Button>
      <aside
        ref={railRef}
        id={railId}
        className="workbench-context-rail"
        aria-label={label}
        tabIndex={open ? 0 : -1}
        hidden={!open}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.preventDefault();
            closeAndRestoreFocus();
          }
        }}
      >
        {children}
      </aside>
    </div>
  );
}
