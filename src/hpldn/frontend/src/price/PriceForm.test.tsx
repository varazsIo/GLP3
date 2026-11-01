import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { ADDRESSES } from '../test/fixtures';
import PriceForm from './PriceForm';

const TODAY = new Date(2026, 8, 14);

function setUp(onSubmit = jest.fn()): jest.Mock {
  render(
    <PriceForm
      findAddresses={jest.fn().mockResolvedValue(ADDRESSES)}
      searchAddresses={jest.fn().mockResolvedValue(ADDRESSES)}
      onSubmit={onSubmit}
      today={TODAY}
    />,
  );
  return onSubmit;
}

describe('PriceForm', () => {
  it('has the address, the sale date and the button, with no Find button or home list', () => {
    const { container } = render(
      <PriceForm findAddresses={jest.fn()} searchAddresses={jest.fn()} onSubmit={jest.fn()} today={TODAY} />,
    );
    expect(screen.getByRole('combobox', { name: 'Address' })).toHaveAttribute(
      'placeholder',
      'Start typing an address or postcode',
    );
    expect(screen.getByLabelText('Sale date')).toHaveValue('15/09/2026');
    expect(screen.getByRole('button', { name: 'Price this home' })).toBeInTheDocument();

    expect(screen.queryByRole('button', { name: /find homes/i })).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Home')).not.toBeInTheDocument();
    expect(container).not.toHaveTextContent(/Find the homes/i);
  });

  it('asks for a home from the suggestions, focusing the address', () => {
    const onSubmit = setUp();
    fireEvent.click(screen.getByRole('button', { name: 'Price this home' }));
    expect(screen.getByText('Choose a home from the suggestions.')).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Address' })).toHaveFocus();
    expect(screen.getByRole('status')).toHaveTextContent('Check 1 field above.');
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('prices the chosen home on the typed UK date', async () => {
    const onSubmit = setUp();
    fireEvent.change(screen.getByRole('combobox', { name: 'Address' }), { target: { value: 'CR0 1JN' } });
    fireEvent.click(await screen.findByRole('option', { name: '18 Barclay Road, Croydon, CR0 1JN' }));
    fireEvent.change(screen.getByLabelText('Sale date'), { target: { value: '15/10/2026' } });
    fireEvent.click(screen.getByRole('button', { name: 'Price this home' }));

    expect(onSubmit).toHaveBeenCalledWith({ ...ADDRESSES[1], saleDate: '2026-10-15' });
  });

  it('puts a date problem on the date field in DD/MM/YYYY', async () => {
    const onSubmit = setUp();
    fireEvent.change(screen.getByRole('combobox', { name: 'Address' }), { target: { value: 'CR0 1JN' } });
    fireEvent.click(await screen.findByRole('option', { name: '18 Barclay Road, Croydon, CR0 1JN' }));

    fireEvent.change(screen.getByLabelText('Sale date'), { target: { value: '14/09/2026' } });
    fireEvent.click(screen.getByRole('button', { name: 'Price this home' }));
    expect(screen.getByText('Choose a date from 15/09/2026.')).toBeInTheDocument();
    expect(screen.getByLabelText('Sale date')).toHaveFocus();

    fireEvent.change(screen.getByLabelText('Sale date'), { target: { value: '31/02/2027' } });
    fireEvent.click(screen.getByRole('button', { name: 'Price this home' }));
    // A well-formed day that does not exist is named as such.
    expect(screen.getByText('There is no 31/02/2027. Check the day and month.')).toBeInTheDocument();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('shows the price is on its way while pending', () => {
    const onSubmit = jest.fn();
    render(
      <PriceForm
        findAddresses={jest.fn()}
        searchAddresses={jest.fn()}
        onSubmit={onSubmit}
        today={TODAY}
        pending
      />,
    );
    const button = screen.getByRole('button', { name: 'Price this home' });
    expect(button).toHaveAttribute('aria-busy', 'true');
    // Still live, so a changed search can replace the one in flight.
    expect(button).not.toBeDisabled();
  });
});
