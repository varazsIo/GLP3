import React from 'react';
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import type { AddressOption } from '../data/types';
import { ApiError } from '../lib/api';
import { ADDRESSES } from '../test/fixtures';
import { ANNOUNCE_DELAY_MS } from '../ui/Combobox';
import AddressSearch, { MIN_SEARCH_INTERVAL_MS, SEARCH_DEBOUNCE_MS } from './AddressSearch';

const findAddresses = jest.fn<Promise<AddressOption[]>, [string, AbortSignal?]>();
const searchAddresses = jest.fn<Promise<AddressOption[]>, [string, AbortSignal?]>();
const onChange = jest.fn<void, [AddressOption | null]>();

const EIGHT_HOMES = Array.from({ length: 8 }, (_, i) => ({ ...ADDRESSES[1], postnumber: String(i + 1) }));

beforeEach(() => {
  jest.useFakeTimers();
  findAddresses.mockReset().mockResolvedValue(ADDRESSES);
  searchAddresses.mockReset().mockResolvedValue(ADDRESSES.slice(1));
  onChange.mockReset();
});

afterEach(() => {
  jest.useRealTimers();
});

function setUp(): { input: HTMLElement; unmount: () => void } {
  const view = render(
    <AddressSearch id="address" findAddresses={findAddresses} searchAddresses={searchAddresses} onChange={onChange} />,
  );
  return { input: screen.getByRole('combobox', { name: 'Address' }), unmount: view.unmount };
}

/** Lets `ms` pass on the fake clock, settling any answers that arrive. */
async function wait(ms: number): Promise<void> {
  await act(async () => {
    jest.advanceTimersByTime(ms);
  });
}

/**
 * Types into the field and lets the debounce and the minimum interval run out.
 */
async function typeAndWait(input: HTMLElement, value: string): Promise<void> {
  fireEvent.change(input, { target: { value } });
  await wait(SEARCH_DEBOUNCE_MS + MIN_SEARCH_INTERVAL_MS);
}

/** The visible line in the list's place (not the screen-reader announcement). */
function shownMessage(): HTMLElement | null {
  return document.querySelector('#address ~ div p');
}

