import React, { useState } from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import Combobox, { ANNOUNCE_DELAY_MS } from './Combobox';

const FRUIT = ['Apple', 'Apricot', 'Banana'];

/**
 * A combobox over a fixed list, recording what was chosen.
 */
function Harness({
  onSelect = () => {},
  message,
  onRetry,
  busy,
}: {
  onSelect?: (fruit: string) => void;
  message?: string;
  onRetry?: () => void;
  busy?: boolean;
}): React.ReactElement {
  const [text, setText] = useState('');
  return (
    <Combobox<string>
      id="fruit"
      label="Fruit"
      inputValue={text}
      onInputChange={setText}
      options={FRUIT.filter((fruit) => fruit.toLowerCase().startsWith(text.toLowerCase()))}
      getOptionKey={(fruit) => fruit}
      getOptionLabel={(fruit) => fruit}
      onSelect={(fruit) => {
        setText(fruit);
        onSelect(fruit);
      }}
      message={message}
      onRetry={onRetry}
      busy={busy}
    />
  );
}

describe('Combobox', () => {
  it('carries the combobox ARIA attributes, closed to start', () => {
    render(<Harness />);
    const input = screen.getByRole('combobox', { name: 'Fruit' });
    expect(input).toHaveAttribute('aria-autocomplete', 'list');
    expect(input).toHaveAttribute('aria-expanded', 'false');
    expect(input).toHaveAttribute('aria-controls', 'fruit-listbox');
    expect(input).not.toHaveAttribute('aria-activedescendant');
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
  });

  it('opens on typing and moves the active option with the arrow keys, wrapping round', () => {
    render(<Harness />);
    const input = screen.getByRole('combobox', { name: 'Fruit' });
    fireEvent.change(input, { target: { value: 'a' } });
    expect(input).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getAllByRole('option')).toHaveLength(2);

    fireEvent.keyDown(input, { key: 'ArrowDown' });
    expect(input).toHaveAttribute('aria-activedescendant', 'fruit-option-0');
    expect(screen.getByRole('option', { name: 'Apple' })).toHaveAttribute('aria-selected', 'true');

    fireEvent.keyDown(input, { key: 'ArrowDown' });
    expect(input).toHaveAttribute('aria-activedescendant', 'fruit-option-1');
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    expect(input).toHaveAttribute('aria-activedescendant', 'fruit-option-0');
    fireEvent.keyDown(input, { key: 'ArrowUp' });
    expect(input).toHaveAttribute('aria-activedescendant', 'fruit-option-1');
  });

  it('chooses the active option with Enter and closes', () => {
    const chosen: string[] = [];
    render(<Harness onSelect={(fruit) => chosen.push(fruit)} />);
    const input = screen.getByRole('combobox', { name: 'Fruit' });
    fireEvent.change(input, { target: { value: 'ap' } });
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    fireEvent.keyDown(input, { key: 'Enter' });

    expect(chosen).toEqual(['Apricot']);
    expect(input).toHaveValue('Apricot');
    expect(input).toHaveAttribute('aria-expanded', 'false');
  });

  it('ignores Enter with nothing active, and closes on Esc without choosing', () => {
    const chosen: string[] = [];
    render(<Harness onSelect={(fruit) => chosen.push(fruit)} />);
    const input = screen.getByRole('combobox', { name: 'Fruit' });
    fireEvent.change(input, { target: { value: 'b' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(chosen).toEqual([]);

    fireEvent.keyDown(input, { key: 'Escape' });
    expect(input).toHaveAttribute('aria-expanded', 'false');
    expect(input).toHaveValue('b');
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    expect(input).toHaveAttribute('aria-expanded', 'true');
  });

  it('chooses an option with a press, keeping focus in the input', () => {
    const chosen: string[] = [];
    render(<Harness onSelect={(fruit) => chosen.push(fruit)} />);
    const input = screen.getByRole('combobox', { name: 'Fruit' });
    input.focus();
    fireEvent.change(input, { target: { value: 'ban' } });
    const option = screen.getByRole('option', { name: 'Banana' });
    expect(fireEvent.mouseDown(option)).toBe(false);
    fireEvent.click(option);

    expect(chosen).toEqual(['Banana']);
    expect(input).toHaveFocus();
  });

  it('shows a message in place of the list at once, and announces it once it has settled', () => {
    jest.useFakeTimers();
    try {
      render(<Harness message="Searching…" />);
      const input = screen.getByRole('combobox', { name: 'Fruit' });
      fireEvent.change(input, { target: { value: 'zz' } });
      expect(screen.getByText('Searching…')).toBeVisible();
      expect(input).toHaveAttribute('aria-expanded', 'false');
      expect(screen.getByRole('status')).toBeEmptyDOMElement();

      act(() => {
        jest.advanceTimersByTime(ANNOUNCE_DELAY_MS);
      });
      expect(screen.getByRole('status')).toHaveTextContent('Searching…');
    } finally {
      jest.useRealTimers();
    }
  });

  it('announces how many suggestions there are, and has no live region while closed', () => {
    jest.useFakeTimers();
    try {
      render(<Harness />);
      expect(screen.queryByRole('status')).not.toBeInTheDocument();
      fireEvent.change(screen.getByRole('combobox', { name: 'Fruit' }), { target: { value: 'ap' } });
      act(() => {
        jest.advanceTimersByTime(ANNOUNCE_DELAY_MS);
      });
      expect(screen.getByRole('status')).toHaveTextContent('2 suggestions');
    } finally {
      jest.useRealTimers();
    }
  });

  it('marks the input busy when asked', () => {
    const { rerender } = render(<Harness busy />);
    expect(screen.getByRole('combobox', { name: 'Fruit' })).toHaveAttribute('aria-busy', 'true');
    rerender(<Harness />);
    expect(screen.getByRole('combobox', { name: 'Fruit' })).not.toHaveAttribute('aria-busy');
  });

  it('offers a retry under the message that a press or Tab and Enter reach, keeping the list open', () => {
    const onRetry = jest.fn();
    render(<Harness message="Something went wrong" onRetry={onRetry} />);
    const input = screen.getByRole('combobox', { name: 'Fruit' });
    input.focus();
    fireEvent.change(input, { target: { value: 'zz' } });

    const retry = screen.getByRole('button', { name: 'Try again' });
    expect(fireEvent.mouseDown(retry)).toBe(false);
    fireEvent.click(retry);
    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(input).toHaveFocus();

    fireEvent.keyDown(input, { key: 'Tab' });
    retry.focus();
    expect(retry).toHaveFocus();
    expect(screen.getByRole('button', { name: 'Try again' })).toBeVisible();
  });
});
