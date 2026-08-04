import {
  cloneElement,
  forwardRef,
  useId,
  type LabelHTMLAttributes,
  type ReactElement,
  type ReactNode,
} from "react";

type FieldControlProps = {
  id?: string;
  className?: string;
  "aria-describedby"?: string;
  "aria-labelledby"?: string;
  "aria-invalid"?: boolean | "true" | "false";
};

export type FieldProps = Omit<
  LabelHTMLAttributes<HTMLLabelElement>,
  "children"
> & {
  label: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
  children: ReactElement<FieldControlProps>;
};

export const Field = forwardRef<HTMLLabelElement, FieldProps>(function Field(
  {
    label,
    hint,
    error,
    className = "",
    children,
    ...nativeProps
  },
  ref,
) {
  const generatedId = useId();
  const controlId = children.props.id ?? `${generatedId}-control`;
  const labelId = `${generatedId}-label`;
  const hintId = hint ? `${generatedId}-hint` : undefined;
  const errorId = error ? `${generatedId}-error` : undefined;
  const describedBy = [
    children.props["aria-describedby"],
    hintId,
    errorId,
  ]
    .filter(Boolean)
    .join(" ");
  const control = cloneElement(children, {
    id: controlId,
    className: ["wf-field-control", children.props.className]
      .filter(Boolean)
      .join(" "),
    "aria-describedby": describedBy || undefined,
    "aria-labelledby": children.props["aria-labelledby"] ?? labelId,
    "aria-invalid": error ? true : children.props["aria-invalid"],
  });

  return (
    <label
      {...nativeProps}
      ref={ref}
      className={`wf-field field ${className}`.trim()}
      htmlFor={controlId}
    >
      <span id={labelId} className="wf-field-label">
        {label}
      </span>
      {control}
      {hint ? (
        <small id={hintId} className="wf-field-hint">
          {hint}
        </small>
      ) : null}
      {error ? (
        <small id={errorId} className="wf-field-error" role="alert">
          {error}
        </small>
      ) : null}
    </label>
  );
});
