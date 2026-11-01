/**
 * Moving between the site's pages without reloading the document.
 *
 * Every page is in the one bundle, so following a link to another page swaps
 * the page in place: no blank document, no white flash, no second load of the
 * area figures. Where the browser supports view transitions the old page
 * cross-fades into the new one; with reduced motion it simply changes.
 *
 * `App` listens with `subscribeToNavigation` and hands link clicks to
 * `followLink`; anything else calls `navigate`.
 */
import { flushSync } from 'react-dom';
import { matchPage, pageFor } from './routes';

type Listener = (pathname: string) => void;

/** The parts of a `ViewTransition` this module touches, which older DOM typings lack. */
interface ViewTransitionLike {
  ready?: Promise<unknown>;
  finished?: Promise<unknown>;
  updateCallbackDone?: Promise<unknown>;
}

const listeners = new Set<Listener>();

/** The pathname of the page last shown, to tell a change of page from a change of fragment. */
let shownPathname = '';

/** What a history entry remembers: how far its page was scrolled, so Back returns to the same place. */
interface HistoryState {
  scrollY?: number;
}

/**
 * Calls `listener` with the new pathname whenever the page changes, including
 * on Back and Forward.
 *
 * @returns A function that stops listening.
 */
export function subscribeToNavigation(listener: Listener): () => void {
  if (!listeners.size) {
    shownPathname = window.location.pathname;
    window.addEventListener('popstate', onPopState);
  }
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
    if (!listeners.size) {
      window.removeEventListener('popstate', onPopState);
    }
  };
}

function prefersReducedMotion(): boolean {
  return typeof window.matchMedia !== 'function' || window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

/**
 * Runs a page swap inside a view transition where there is one.
 *
 * The swap renders synchronously (`flushSync`), so the transition captures the
 * new page, and `after` runs once it is in the DOM.
 */
function swapPage(pathname: string, after: () => void): void {
  shownPathname = pathname;
  const update = (): void => {
    flushSync(() => listeners.forEach((listener) => listener(pathname)));
    after();
  };
  const doc = document as Document & { startViewTransition?: (callback: () => void) => ViewTransitionLike | undefined };
  // A hidden tab cannot run a transition, and the browser rejects one started there.
  if (typeof doc.startViewTransition !== 'function' || prefersReducedMotion() || doc.visibilityState === 'hidden') {
    update();
    return;
  }
  let transition: ViewTransitionLike | undefined;
  try {
    transition = doc.startViewTransition(update);
  } catch {
    update();
    return;
  }
  // A transition the browser skips or aborts (another starting over it, the tab
  // hiding mid-way) still swaps the page; its rejected promises are expected,
  // so they are handled here rather than reported as uncaught errors.
  [transition?.ready, transition?.finished, transition?.updateCallbackDone].forEach((promise) => {
    promise?.catch(() => undefined);
  });
}

/**
 * Scrolls straight to a position, overriding the page's smooth scrolling:
 * a new page should open in place, not glide there from the old one.
 */
function jumpTo(top: number, element?: HTMLElement | null): void {
  const root = document.documentElement;
  const behaviour = root.style.scrollBehavior;
  root.style.scrollBehavior = 'auto';
  if (element) {
    if (typeof element.scrollIntoView === 'function') {
      element.scrollIntoView({ block: 'start' });
    }
  } else {
    window.scrollTo(0, top);
  }
  root.style.scrollBehavior = behaviour;
}

/**
 * Settles a newly shown page: scrolls to its fragment or its top, and moves
 * focus to the element asked for, or else the main content, so a screen
 * reader starts reading the new page.
 */
function settle(hash: string, scrollY: number | undefined, focusId?: string): void {
  const target = hash ? document.getElementById(decodeURIComponent(hash.slice(1))) : null;
  jumpTo(scrollY ?? 0, scrollY === undefined ? target : null);
  const focus = (focusId ? document.getElementById(focusId) : null) ?? document.getElementById('main');
  focus?.focus({ preventScroll: true });
}

/**
 * Back or Forward: swaps the page if it changed. A step between fragments of
 * the same page is the browser's to scroll.
 */
function onPopState(event: PopStateEvent): void {
  const { pathname, hash } = window.location;
  if (pageFor(pathname) === pageFor(shownPathname)) {
    shownPathname = pathname;
    return;
  }
  const state = (event.state ?? {}) as HistoryState;
  swapPage(pathname, () => settle(hash, state.scrollY));
}

/**
 * Shows another page of the site without reloading, adding it to the history.
 *
 * @param href A site path, with any fragment, e.g. "/terms-and-conditions#legal-advice".
 * @param options `focusId` focuses that element once the page shows, e.g. the address search.
 */
export function navigate(href: string, options: { focusId?: string } = {}): void {
  const url = new URL(href, window.location.href);
  const current = (window.history.state ?? {}) as HistoryState;
  window.history.replaceState({ ...current, scrollY: window.scrollY }, '');
  window.history.pushState({}, '', url.pathname + url.search + url.hash);
  swapPage(url.pathname, () => settle(url.hash, undefined, options.focusId));
}

/**
 * Handles a click anywhere in the page: a plain left click on a link to
 * another page of the site navigates in place instead of reloading.
 *
 * Left alone: modified or middle clicks, links with a target or a download,
 * other sites, paths the site does not serve, and links to a fragment of the
 * page already showing, which the browser scrolls to itself.
 *
 * @param event The click.
 * @param currentPath The pathname of the page showing.
 */
export function followLink(event: MouseEvent, currentPath: string): void {
  if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
    return;
  }
  const anchor = event.target instanceof Element ? event.target.closest('a') : null;
  if (!anchor || !anchor.getAttribute('href') || anchor.hasAttribute('download')) {
    return;
  }
  if (anchor.target && anchor.target !== '_self') {
    return;
  }
  const url = new URL(anchor.href, window.location.href);
  if (url.origin !== window.location.origin || matchPage(url.pathname) === null) {
    return;
  }
  if (matchPage(url.pathname) === matchPage(currentPath) && url.hash) {
    return;
  }
  event.preventDefault();
  navigate(url.pathname + url.search + url.hash);
}
