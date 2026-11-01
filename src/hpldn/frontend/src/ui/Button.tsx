import React from 'react';
import styles from './Button.module.css';

/** Visual weight of a button. */
export type ButtonVariant = 'primary' | 'secondary' | 'ghost';
/** Button size; `field` matches the height and padding of a form control. */
export type ButtonSize = 'sm' | 'md' | 'lg' | 'field';
/** A pill for standalone actions, a rectangle for anything sitting in a form. */
export type ButtonShape = 'pill' | 'rect';

/**
 * Props for `Button`, extending the native button attributes.
 */
export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  /** Defaults to `primary`. */
  variant?: ButtonVariant;
  /** Defaults to `md`. */
  size?: ButtonSize;
  /** Defaults to `pill`. */
  shape?: ButtonShape;
  /** Stretches the button to its container's width. */
  fullWidth?: boolean;
  /**
   * Shows a spinner while the action it started is under way. The button
   * stays live: pressing it again starts the action afresh, which is how a
   * form replaces a request still in flight.
   */
  loading?: boolean;
  /** The rendered button, for a caller that moves focus to it. */
  ref?: React.Ref<HTMLButtonElement>;
}

/**
 * The page's only styled button, in primary, secondary and ghost variants.
 */
export function Button({
  variant = 'primary',
  size = 'md',
  shape = 'pill',
  fullWidth = false,
  loading = false,
  className,
  type = 'button',
  children,
  ...rest
}: ButtonProps): React.ReactElement {
  const classes = [styles.base, styles[variant], styles[size], styles[shape]];
  if (fullWidth) {
    classes.push(styles.full);
  }
  if (loading) {
    classes.push(styles.loading);
  }
  if (className) {
    classes.push(className);
  }
  return (
    <button
      type={type}
      className={classes.join(' ')}
      aria-busy={loading || undefined}
      {...rest}
    >
      {loading ? <span className={styles.spinner} aria-hidden="true" /> : null}
      {children}
    </button>
  );
}

export default Button;
