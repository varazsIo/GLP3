/**
 * The site's pages and their addresses.
 *
 * The home page is the scrolling tool; the legal pages and the AVM explainer
 * have their own paths. There is no router library: every path serves the same
 * bundle, which picks the page from the pathname, and `./navigation` moves
 * between pages without reloading.
 */

/** A page the site can show. */
export type PageKey = 'home' | 'terms' | 'privacy' | 'avm';

const BASE = (process.env.PUBLIC_URL ?? '').replace(/\/$/, '');

const PATHS: Record<PageKey, string> = {
  home: '/',
  terms: '/terms-and-conditions',
  privacy: '/privacy-policy',
  avm: '/avm',
};

/**
 * Builds the href for a page.
 *
 * @param page The page to link to.
 * @param fragment An optional anchor, e.g. `hrefFor('home', 'faq')`.
 */
export function hrefFor(page: PageKey, fragment?: string): string {
  return BASE + PATHS[page] + (fragment ? '#' + fragment : '');
}

/**
 * Returns the page a pathname is the address of, or null for a path the site
 * does not serve.
 */
export function matchPage(pathname: string): PageKey | null {
  const path = pathname.startsWith(BASE) ? pathname.slice(BASE.length) : pathname;
  const trimmed = path.replace(/\/+$/, '') || '/';
  return (Object.keys(PATHS) as PageKey[]).find((key) => PATHS[key] === trimmed) ?? null;
}

/**
 * Returns the page a pathname shows, falling back to home for anything unrecognised.
 */
export function pageFor(pathname: string): PageKey {
  return matchPage(pathname) ?? 'home';
}
