import {
  forwardRef,
  type HTMLAttributes,
  type ReactNode,
} from "react";

export type EmptyStateProps = Omit<HTMLAttributes<HTMLDivElement>, "title"> & {
  title?: ReactNode;
  icon?: ReactNode;
  actions?: ReactNode;
  headingLevel?: 2 | 3 | 4;
  children: ReactNode;
};

export const EmptyState = forwardRef<HTMLDivElement, EmptyStateProps>(
  function EmptyState(
    {
      title,
      icon,
      actions,
      headingLevel = 2,
      className = "",
      children,
      ...nativeProps
    },
    ref,
  ) {
    const Heading = `h${headingLevel}` as "h2" | "h3" | "h4";

    return (
      <div
        {...nativeProps}
        ref={ref}
        className={`wf-empty-state empty-state ${className}`.trim()}
      >
        {icon ? (
          <span className="wf-empty-state-icon" aria-hidden="true">
            {icon}
          </span>
        ) : null}
        {title ? (
          <Heading className="wf-empty-state-title">{title}</Heading>
        ) : null}
        <div className="wf-empty-state-body">{children}</div>
        {actions ? (
          <div className="wf-empty-state-actions">{actions}</div>
        ) : null}
      </div>
    );
  },
);
