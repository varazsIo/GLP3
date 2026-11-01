import React, { useEffect, useRef, useState } from 'react';
import styles from './Reveal.module.css';

/**
 * Props for `Reveal`.
 */
export interface RevealProps {
  /** Stagger within a group, in milliseconds. */
  delay?: number;
  /** The element to render; defaults to `div`. */
  as?: 'div' | 'li' | 'section';
  className?: string;
  children: React.ReactNode;
}

/**
 * Fades and lifts its children the first time they scroll into view.
 *
 * Without `IntersectionObserver` (server render, tests, older browsers) the
 * content is visible from the start.
 */
export function Reveal({
  delay = 0,
  as = 'div',
  className,
  children,
}: RevealProps): React.ReactElement {
  const ref = useRef<HTMLElement | null>(null);
  const [shown, setShown] = useState(
    () => typeof window === 'undefined' || typeof window.IntersectionObserver !== 'function',
  );

  useEffect(() => {
    if (shown || !ref.current) {
      return undefined;
    }
    const node = ref.current;
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            setShown(true);
            observer.disconnect();
          }
        });
      },
      { rootMargin: '0px 0px -10% 0px', threshold: 0.05 },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [shown]);

  const Tag = as as React.ElementType;
  return (
    <Tag
      ref={ref}
      className={[styles.reveal, shown ? styles.shown : '', className]
        .filter(Boolean)
        .join(' ')}
      style={delay && !shown ? { transitionDelay: delay + 'ms' } : undefined}
    >
      {children}
    </Tag>
  );
}

export default Reveal;
