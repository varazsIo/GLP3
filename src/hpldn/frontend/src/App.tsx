import React, { useEffect, useRef, useState } from 'react';
import { AreasProvider } from './data/AreasProvider';
import { followLink, subscribeToNavigation } from './lib/navigation';
import { pageFor } from './lib/routes';
import type { PageKey } from './lib/routes';
import AvmPage from './pages/AvmPage';
import HomePage from './pages/HomePage';
import PrivacyPage from './pages/PrivacyPage';
import TermsPage from './pages/TermsPage';

/** The document title for each page. */
const TITLES: Record<PageKey, string> = {
  home: 'HPLDN — What’s a London home worth?',
  terms: 'Terms and conditions — HPLDN',
  privacy: 'Privacy policy — HPLDN',
  avm: 'How our estimates work — HPLDN',
};

/**
 * Props for `App`.
 */
export interface AppProps {
  /** The pathname to render. Defaults to the browser's; tests pass one in. */
  path?: string;
}

/**
 * The root component: renders the page for the current path and sets its title.
 *
 * Links between pages swap the page in place (`lib/navigation`), so moving
 * around never reloads the document. One `AreasProvider` wraps every page, so
 * the area figures load once, when the first page that shows them opens, and
 * are still there on the way back.
 */
export function App({ path }: AppProps): React.ReactElement {
  const [pathname, setPathname] = useState(() => path ?? window.location.pathname);
  const page = pageFor(pathname);
  const shownPath = useRef(pathname);
  shownPath.current = pathname;

  useEffect(() => subscribeToNavigation(setPathname), []);

  useEffect(() => {
    const onClick = (event: MouseEvent): void => followLink(event, shownPath.current);
    document.addEventListener('click', onClick);
    return () => document.removeEventListener('click', onClick);
  }, []);

  useEffect(() => {
    document.title = TITLES[page];
  }, [page]);

  let content: React.ReactElement;
  switch (page) {
    case 'terms':
      content = <TermsPage />;
      break;
    case 'privacy':
      content = <PrivacyPage />;
      break;
    case 'avm':
      content = <AvmPage />;
      break;
    default:
      content = <HomePage />;
  }
  return <AreasProvider>{content}</AreasProvider>;
}

export default App;
