import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { getAreas } from '../lib/api';
import type { ApiError } from '../lib/api';
import type { AreaStats, DataWindow, MortgageRates } from './types';

/** The area the page shows before the landlord picks one. */
export const DEFAULT_DISTRICT = 'SW11';

/** Where the area figures' single load has got to. */
export type AreasStatus = 'loading' | 'ready' | 'error';

/**
 * The area figures and load state shared by `AreasProvider`.
 */
export interface AreasState {
  status: AreasStatus;
  /** Every covered area, in the server's order. Empty until ready. */
  areas: AreaStats[];
  /** The same areas, keyed by code. */
  areaByCode: Record<string, AreaStats>;
  /** The years the figures come from. Null until ready. */
  window: DataWindow | null;
  /** The latest published mortgage rates. Null until ready, or when none are held. */
  mortgageRates: MortgageRates | null;
  /** A sentence to show when status is 'error'. */
  errorMessage: string | null;
  /** Starts the load again after an error. */
  retry: () => void;
  /** Starts the first load; `useAreas` calls it, so pages without figures load nothing. */
  request: () => void;
}

const AreasContext = createContext<AreasState | null>(null);

interface Loaded {
  areas: AreaStats[];
  window: DataWindow;
  mortgageRates: MortgageRates | null;
}

/**
 * Loads the area figures once and shares them with every page below it.
 *
 * Fetches `GET /areas` when the first component asks for the figures, and on
 * `retry`, cancelling a stale request. The figures stay loaded as pages come
 * and go.
 */
export function AreasProvider({ children }: { children: React.ReactNode }): React.ReactElement {
  const [status, setStatus] = useState<AreasStatus>('loading');
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [wanted, setWanted] = useState(false);

  useEffect(() => {
    if (!wanted) {
      return undefined;
    }
    const controller = new AbortController();
    setStatus('loading');
    setErrorMessage(null);
    getAreas(controller.signal)
      .then((response) => {
        setLoaded({ areas: response.areas, window: response.window, mortgageRates: response.mortgageRates ?? null });
        setStatus('ready');
      })
      .catch((error: unknown) => {
        // Only this effect's own cancellation is silent; a timeout while the
        // body is read is an error with a retry, not a load that never ends.
        if (controller.signal.aborted) {
          return;
        }
        setErrorMessage(
          (error as Partial<ApiError>)?.userMessage ??
            'We could not load the area figures just now. Try again in a moment.',
        );
        setStatus('error');
      });
    return () => controller.abort();
  }, [attempt, wanted]);

  const retry = useCallback(() => setAttempt((n) => n + 1), []);
  const request = useCallback(() => setWanted(true), []);

  const value = useMemo<AreasState>(() => {
    const areas = loaded?.areas ?? [];
    const areaByCode: Record<string, AreaStats> = {};
    areas.forEach((area) => {
      areaByCode[area.code] = area;
    });
    return {
      status,
      areas,
      areaByCode,
      window: loaded?.window ?? null,
      mortgageRates: loaded?.mortgageRates ?? null,
      errorMessage,
      retry,
      request,
    };
  }, [status, loaded, errorMessage, retry, request]);

  return <AreasContext.Provider value={value}>{children}</AreasContext.Provider>;
}

/**
 * Returns the live area figures, starting their load if nothing has yet.
 *
 * @throws {Error} When used outside an `AreasProvider`.
 */
export function useAreas(): AreasState {
  const state = useContext(AreasContext);
  if (!state) {
    throw new Error('useAreas must be used inside an AreasProvider');
  }
  const { request } = state;
  useEffect(() => request(), [request]);
  return state;
}
