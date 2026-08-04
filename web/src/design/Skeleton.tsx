import { forwardRef, type HTMLAttributes } from "react";

export type SkeletonProps = HTMLAttributes<HTMLDivElement> & {
  label?: string;
  lines?: number;
};

export const Skeleton = forwardRef<HTMLDivElement, SkeletonProps>(
  function Skeleton(
    { label, lines = 1, className = "", ...nativeProps },
    ref,
  ) {
    const lineCount = Math.min(8, Math.max(1, Math.floor(lines)));

    return (
      <div
        {...nativeProps}
        ref={ref}
        className={`wf-skeleton ${className}`.trim()}
        role={label ? "status" : undefined}
        aria-label={label}
        aria-hidden={label ? undefined : true}
      >
        {Array.from({ length: lineCount }, (_, index) => (
          <span
            className="wf-skeleton-line"
            key={index}
            style={{ "--wf-skeleton-line": index } as React.CSSProperties}
          />
        ))}
      </div>
    );
  },
);
