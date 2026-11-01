import React from 'react';
import styles from './Field.module.css';

/**
 * Props for `Field`.
 */
export interface FieldProps {
  /** The control's id; hint and error ids derive from it. */
  id: string;
  /** Visible label text. */
  label: string;
  /** Helper text, hidden while an error shows. */
  hint?: string;
  /** Error text; marks the control invalid. */
  error?: string;
  /** Renders the control with the id, class and ARIA attributes it must carry. */
  children: (props: {
    id: string;
    className: string;
    'aria-invalid': boolean;
    'aria-describedby': string | undefined;
  }) => React.ReactNode;
}

/**
 * A form field: label, control, hint and error.
 *
 * The control is a render prop, so the field works for inputs and selects alike.
 */
export function Field({ id, label, hint, error, children }: FieldProps): React.ReactElement {
  const hintId = hint ? id + '-hint' : undefined;
  const errorId = error ? id + '-error' : undefined;
  const describedBy = [errorId, hintId].filter(Boolean).join(' ') || undefined;
  const className = [styles.control, error ? styles.invalid : ''].filter(Boolean).join(' ');

  return (
    <div className={styles.field}>
      <label className={styles.label} htmlFor={id}>
        {label}
      </label>
      {children({
        id,
        className,
        'aria-invalid': Boolean(error),
        'aria-describedby': describedBy,
      })}
      {error ? (
        <p className={styles.error} id={errorId}>
          {error}
        </p>
      ) : null}
      {hint && !error ? (
        <p className={styles.hint} id={hintId}>
          {hint}
        </p>
      ) : null}
    </div>
  );
}

export default Field;
