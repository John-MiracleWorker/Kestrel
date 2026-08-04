import {
  CircleCheck,
  Info,
  ShieldAlert,
  TriangleAlert,
  type LucideIcon,
} from "lucide-react";
import {
  forwardRef,
  type HTMLAttributes,
  type ReactNode,
} from "react";

export type NoticeVariant = "info" | "success" | "caution" | "danger";

export type NoticeProps = Omit<HTMLAttributes<HTMLDivElement>, "title"> & {
  variant?: NoticeVariant;
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
};

const NOTICE_ICONS: Record<NoticeVariant, LucideIcon> = {
  info: Info,
  success: CircleCheck,
  caution: TriangleAlert,
  danger: ShieldAlert,
};

export const Notice = forwardRef<HTMLDivElement, NoticeProps>(function Notice(
  {
    variant = "info",
    title,
    actions,
    className = "",
    children,
    role,
    ...nativeProps
  },
  ref,
) {
  const Icon = NOTICE_ICONS[variant];

  return (
    <div
      {...nativeProps}
      ref={ref}
      role={role ?? (variant === "danger" ? "alert" : "status")}
      className={`wf-notice wf-notice-${variant} ${className}`.trim()}
    >
      <Icon className="wf-notice-icon" size={18} aria-hidden="true" />
      <div className="wf-notice-content">
        {title ? <strong className="wf-notice-title">{title}</strong> : null}
        <div className="wf-notice-body">{children}</div>
      </div>
      {actions ? <div className="wf-notice-actions">{actions}</div> : null}
    </div>
  );
});
