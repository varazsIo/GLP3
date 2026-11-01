import React from 'react';
import { render, screen } from '@testing-library/react';
import Reveal from './Reveal';
import ValueHeadline from '../estimate/ValueHeadline';

type Listener = (entries: IntersectionObserverEntry[]) => void;

/** A stand-in observer that never reports an intersection. */
class SilentObserver {
  readonly callback: Listener;

  constructor(callback: Listener) {
    // Kept but never called: nothing ever intersects.
    this.callback = callback;
  }

  observe(): void {}

  unobserve(): void {}

  disconnect(): void {}

  takeRecords(): IntersectionObserverEntry[] {
    return [];
  }
}

describe('Reveal', () => {
  const original = (window as unknown as { IntersectionObserver?: unknown }).IntersectionObserver;

  afterEach(() => {
    (window as unknown as { IntersectionObserver?: unknown }).IntersectionObserver = original;
  });

  it('renders its children even before anything scrolls into view', () => {
    (window as unknown as { IntersectionObserver: unknown }).IntersectionObserver = SilentObserver;
    render(
      <Reveal>
        <p>Prices here are rising</p>
      </Reveal>,
    );
    expect(screen.getByText('Prices here are rising')).toBeInTheDocument();
  });

  it('shows content outright where the observer is unavailable', () => {
    delete (window as unknown as { IntersectionObserver?: unknown }).IntersectionObserver;
    render(
      <Reveal>
        <p>Near the river</p>
      </Reveal>,
    );
    expect(screen.getByText('Near the river')).toBeInTheDocument();
  });
});

describe('ValueHeadline', () => {
  it('has the final figure in the DOM from the first render', () => {
    render(<ValueHeadline value={642000} />);
    expect(screen.getByText('£642,000')).toBeInTheDocument();
  });

  it('labels the figure in landlord language', () => {
    render(<ValueHeadline value={642000} note="72 m², 2 bedrooms" />);
    expect(screen.getByText('Estimated value')).toBeInTheDocument();
    expect(screen.getByText('72 m², 2 bedrooms')).toBeInTheDocument();
  });

  it('shows no note line when given none, as on the price card', () => {
    const { container } = render(<ValueHeadline value={244000} label="Price if sold on 15 September 2026" />);
    expect(container).toHaveTextContent(/^Price if sold on 15 September 2026£244,000$/);
  });
});
