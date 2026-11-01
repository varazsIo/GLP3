import { useEffect, useState } from 'react';

const QUERY = '(prefers-reduced-motion: reduce)';

/**
 * Tracks whether the operating system asks for reduced motion.
 *
 * @returns True when `prefers-reduced-motion: reduce` matches, or when it
 *     cannot be queried.
 */
export function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState<boolean>(() => {
    if (typeof window === 'undefined' || !window.matchMedia) {
      return true;
    }
    return window.matchMedia(QUERY).matches;
  });

  useEffect(() => {
    if (typeof window === 'undefined' || !window.matchMedia) {
      return undefined;
    }
    const list = window.matchMedia(QUERY);
    const onChange = (): void => setReduced(list.matches);
    setReduced(list.matches);
    if (list.addEventListener) {
      list.addEventListener('change', onChange);
      return () => list.removeEventListener('change', onChange);
    }
    list.addListener(onChange);
    return () => list.removeListener(onChange);
  }, []);

  return reduced;
}

export default useReducedMotion;
