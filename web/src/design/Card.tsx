import {
  forwardRef,
  useId,
  type HTMLAttributes,
  type ReactNode,
} from "react";

export type CardProps = Omit<HTMLAttributes<HTMLElement>, "title"> & {
  title: ReactNode;
  icon?: ReactNode;
  actions?: ReactNode;
  headingLevel?: 2 | 3 | 4;
  labelled?: boolean;
  children: ReactNode;
};

export const Card = forwardRef<HTMLElement, CardProps>(function Card(
  {
    title,
    icon,
    actions,
    headingLevel = 2,
    labelled = true,
    className = "",
    children,
    id,
    "aria-labelledby": ariaLabelledBy,
    ...nativeProps
  },
  ref,
) {
  const generatedId = useId();
  const headingId = id ? `${id}-title` : `${generatedId}-title`;
  const Heading = `h${headingLevel}` as "h2" | "h3" | "h4";

  return (
    <section
      {...nativeProps}
      ref={ref}
      id={id}
      className={`wf-card ${className}`.trim()}
      aria-labelledby={
        ariaLabelledBy ?? (labelled ? headingId : undefined)
      }
    >
      <header className="wf-card-header panel-head">
        <Heading id={headingId} className="wf-card-title">
          {icon ? (
            <span className="wf-card-icon" aria-hidden="true">
              {icon}
            </span>
          ) : null}
          {title}
        </Heading>
        {actions ? (
          <div className="wf-card-actions panel-actions">{actions}</div>
        ) : null}
      </header>
      <div className="wf-card-body">{children}</div>
    </section>
  );
});