describe('AddressSearch', () => {
  it('lists every home in a full postcode, whatever its case and spacing', async () => {
    const { input } = setUp();
    await typeAndWait(input, 'cr01jn');
    expect(findAddresses).toHaveBeenCalledWith('CR0 1JN', expect.anything());
    expect(searchAddresses).not.toHaveBeenCalled();
    expect(screen.getAllByRole('option')).toHaveLength(3);
  });

  it('searches addresses with one call for anything that is not a full postcode', async () => {
    const { input } = setUp();
    await typeAndWait(input, 'barclay ro');
    expect(searchAddresses).toHaveBeenCalledTimes(1);
    expect(searchAddresses).toHaveBeenCalledWith('barclay ro', expect.anything());
    expect(findAddresses).not.toHaveBeenCalled();
  });

  it('shows the street line in bold with the postcode under it', async () => {
    const { input } = setUp();
    await typeAndWait(input, 'CR0 1JN');
    const option = screen.getByRole('option', { name: '18 Barclay Road, Croydon, CR0 1JN' });
    expect(within(option).getByText('18 Barclay Road, Croydon')).toHaveProperty('tagName', 'STRONG');
    expect(option).toHaveTextContent('CR0 1JN');
  });

  // Counts the requests one typed address sends, so a fan-out cannot return.
  it('sends one request for an address typed at normal speed, and none to the postcode route', async () => {
    const { input } = setUp();
    const address = '17 barclay road croydon';
    for (let i = 1; i <= address.length; i += 1) {
      fireEvent.change(input, { target: { value: address.slice(0, i) } });
      await wait(180); // ~5.5 keystrokes a second, faster than the debounce
    }
    expect(searchAddresses).not.toHaveBeenCalled();
    await wait(SEARCH_DEBOUNCE_MS);
    expect(searchAddresses).toHaveBeenCalledTimes(1);
    expect(searchAddresses).toHaveBeenCalledWith('17 barclay road croydon', expect.anything());
    expect(findAddresses).not.toHaveBeenCalled();
  });

  it('sends at most one request per pause, never two within the minimum interval', async () => {
    const sentAt: number[] = [];
    searchAddresses.mockImplementation(() => {
      sentAt.push(Date.now());
      return Promise.resolve(ADDRESSES.slice(1));
    });
    const { input } = setUp();
    const address = 'flat 3 high street';
    const pauses = [4, 7, 9, 12, 13, 18]; // pauses after these characters, just past the debounce
    for (let i = 1; i <= address.length; i += 1) {
      fireEvent.change(input, { target: { value: address.slice(0, i) } });
      await wait(pauses.includes(i) ? SEARCH_DEBOUNCE_MS + 10 : 120);
    }
    await wait(MIN_SEARCH_INTERVAL_MS);

    expect(findAddresses).not.toHaveBeenCalled();
    expect(sentAt.length).toBeGreaterThan(1);
    expect(sentAt.length).toBeLessThanOrEqual(pauses.length);
    sentAt.slice(1).forEach((at, i) => expect(at - sentAt[i]).toBeGreaterThanOrEqual(MIN_SEARCH_INTERVAL_MS));
  });

  it('waits for typing to pause, sends nothing under three characters, and marks the field busy', async () => {
    const { input } = setUp();
    fireEvent.change(input, { target: { value: 'ba' } });
    await wait(SEARCH_DEBOUNCE_MS * 2);
    expect(searchAddresses).not.toHaveBeenCalled();
    expect(input).not.toHaveAttribute('aria-busy');

    fireEvent.change(input, { target: { value: 'bar' } });
    await wait(SEARCH_DEBOUNCE_MS - 1);
    fireEvent.change(input, { target: { value: 'barc' } });
    await wait(SEARCH_DEBOUNCE_MS - 1);
    expect(searchAddresses).not.toHaveBeenCalled();
    expect(shownMessage()).toHaveTextContent('Searching…');
    expect(input).toHaveAttribute('aria-busy', 'true');

    await wait(1);
    expect(searchAddresses).toHaveBeenCalledTimes(1);
    expect(searchAddresses).toHaveBeenCalledWith('barc', expect.anything());
    expect(input).not.toHaveAttribute('aria-busy');
  });

  it('never sends text outside the server’s bounds, and caps the field at 64 characters', async () => {
    const { input } = setUp();
    expect(input).toHaveAttribute('maxLength', '64');
    await typeAndWait(input, 'zz');
    await typeAndWait(input, "a'b");
    await typeAndWait(input, '((( )))');
    await typeAndWait(input, 'a'.repeat(65));
    expect(searchAddresses).not.toHaveBeenCalled();
    expect(findAddresses).not.toHaveBeenCalled();

    await typeAndWait(input, 'a'.repeat(64));
    expect(searchAddresses).toHaveBeenCalledTimes(1);
  });

  it('does not ask again for text the last answer already covers', async () => {
    const { input } = setUp();
    await typeAndWait(input, 'barclay rd');
    await typeAndWait(input, 'Barclay Rd,');
    await typeAndWait(input, 'barclay rd ');
    expect(searchAddresses).toHaveBeenCalledTimes(1);
    expect(screen.getAllByRole('option')).toHaveLength(2);

    await typeAndWait(input, 'barclay rda');
    expect(searchAddresses).toHaveBeenCalledTimes(2);
  });

  it('queues newer text behind a search in flight instead of cancelling it, and shows only the newer answer', async () => {
    const signals: AbortSignal[] = [];
    let answerFirst: (homes: AddressOption[]) => void = () => undefined;
    searchAddresses.mockImplementation((q, signal) => {
      signals.push(signal as AbortSignal);
      return signals.length === 1
        ? new Promise<AddressOption[]>((resolve) => {
            answerFirst = resolve;
          })
        : Promise.resolve(ADDRESSES.slice(2));
    });
    const { input } = setUp();
    await typeAndWait(input, 'barclay');
    await typeAndWait(input, 'barclay court');
    // The newer text waits for the answer in flight; nothing is cancelled.
    expect(signals).toHaveLength(1);

    await act(async () => {
      answerFirst(ADDRESSES);
    });
    await wait(MIN_SEARCH_INTERVAL_MS);

    expect(searchAddresses).toHaveBeenLastCalledWith('barclay court', expect.anything());
    expect(signals).toHaveLength(2);
    expect(signals.some((signal) => signal.aborted)).toBe(false);
    expect(screen.getAllByRole('option')).toHaveLength(1);
  });

  it('cancels the search in flight when the field goes away', async () => {
    let signal: AbortSignal | undefined;
    searchAddresses.mockImplementation((q, s) => {
      signal = s;
      return new Promise(() => {});
    });
    const { input, unmount } = setUp();
    await typeAndWait(input, 'barclay');
    expect(signal?.aborted).toBe(false);
    unmount();
    expect(signal?.aborted).toBe(true);
  });

  it('fills the field with the chosen home, and clears the choice once the text is edited', async () => {
    const { input } = setUp();
    await typeAndWait(input, 'CR0 1JN');
    fireEvent.click(screen.getByRole('option', { name: '18 Barclay Road, Croydon, CR0 1JN' }));
    expect(input).toHaveValue('18 Barclay Road, Croydon, CR0 1JN');
    expect(onChange).toHaveBeenLastCalledWith(ADDRESSES[1]);

    fireEvent.change(input, { target: { value: '18 Barclay Road, Croydon, CR0 1J' } });
    expect(onChange).toHaveBeenLastCalledWith(null);
  });

  it('chooses a suggestion from the keyboard', async () => {
    const { input } = setUp();
    await typeAndWait(input, 'barclay');
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    expect(input).toHaveAttribute('aria-activedescendant', 'address-option-1');
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(onChange).toHaveBeenLastCalledWith(ADDRESSES[2]);
    expect(input).toHaveValue('Flat 2 Barclay Court, Croydon, CR0 1JN');
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
  });

  it('says when nothing is found', async () => {
    searchAddresses.mockResolvedValueOnce([]);
    const { input } = setUp();
    await typeAndWait(input, 'nowhere lane');
    expect(shownMessage()).toHaveTextContent('No homes found. Try the postcode');
    expect(screen.queryByRole('button', { name: 'Search again' })).not.toBeInTheDocument();
  });

  it.each([
    ['a missing route', new ApiError(404, 'Something went wrong on our side. Try again in a moment.', undefined, true)],
    ['a server error', new ApiError(500, 'Something went wrong on our side. Try again in a moment.')],
    ['no connection', new ApiError(0, 'We could not reach our data just now. Check your connection and try again.')],
  ])('shows %s as a failure with a working retry, never as “no homes”', async (_name, failure) => {
    searchAddresses.mockRejectedValueOnce(failure);
    const { input } = setUp();
    await typeAndWait(input, 'barclay road');
    expect(shownMessage()).toHaveTextContent(failure.userMessage);
    expect(shownMessage()).not.toHaveTextContent('No homes found');
    expect(screen.queryByRole('option')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Search again' }));
    await wait(MIN_SEARCH_INTERVAL_MS);
    expect(searchAddresses).toHaveBeenCalledTimes(2);
    expect(searchAddresses).toHaveBeenLastCalledWith('barclay road', expect.anything());
    expect(screen.getAllByRole('option')).toHaveLength(2);
    expect(input).toHaveFocus();
  });

  it('keeps the retry button on screen when Tab moves to it', async () => {
    searchAddresses.mockRejectedValueOnce(new ApiError(503, 'Address search is not available right now.'));
    const { input } = setUp();
    input.focus();
    await typeAndWait(input, 'barclay road');
    const retry = screen.getByRole('button', { name: 'Search again' });
    fireEvent.keyDown(input, { key: 'Tab' });
    fireEvent.blur(input, { relatedTarget: retry });
    expect(screen.getByRole('button', { name: 'Search again' })).toBeVisible();
  });

  it('waits quietly when the server asks it to slow down, then searches once more', async () => {
    searchAddresses.mockRejectedValueOnce(new ApiError(429, 'One moment…', undefined, false, 2));
    const { input } = setUp();
    await typeAndWait(input, 'barclay road');
    expect(shownMessage()).toHaveTextContent('One moment…');
    expect(screen.queryByRole('button', { name: 'Search again' })).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(input).toHaveAttribute('aria-busy', 'true');

    await wait(1999 - MIN_SEARCH_INTERVAL_MS);
    expect(searchAddresses).toHaveBeenCalledTimes(1);
    await wait(MIN_SEARCH_INTERVAL_MS + 1);
    expect(searchAddresses).toHaveBeenCalledTimes(2);
    expect(screen.getAllByRole('option')).toHaveLength(2);
  });

  it('asks for the house number or postcode when a street fills the list, even with an outward code typed', async () => {
    searchAddresses.mockResolvedValue(EIGHT_HOMES);
    const { input } = setUp();
    await typeAndWait(input, 'barclay road');
    expect(screen.getAllByRole('option')).toHaveLength(8);
    expect(shownMessage()).toHaveTextContent('Add the house number or postcode to narrow the list');

    await typeAndWait(input, 'barclay road cr0');
    expect(shownMessage()).toHaveTextContent('Add the house number or postcode to narrow the list');

    await typeAndWait(input, '1 barclay road');
    expect(shownMessage()).toBeNull();
  });

  it('shows the server’s message when a postcode has no homes to price, without a retry', async () => {
    findAddresses.mockRejectedValueOnce(new ApiError(404, 'We could not find any homes we can price in ZZ1 1ZZ'));
    const { input } = setUp();
    await typeAndWait(input, 'ZZ1 1ZZ');
    expect(shownMessage()).toHaveTextContent('We could not find any homes we can price in ZZ1 1ZZ');
    expect(screen.queryByRole('button', { name: 'Search again' })).not.toBeInTheDocument();
  });

  it('tells screen readers what settled, not every intermediate state', async () => {
    const { input } = setUp();
    input.focus();
    fireEvent.change(input, { target: { value: 'barclay' } });
    await wait(SEARCH_DEBOUNCE_MS - 1);
    expect(screen.getByRole('status')).toBeEmptyDOMElement();

    await wait(1);
    expect(screen.getByRole('status')).toBeEmptyDOMElement();
    await wait(ANNOUNCE_DELAY_MS);
    expect(screen.getByRole('status')).toHaveTextContent('2 suggestions');
  });
});
