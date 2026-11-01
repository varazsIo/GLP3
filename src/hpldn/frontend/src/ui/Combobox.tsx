import React, { useEffect, useRef, useState } from 'react';
import assignRef from './assignRef';
import Button from './Button';
import Field from './Field';
import styles from './Combobox.module.css';

/**
 * How long the message and result count must stay the same before screen
 * readers hear them, in milliseconds, so a fast typist is not read "Searching…"
 * at every pause.
 */
export const ANNOUNCE_DELAY_MS = 500;

/**
 * Props for `Combobox`.
 */
export interface ComboboxProps<T> {
  /** The input's id; the listbox and option ids derive from it. */
  id: string;
  /** Visible label text. */
  label: string;
  /** What the input shows. */
  inputValue: string;
  /** Called with the text as the landlord types. */
  onInputChange: (text: string) => void;
  /** The suggestions to offer, in order. */
  options: ReadonlyArray<T>;
  /** A stable key for an option. */
  getOptionKey: (option: T) => string;
  /** How an option reads to assistive technology, e.g. its full address. */
  getOptionLabel: (option: T) => string;
  /** How an option looks in the list; defaults to its label. */
  renderOption?: (option: T) => React.ReactNode;
  /** Called with the option the landlord chooses. */
  onSelect: (option: T) => void;
  /** A line in the list's place, e.g. "Searching…" or why nothing was found. */
  message?: string | null;
  /** The option currently chosen, marked in the list. */
  selectedKey?: string | null;
  placeholder?: string;
  /** Error text; marks the input invalid. */
  error?: string;
  /** Helper text under the input. */
  hint?: string;
  /** Opens the list as soon as the input takes focus, rather than on typing. */
  openOnFocus?: boolean;
  /** Selects the input's text on focus, so typing replaces it. */
  selectOnFocus?: boolean;
  /** Called when focus leaves the input. */
  onBlur?: () => void;
  /** The input element, for focusing it from outside. */
  inputRef?: React.Ref<HTMLInputElement>;
  /** Passed to the input, e.g. "off" or "street-address". */
  autoComplete?: string;
  name?: string;
  /** Marks the input `aria-busy` while suggestions are being fetched. */
  busy?: boolean;
  /** Shows a button under the message that calls this, e.g. to search again after a failure. */
  onRetry?: () => void;
  /** The retry button's text; defaults to "Try again". */
  retryLabel?: string;
  /** The most characters the input takes. */
  maxLength?: number;
}

/**
 * Returns `value` once it has stayed the same for `delayMs`.
 */
function useSettled<T>(value: T, delayMs: number): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);
  return settled;
}

/**
 * An editable combobox with a list of suggestions (the WAI-ARIA 1.2 pattern).
 *
 * The input keeps focus throughout: ↑ and ↓ move the active suggestion, which
 * `aria-activedescendant` announces, Enter chooses it, Esc closes the list and
 * Tab leaves. A press on a suggestion chooses it without taking focus from the
 * input, for mouse and touch alike. The text and the options belong to the
 * caller, so it can search a server or filter a local list.
 *
 * The visible message changes at once; screen readers hear it, or how many
 * suggestions there are, from a separate live region once it has settled for
 * `ANNOUNCE_DELAY_MS`. With `onRetry`, the message carries a button Tab reaches
 * from the input without the list closing first.
 */
