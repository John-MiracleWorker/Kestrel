import {
  forwardRef,
  useId,
  useState,
  type ButtonHTMLAttributes,
  type ReactNode,
} from "react";
import { ChevronDown } from "lucide-react";

export type DisclosureProps = Omit<
  ButtonHTMLAttributes<HTMLButtonElement>,
  "children" | "title"
> & {
  title: ReactNode;
  children: ReactNode;
  defaultOpen?: boolean;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  contentClassName?: string;
};

export const Disclosure = forwardRef<HTMLButtonElement, DisclosureProps>(
  function Disclosure(
    {
      title,
      children,
      defaultOpen = false,
      open,
      onOpenChange,
      contentClassName = "",
      className = "",
      onClick,
      type = "button",
      ...nativeProps
    },
    ref,
  ) {
    const [internalOpen, setInternalOpen] = useState(defaultOpen);
    const contentId = `${useId()}-content`;
    const expanded = open ?? internalOpen;

    return (
      <div className="wf-disclosure">
        <button
          {...nativeProps}
          ref={ref}
          type={type}
          className={`wf-disclosure-trigger ${className}`.trim()}
          aria-expanded={expanded}
          aria-controls={contentId}
          onClick={(event) => {
            onClick?.(event);
            if (event.defaultPrevented) return;
            const next = !expanded;
            if (open === undefined) {
              setInternalOpen(next);
            }
            onOpenChange?.(next);
          }}
        >
          <span>{title}</span>
          <ChevronDown
            className="wf-disclosure-chevron"
            size={17}
            aria-hidden="true"
          />
        </button>
        <div
          id={contentId}
          className={`wf-disclosure-content ${contentClassName}`.trim()}
          hidden={!expanded}
        >
          {children}
        </div>
      </div>
    );
  },
);
