import { useEffect, useState } from 'react';

/**
 * Tracks whether a media query matches, e.g. `useMediaQuery('(max-width: 640px)')`.
 *
 * @param query The media query.
 * @returns True while it matches; false where it cannot be queried, as in tests.
 */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState<boolean>(() =>
    typeof window !== 'undefined' && typeof window.matchMedia === 'function' ? window.matchMedia(query).matches : false,
  );

  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
      return undefined;
    }
    const list = window.matchMedia(query);
    const onChange = (): void => setMatches(list.matches);
    setMatches(list.matches);
    if (list.addEventListener) {
      list.addEventListener('change', onChange);
      return () => list.removeEventListener('change', onChange);
    }
    list.addListener(onChange);
    return () => list.removeListener(onChange);
  }, [query]);

  return matches;
}

export default useMediaQuery;