export function Combobox<T>({
  id,
  label,
  inputValue,
  onInputChange,
  options,
  getOptionKey,
  getOptionLabel,
  renderOption,
  onSelect,
  message,
  selectedKey,
  placeholder,
  error,
  hint,
  openOnFocus = false,
  selectOnFocus = false,
  onBlur,
  inputRef,
  autoComplete = 'off',
  name,
  busy = false,
  onRetry,
  retryLabel = 'Try again',
  maxLength,
}: ComboboxProps<T>): React.ReactElement {
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const listRef = useRef<HTMLUListElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const inputEl = useRef<HTMLInputElement | null>(null);
  const listboxId = id + '-listbox';

  const hasOptions = options.length > 0;
  const expanded = open && hasOptions;
  const showMessage = open && Boolean(message);
  const showRetry = showMessage && Boolean(onRetry);

  let announcement = '';
  if (open && message) {
    announcement = message;
  } else if (expanded) {
    announcement = options.length === 1 ? '1 suggestion' : options.length + ' suggestions';
  }
  const announced = useSettled(announcement, ANNOUNCE_DELAY_MS);

  // New options start with nothing active, so Enter never picks a stale row.
  useEffect(() => {
    setActiveIndex(-1);
  }, [options]);

  useEffect(() => {
    if (activeIndex < 0 || !listRef.current) {
      return;
    }
    const option = listRef.current.children[activeIndex] as HTMLElement | undefined;
    if (option && typeof option.scrollIntoView === 'function') {
      option.scrollIntoView({ block: 'nearest' });
    }
  }, [activeIndex]);

  function choose(option: T): void {
    onSelect(option);
    setOpen(false);
    setActiveIndex(-1);
  }

  function handleKeyDown(event: React.KeyboardEvent<HTMLInputElement>): void {
    switch (event.key) {
      case 'ArrowDown': {
        event.preventDefault();
        if (!open) {
          setOpen(true);
          if (event.altKey) {
            return;
          }
        }
        if (hasOptions) {
          setActiveIndex((index) => (index + 1) % options.length);
        }
        return;
      }
      case 'ArrowUp': {
        event.preventDefault();
        if (!open) {
          setOpen(true);
        }
        if (hasOptions) {
          setActiveIndex((index) => (index <= 0 ? options.length - 1 : index - 1));
        }
        return;
      }
      case 'Enter': {
        if (expanded && activeIndex >= 0 && activeIndex < options.length) {
          event.preventDefault();
          choose(options[activeIndex]);
        }
        return;
      }
      case 'Escape': {
        if (open) {
          event.preventDefault();
          setOpen(false);
          setActiveIndex(-1);
        }
        return;
      }
      case 'Tab': {
        // Tab moves to the retry button while there is one, so keep it on screen.
        if (!(showRetry && !event.shiftKey)) {
          setOpen(false);
        }
        return;
      }
      default:
    }
  }

  const activeId = expanded && activeIndex >= 0 ? id + '-option-' + activeIndex : undefined;

  return (
    <Field id={id} label={label} error={error} hint={hint}>
      {(props) => (
        <div className={styles.wrap} ref={wrapRef}>
          <input
            {...props}
            ref={(node) => {
              inputEl.current = node;
              assignRef(inputRef, node);
            }}
            type="text"
            name={name}
            role="combobox"
            aria-autocomplete="list"
            aria-expanded={expanded}
            aria-controls={listboxId}
            aria-activedescendant={activeId}
            aria-busy={busy || undefined}
            maxLength={maxLength}
            autoComplete={autoComplete}
            spellCheck={false}
            placeholder={placeholder}
            value={inputValue}
            onChange={(event) => {
              onInputChange(event.target.value);
              setOpen(true);
            }}
            onKeyDown={handleKeyDown}
            onFocus={(event) => {
              if (selectOnFocus) {
                event.target.select();
              }
              if (openOnFocus) {
                setOpen(true);
              }
            }}
            onClick={() => {
              if (openOnFocus) {
                setOpen(true);
              }
            }}
            onBlur={(event) => {
              // Focus moving to the retry button keeps the list open.
              if (!wrapRef.current?.contains(event.relatedTarget as Node | null)) {
                setOpen(false);
              }
              setActiveIndex(-1);
              onBlur?.();
            }}
          />
          {/* Mounted while the list is open, empty until something settles, so it is
              already a live region when its first announcement arrives. */}
          {open ? (
            <p className={styles.announcer} role="status" aria-live="polite">
              {announced}
            </p>
          ) : null}
          <div
            className={styles.popup}
            hidden={!expanded && !showMessage}
            onBlur={(event) => {
              if (!wrapRef.current?.contains(event.relatedTarget as Node | null)) {
                setOpen(false);
              }
            }}
          >
            {showMessage ? (
              <div className={styles.messageRow}>
                <p className={styles.message}>{message}</p>
                {showRetry ? (
                  <Button
                    variant="secondary"
                    size="sm"
                    shape="rect"
                    className={styles.retry}
                    onMouseDown={(event) => event.preventDefault()}
                    onClick={() => {
                      onRetry?.();
                      inputEl.current?.focus();
                    }}
                  >
                    {retryLabel}
                  </Button>
                ) : null}
              </div>
            ) : null}
            <ul className={styles.listbox} id={listboxId} role="listbox" aria-label={label} ref={listRef} hidden={!expanded}>
              {options.map((option, index) => {
                const key = getOptionKey(option);
                return (
                  <li
                    key={key}
                    id={id + '-option-' + index}
                    role="option"
                    aria-selected={index === activeIndex}
                    aria-label={renderOption ? getOptionLabel(option) : undefined}
                    className={[
                      styles.option,
                      index === activeIndex ? styles.active : '',
                      key === selectedKey ? styles.chosen : '',
                    ]
                      .filter(Boolean)
                      .join(' ')}
                    // Keep focus in the input, so the choice does not blur and close the list first.
                    onMouseDown={(event) => event.preventDefault()}
                    onMouseMove={() => {
                      if (index !== activeIndex) {
                        setActiveIndex(index);
                      }
                    }}
                    onClick={() => choose(option)}
                  >
                    {renderOption ? renderOption(option) : getOptionLabel(option)}
                  </li>
                );
              })}
            </ul>
          </div>
        </div>
      )}
    </Field>
  );
}

export default Combobox;
