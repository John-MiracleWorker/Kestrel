import {
  forwardRef,
  type ButtonHTMLAttributes,
  type ReactNode,
} from "react";
import { LoaderCircle } from "lucide-react";

export type ButtonVariant = "primary" | "secondary" | "quiet" | "danger";
export type ButtonSize = "small" | "medium";

export type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant;
  size?: ButtonSize;
  pending?: boolean;
  children: ReactNode;
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  function Button(
    {
      variant = "secondary",
      size = "medium",
      pending = false,
      disabled,
      type = "button",
      className = "",
      "aria-busy": ariaBusy,
      children,
      ...nativeProps
    },
    ref,
  ) {
    const compatibilityVariant =
      variant === "primary"
        ? "primary"
        : variant === "quiet"
          ? "subtle"
          : variant === "danger"
            ? "danger"
            : "";

    return (
      <button
        {...nativeProps}
        ref={ref}
        type={type}
        disabled={disabled || pending}
        aria-busy={pending ? true : ariaBusy}
        className={[
          "wf-button",
          "btn",
          `wf-button-${variant}`,
          `wf-button-${size}`,
          compatibilityVariant,
          className,
        ]
          .filter(Boolean)
          .join(" ")}
      >
        {pending ? (
          <LoaderCircle
            className="wf-button-spinner"
            size={16}
            aria-hidden="true"
          />
        ) : null}
        <span className="wf-button-label">{children}</span>
      </button>
    );
  },
);
