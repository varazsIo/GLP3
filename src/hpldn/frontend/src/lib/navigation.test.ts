import { followLink, navigate, subscribeToNavigation } from './navigation';
import { matchPage, pageFor } from './routes';

/**
 * Builds a link in the document and a click on it, without dispatching it.
 */
function clickOn(href: string, attributes: Record<string, string> = {}, init: MouseEventInit = {}): MouseEvent {
  const link = document.createElement('a');
  link.setAttribute('href', href);
  Object.entries(attributes).forEach(([name, value]) => link.setAttribute(name, value));
  link.textContent = 'link';
  document.body.appendChild(link);
  const event = new MouseEvent('click', { bubbles: true, cancelable: true, button: 0, ...init });
  Object.defineProperty(event, 'target', { value: link });
  return event;
}

let shown: string[] = [];
let stop: () => void = () => {};

beforeEach(() => {
  window.history.replaceState(null, '', '/');
  shown = [];
  stop = subscribeToNavigation((pathname) => shown.push(pathname));
});

afterEach(() => {
  stop();
  document.body.innerHTML = '';
  window.history.replaceState(null, '', '/');
});

describe('matchPage', () => {
  it('knows only the site’s own paths, with or without a trailing slash', () => {
    expect(matchPage('/')).toBe('home');
    expect(matchPage('/privacy-policy/')).toBe('privacy');
    expect(matchPage('/nope')).toBeNull();
    expect(pageFor('/nope')).toBe('home');
  });
});

describe('navigate', () => {
  it('adds a history entry, shows the new page and remembers how far the old one was scrolled', () => {
    navigate('/terms-and-conditions#legal-advice');
    expect(window.location.pathname + window.location.hash).toBe('/terms-and-conditions#legal-advice');
    expect(shown).toEqual(['/terms-and-conditions']);

    window.history.back();
    return new Promise<void>((resolve) => {
      window.addEventListener(
        'popstate',
        () => {
          expect(window.history.state).toEqual({ scrollY: 0 });
          expect(shown).toEqual(['/terms-and-conditions', '/']);
          resolve();
        },
        { once: true },
      );
    });
  });

  it('does not swap the page for a step between fragments of the same page', async () => {
    window.history.pushState({}, '', '/#areas');
    const popped = new Promise((resolve) => window.addEventListener('popstate', resolve, { once: true }));
    window.history.back();
    await popped;
    expect(shown).toEqual([]);
  });
});

describe('followLink', () => {
  it('takes over a plain click on a link to another page', () => {
    const event = clickOn('/avm#accuracy');
    followLink(event, '/');
    expect(event.defaultPrevented).toBe(true);
    expect(shown).toEqual(['/avm']);
  });

  it.each([
    ['a modified click', '/avm', {}, { metaKey: true }],
    ['a middle click', '/avm', {}, { button: 1 }],
    ['a new tab', '/avm', { target: '_blank' }, {}],
    ['a download', '/avm', { download: '' }, {}],
    ['another site', 'https://example.com/avm', {}, {}],
    ['a path the site does not serve', '/static/report.pdf', {}, {}],
    ['a fragment of the page showing', '/#faq', {}, {}],
  ])('leaves %s to the browser', (_label, href, attributes, init) => {
    const event = clickOn(href, attributes as Record<string, string>, init as MouseEventInit);
    followLink(event, '/');
    expect(event.defaultPrevented).toBe(false);
    expect(shown).toEqual([]);
  });
});
