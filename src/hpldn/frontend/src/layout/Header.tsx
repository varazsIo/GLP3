import React, { useCallback, useEffect, useRef, useState } from 'react';
import { navigate } from '../lib/navigation';
import { hrefFor } from '../lib/routes';
import { ADDRESS_INPUT_ID } from '../price/PriceForm';
import Button from '../ui/Button';
import styles from './Header.module.css';

/**
 * Props for `Header`.
 */
export interface HeaderProps {
  /**
   * Called when the landlord asks to price a home. Without it, as on the legal
   * and AVM pages, the action navigates to the home page's address search.
   */
  onGetEstimate?: () => void;
  /** Transparent until scrolled, for the dark home hero. Otherwise always solid. */
  overHero?: boolean;
}

const LINKS: ReadonlyArray<{ id: string; href: string; label: string }> = [
  { id: 'top', href: hrefFor('home', 'top'), label: 'Price a home' },
  { id: 'areas', href: hrefFor('home', 'areas'), label: 'Areas' },
  { id: 'yield', href: hrefFor('home', 'yield'), label: 'Yield' },
  { id: 'how', href: hrefFor('home', 'how'), label: 'How it works' },
];

/** The home page's sections in order, the linked ones and the one after them, which has no link. */
const SPY_IDS: ReadonlyArray<string> = [...LINKS.map((link) => link.id), 'faq'];

/**
 * Returns the section being read: the last one whose top has passed a line a
 * third of the way down the screen, or null when none of them is on the page.
 */
function sectionInView(): string | null {
  const line = window.innerHeight / 3;
  let current: string | null = null;
  SPY_IDS.forEach((id) => {
    const element = document.getElementById(id);
    if (element && element.getBoundingClientRect().top <= line) {
      current = id;
    }
  });
  return current;
}

/**
 * Opens the home page at the address search, without reloading, and focuses it.
 */
function goToForm(): void {
  navigate(hrefFor('home', 'top'), { focusId: ADDRESS_INPUT_ID });
}

/**
 * Site header with section links and a mobile menu.
 *
 * Transparent over the home hero, solid with a hairline once scrolled past it.
 * On the home page the link to the section being read is marked current.
 * The mobile menu covers the page below the header, scrolls inside itself on
 * a short screen, holds the page still and inert behind it, closes on Esc or a tap outside, and moves focus to its
 * first link on opening and back to the Menu button on closing (unless a link
 * was followed).
 */
export function Header({ onGetEstimate = goToForm, overHero = false }: HeaderProps): React.ReactElement {
  const [scrolled, setScrolled] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [activeId, setActiveId] = useState<string | null>(null);
  const menuButton = useRef<HTMLButtonElement>(null);
  const mobileNav = useRef<HTMLElement>(null);
  const returnFocus = useRef(false);

  const closeMenu = useCallback((restoreFocus: boolean) => {
    returnFocus.current = restoreFocus;
    setMenuOpen(false);
  }, []);

  useEffect(() => {
    const onScroll = (): void => {
      setScrolled(window.scrollY > 80);
      // Only the home page has the sections; elsewhere no link is current.
      setActiveId(overHero ? sectionInView() : null);
    };
    onScroll();
    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('resize', onScroll);
    return () => {
      window.removeEventListener('scroll', onScroll);
      window.removeEventListener('resize', onScroll);
    };
  }, [overHero]);

  useEffect(() => {
    if (!menuOpen) {
      if (returnFocus.current) {
        returnFocus.current = false;
        menuButton.current?.focus({ preventScroll: true });
      }
      return undefined;
    }
    mobileNav.current?.querySelector<HTMLElement>('a, button')?.focus({ preventScroll: true });

    const onResize = (): void => {
      if (window.innerWidth > 900) {
        closeMenu(false);
      }
    };
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') {
        closeMenu(true);
      }
    };
    const onPointerDown = (event: Event): void => {
      const target = event.target as Node | null;
      if (target && !mobileNav.current?.contains(target) && !menuButton.current?.contains(target)) {
        closeMenu(false);
      }
    };
    const bodyOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    // The page behind the menu is out of reach of Tab and screen readers until it closes.
    const behind = Array.from(document.querySelectorAll<HTMLElement>('main, footer'));
    behind.forEach((element) => element.setAttribute('inert', ''));
    window.addEventListener('resize', onResize);
    document.addEventListener('keydown', onKeyDown);
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('touchstart', onPointerDown);
    return () => {
      document.body.style.overflow = bodyOverflow;
      behind.forEach((element) => element.removeAttribute('inert'));
      window.removeEventListener('resize', onResize);
      document.removeEventListener('keydown', onKeyDown);
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('touchstart', onPointerDown);
    };
  }, [menuOpen, closeMenu]);

  const solid = !overHero || scrolled || menuOpen;

  return (
    <header className={[styles.header, solid ? styles.scrolled : ''].filter(Boolean).join(' ')}>
      <div className={styles.inner}>
        <a className={styles.wordmark} href={hrefFor('home', 'top')} aria-label="HPLDN, home">
          HPLDN
        </a>

        <nav className={styles.nav} aria-label="Sections">
          {LINKS.map((link) => (
            <a
              className={[styles.navLink, link.id === activeId ? styles.active : ''].filter(Boolean).join(' ')}
              key={link.href}
              href={link.href}
              aria-current={link.id === activeId ? 'location' : undefined}
            >
              {link.label}
            </a>
          ))}
        </nav>

        <div className={styles.action}>
          <Button variant="primary" size="sm" onClick={onGetEstimate}>
            Price a home
          </Button>
        </div>

        <button
          type="button"
          className={styles.menuButton}
          aria-expanded={menuOpen}
          aria-controls="mobile-nav"
          ref={menuButton}
          onClick={() => (menuOpen ? closeMenu(true) : setMenuOpen(true))}
        >
          {menuOpen ? 'Close' : 'Menu'}
        </button>
      </div>

      {menuOpen ? (
        <nav className={styles.mobileNav} id="mobile-nav" aria-label="Sections" ref={mobileNav}>
          {LINKS.map((link) => (
            <a
              className={[styles.mobileLink, link.id === activeId ? styles.active : ''].filter(Boolean).join(' ')}
              key={link.href}
              href={link.href}
              aria-current={link.id === activeId ? 'location' : undefined}
              onClick={() => closeMenu(false)}
            >
              {link.label}
            </a>
          ))}
          <Button
            variant="primary"
            onClick={() => {
              closeMenu(false);
              onGetEstimate();
            }}
          >
            Price a home
          </Button>
        </nav>
      ) : null}
    </header>
  );
}

export default Header;
