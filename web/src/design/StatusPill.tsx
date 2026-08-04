import {
  CircleMinus,
  CircleCheck,
  Clock3,
  ShieldAlert,
  TriangleAlert,
  type LucideIcon,
} from "lucide-react";
import {
  forwardRef,
  type HTMLAttributes,
  type ReactNode,
} from "react";

export type StatusState =
  | "healthy"
  | "blocked"
  | "waiting"
  | "caution"
  | "inactive";

export type StatusPillProps = HTMLAttributes<HTMLSpanElement> & {
  state: StatusState;
  iconLabel?: string;
  children: ReactNode;
};

const STATUS_META: Record<
  StatusState,
  {
    label: string;
    icon: LucideIcon;
  }
> = {
  healthy: { label: "Healthy", icon: CircleCheck },
  blocked: { label: "Blocked", icon: ShieldAlert },
  waiting: { label: "Waiting", icon: Clock3 },
  caution: { label: "Caution", icon: TriangleAlert },
  inactive: { label: "Inactive", icon: CircleMinus },
};

export const StatusPill = forwardRef<HTMLSpanElement, StatusPillProps>(
  function StatusPill(
    { state, iconLabel, className = "", children, ...nativeProps },
    ref,
  ) {
    const { label, icon: Icon } = STATUS_META[state];

    return (
      <span
        {...nativeProps}
        ref={ref}
        className={[
          "wf-status-pill",
          `wf-status-${state}`,
          className,
        ]
          .filter(Boolean)
          .join(" ")}
        data-state={state}
      >
        <Icon
          className="wf-status-icon"
          size={14}
          role="img"
          aria-label={iconLabel ?? label}
          data-testid="status-icon"
        />
        <span>{children}</span>
      </span>
    );
  },
);
