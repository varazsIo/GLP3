import React, { useEffect, useRef, useState } from 'react';
import { enGB } from 'date-fns/locale/en-GB';
import { DayPicker } from 'react-day-picker';
import 'react-day-picker/style.css';
import { formatUkDate, isoDate, maskUkDate, parseUkDate } from '../lib/price';
import assignRef from './assignRef';
import Field from './Field';
import styles from './DateField.module.css';

/**
 * Props for `DateField`.
 */
export interface DateFieldProps {
  /** The input's id; the calendar's ids derive from it. */
  id: string;
  /** Visible label text. */
  label: string;
  /** The chosen day as ISO, YYYY-MM-DD, or empty while the text is not a whole date. */
  value: string;
  /** Called with the ISO day, or an empty string while the text is not a whole date. */
  onChange: (iso: string) => void;
  /** The first day that can be chosen, ISO. */
  min: string;
  /** The last day that can be chosen, ISO. */
  max: string;
  /** Error text; marks the input invalid. */
  error?: string;
  name?: string;
  /** The input element, for focusing it from outside. */
  inputRef?: React.Ref<HTMLInputElement>;
}

/**
 * Reads an ISO day as a local midnight, for the calendar.
 */
function toDate(iso: string): Date | undefined {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
  return match ? new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3])) : undefined;
}

/**
 * A date field that always reads DD/MM/YYYY, whatever the browser's locale.
 *
 * The text input is masked as digits arrive; the calendar button opens a
 * Monday-first en-GB calendar where days outside `min` to `max` cannot be
 * picked. Esc or a press outside closes the calendar, and focus returns to the
 * input; tabbing out of it closes it too.
 */
export function DateField({
  id,
  label,
  value,
  onChange,
  min,
  max,
  error,
  name,
  inputRef,
}: DateFieldProps): React.ReactElement {
  const [text, setText] = useState(() => formatUkDate(value));
  const [open, setOpen] = useState(false);
  const input = useRef<HTMLInputElement | null>(null);
  const wrap = useRef<HTMLDivElement>(null);
  const dialogId = id + '-calendar';

  // A day set from outside, or picked in the calendar, shows in the text; a half-typed date is left alone.
  useEffect(() => {
    if (value && parseUkDate(text) !== value) {
      setText(formatUkDate(value));
    }
    // Only a new value re-syncs the text; typing must not re-run this.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);

  useEffect(() => {
    if (!open) {
      return undefined;
    }
    const close = (): void => {
      setOpen(false);
      input.current?.focus({ preventScroll: true });
    };
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') {
        event.preventDefault();
        close();
      }
    };
    const onPointerDown = (event: Event): void => {
      const target = event.target as Node | null;
      if (target && !wrap.current?.contains(target)) {
        close();
      }
    };
    document.addEventListener('keydown', onKeyDown);
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('touchstart', onPointerDown);
    return () => {
      document.removeEventListener('keydown', onKeyDown);
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('touchstart', onPointerDown);
    };
  }, [open]);

  const selected = toDate(value);
  const first = toDate(min);
  const last = toDate(max);

  function handleText(next: string): void {
    const masked = maskUkDate(next);
    setText(masked);
    onChange(parseUkDate(masked) ?? '');
  }

  return (
    <Field id={id} label={label} error={error}>
      {({ className, ...props }) => (
        <div
          className={styles.wrap}
          ref={wrap}
          onBlur={(event) => {
            // Tabbing out of the calendar closes it; a press outside is handled above.
            const next = event.relatedTarget as Node | null;
            if (open && next && !wrap.current?.contains(next)) {
              setOpen(false);
            }
          }}
        >
          <input
            {...props}
            className={[className, styles.input].join(' ')}
            ref={(node) => {
              input.current = node;
              assignRef(inputRef, node);
            }}
            type="text"
            name={name}
            inputMode="numeric"
            autoComplete="off"
            placeholder="DD/MM/YYYY"
            maxLength={10}
            value={text}
            onChange={(event) => handleText(event.target.value)}
          />
          <button
            type="button"
            className={styles.toggle}
            aria-label={open ? 'Close calendar' : 'Choose ' + label.toLowerCase() + ' from a calendar'}
            aria-haspopup="dialog"
            aria-expanded={open}
            aria-controls={open ? dialogId : undefined}
            onClick={() => {
              if (open) {
                setOpen(false);
                input.current?.focus({ preventScroll: true });
              } else {
                setOpen(true);
              }
            }}
          >
            <svg className={styles.icon} viewBox="0 0 24 24" aria-hidden="true" focusable="false">
              <rect x="3.5" y="5" width="17" height="15.5" rx="2" />
              <path d="M3.5 10h17M8 3v4M16 3v4" />
            </svg>
          </button>

          {open ? (
            <div className={styles.popover} id={dialogId} role="dialog" aria-label={label}>
              <DayPicker
                mode="single"
                locale={enGB}
                weekStartsOn={1}
                captionLayout="dropdown"
                startMonth={first}
                endMonth={last}
                defaultMonth={selected ?? first}
                disabled={[...(first ? [{ before: first }] : []), ...(last ? [{ after: last }] : [])]}
                selected={selected}
                autoFocus
                onSelect={(day) => {
                  if (!day) {
                    return;
                  }
                  const iso = isoDate(day);
                  setText(formatUkDate(iso));
                  onChange(iso);
                  setOpen(false);
                  input.current?.focus({ preventScroll: true });
                }}
              />
            </div>
          ) : null}
        </div>
      )}
    </Field>
  );
}

export default DateField;
