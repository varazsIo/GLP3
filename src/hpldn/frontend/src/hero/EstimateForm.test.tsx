import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import EstimateForm from './EstimateForm';

const COVERED = new Set(['SW11', 'SW1', 'BR']);

function setUp(onSubmit = jest.fn(), serverPostcodeError: string | null = null): jest.Mock {
  render(<EstimateForm onSubmit={onSubmit} coveredDistricts={COVERED} serverPostcodeError={serverPostcodeError} />);
  return onSubmit;
}

function submit(): void {
  fireEvent.click(screen.getByRole('button', { name: 'Estimate value' }));
}

describe('EstimateForm', () => {
  it('shows both errors on an empty submit', () => {
    const onSubmit = setUp();
    submit();
    expect(screen.getByText('Enter a London postcode.')).toBeInTheDocument();
    expect(screen.getByText('Floor area must be at least 15 m².')).toBeInTheDocument();
    expect(screen.getByText('Check 2 fields above.')).toBeInTheDocument();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('clears each error as its field is corrected, then submits', () => {
    const onSubmit = setUp();
    submit();

    fireEvent.change(screen.getByLabelText('Postcode'), { target: { value: 'SW11 3AA' } });
    expect(screen.queryByText('Enter a London postcode.')).not.toBeInTheDocument();
    expect(screen.getByText('Check 1 field above.')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('Floor area'), { target: { value: '72' } });
    expect(screen.queryByText('Floor area must be at least 15 m².')).not.toBeInTheDocument();
    expect(screen.queryByText(/Check \d fields? above/)).not.toBeInTheDocument();

    submit();
    expect(onSubmit).toHaveBeenCalledWith({
      district: 'SW11',
      postcode: 'SW11 3AA',
      propertyType: 'flat',
      bedrooms: 2,
      floorAreaSqm: 72,
      // Defaults to tomorrow, as the price form's date does.
      saleDate: expect.stringMatching(/^\d{4}-\d{2}-\d{2}$/),
    });
  });

  it('re-checks a field as it is left after a submit', () => {
    setUp();
    submit();
    const floorArea = screen.getByLabelText('Floor area');
    fireEvent.change(floorArea, { target: { value: '5' } });
    expect(screen.queryByText('Floor area must be at least 15 m².')).not.toBeInTheDocument();
    fireEvent.blur(floorArea);
    expect(screen.getByText('Floor area must be at least 15 m².')).toBeInTheDocument();
  });

  it('does not check a field on leaving it before the first submit', () => {
    setUp();
    fireEvent.blur(screen.getByLabelText('Postcode'));
    expect(screen.queryByText('Enter a London postcode.')).not.toBeInTheDocument();
  });

  it('hides a server postcode error once the postcode is edited', () => {
    setUp(jest.fn(), 'We do not cover TN16 yet. Try a London postcode.');
    expect(screen.getByText('We do not cover TN16 yet. Try a London postcode.')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Postcode'), { target: { value: 'SW11 3AA' } });
    expect(screen.queryByText('We do not cover TN16 yet. Try a London postcode.')).not.toBeInTheDocument();
  });

  it('shows the estimate is on its way while pending', () => {
    const onSubmit = jest.fn();
    render(<EstimateForm onSubmit={onSubmit} coveredDistricts={COVERED} pending />);
    const button = screen.getByRole('button', { name: 'Estimate value' });
    expect(button).toHaveAttribute('aria-busy', 'true');
    expect(button).not.toBeDisabled();
  });

  it('marks an invalid postcode on the field itself', () => {
    setUp();
    fireEvent.change(screen.getByLabelText('Postcode'), { target: { value: 'not a postcode' } });
    submit();
    expect(screen.getByLabelText('Postcode')).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByText('Invalid UK Postcode')).toBeInTheDocument();
  });
});
