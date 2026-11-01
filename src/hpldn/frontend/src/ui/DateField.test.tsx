import React, { useState } from 'react';
import { fireEvent, render, screen, within } from '@testing-library/react';
import DateField from './DateField';

/**
 * A DateField holding its own ISO value, showing it for assertions.
 */
function Harness({ initial = '2026-09-15' }: { initial?: string }): React.ReactElement {
  const [value, setValue] = useState(initial);
  return (
    <>
      <DateField id="sale" label="Sale date" value={value} onChange={setValue} min="2026-09-15" max="2031-09-14" />
      <output data-testid="iso">{value}</output>
    </>
  );
}

describe('DateField', () => {
  it('shows the day as DD/MM/YYYY in a plain text field, whatever the locale', () => {
    render(<Harness />);
    const input = screen.getByLabelText('Sale date');
    expect(input).toHaveAttribute('type', 'text');
    expect(input).toHaveAttribute('inputMode', 'numeric');
    expect(input).toHaveValue('15/09/2026');
  });

  it('masks typing to DD/MM/YYYY and keeps the form state ISO', () => {
    render(<Harness />);
    const input = screen.getByLabelText('Sale date');
    fireEvent.change(input, { target: { value: '1510' } });
    expect(input).toHaveValue('15/10');
    expect(screen.getByTestId('iso')).toHaveTextContent(/^$/);

    fireEvent.change(input, { target: { value: '15102026' } });
    expect(input).toHaveValue('15/10/2026');
    expect(screen.getByTestId('iso')).toHaveTextContent('2026-10-15');
  });

  it('opens a Monday-first calendar with days before the first allowed one disabled', () => {
    render(<Harness />);
    fireEvent.click(screen.getByRole('button', { name: 'Choose sale date from a calendar' }));

    const dialog = screen.getByRole('dialog', { name: 'Sale date' });
    const weekdays = within(dialog).getAllByRole('columnheader', { hidden: true });
    expect(weekdays[0]).toHaveAttribute('aria-label', 'Monday');
    expect(weekdays[6]).toHaveAttribute('aria-label', 'Sunday');

    expect(within(dialog).getByRole('button', { name: /Monday, 14 September 2026/ })).toBeDisabled();
    expect(within(dialog).getByRole('button', { name: /^Tuesday, 15 September 2026/ })).toBeEnabled();
  });

  it('fills the field from the calendar, then closes it and returns focus to the input', () => {
    render(<Harness />);
    fireEvent.click(screen.getByRole('button', { name: 'Choose sale date from a calendar' }));
    fireEvent.click(screen.getByRole('button', { name: /^Wednesday, 30 September 2026/ }));

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Sale date')).toHaveValue('30/09/2026');
    expect(screen.getByLabelText('Sale date')).toHaveFocus();
    expect(screen.getByTestId('iso')).toHaveTextContent('2026-09-30');
  });

  it('closes the calendar on Esc and on a press outside, returning focus to the input', () => {
    render(
      <>
        <Harness />
        <p>Elsewhere</p>
      </>,
    );
    const toggle = screen.getByRole('button', { name: 'Choose sale date from a calendar' });
    fireEvent.click(toggle);
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Sale date')).toHaveFocus();

    fireEvent.click(toggle);
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    fireEvent.mouseDown(screen.getByText('Elsewhere'));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Sale date')).toHaveFocus();
  });
});
