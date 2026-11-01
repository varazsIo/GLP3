import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ANCHORED_PRICE, ESTIMATED_PRICE, NEARBY_SCHOOLS, PRICE } from '../test/fixtures';
import PriceResultCard from './PriceResultCard';

describe('PriceResultCard', () => {
  it('reads like a listing: address, price and range, then type, floor area, rooms and energy rating', () => {
    render(<PriceResultCard result={PRICE} status="ready" />);
    expect(screen.getByRole('heading', { name: '18 Barclay Road, Croydon, CR0 1JN' })).toBeInTheDocument();
    expect(
      screen.getByText(
        (_, element) => element?.tagName === 'SPAN' && element.textContent === 'Price if sold on 15 September 2026',
      ),
    ).toBeInTheDocument();
    expect(screen.getByText('15 September 2026')).toBeInTheDocument();
    expect(screen.getByText('£244,000')).toBeInTheDocument();
    expect(screen.getByText('£189,000')).toBeInTheDocument();
    expect(screen.getByText('£277,000')).toBeInTheDocument();

    const values = screen.getAllByRole('definition').map((definition) => definition.textContent);
    const pairs = screen.getAllByRole('term').map((term, index) => term.textContent + ': ' + values[index]);
    expect(pairs).toEqual(['Type: Flat', 'Floor area: 45 m²', 'Rooms: 2', 'Energy rating: C']);

    expect(screen.getByText('Last sold')).toBeInTheDocument();
    expect(screen.getByText(/£260,000 in June 2026/)).toBeInTheDocument();
    // The band speaks for itself; the range has no heading.
    expect(screen.queryByText('Price range')).not.toBeInTheDocument();
    expect(screen.getByText('Based on sales recorded up to July 2026')).toBeInTheDocument();
  });

  it('hides rooms and the energy rating when the records do not hold them', () => {
    render(<PriceResultCard result={{ ...PRICE, habitableRooms: null, energyRating: null }} status="ready" />);
    expect(screen.getAllByRole('term').map((term) => term.textContent)).toEqual(['Type', 'Floor area']);
    expect(screen.queryByText('Rooms')).not.toBeInTheDocument();
  });

  it('labels an estimated home’s size and rooms, hides its energy rating and says why', () => {
    render(<PriceResultCard result={ESTIMATED_PRICE} status="ready" />);
    const values = screen.getAllByRole('definition').map((definition) => definition.textContent);
    const pairs = screen.getAllByRole('term').map((term, index) => term.textContent + ': ' + values[index]);
    expect(pairs).toEqual(['Type: Detached house', 'Floor area (est.): 331 m²', 'Rooms (est.): 9']);
    expect(
      screen.getByText(
        'No energy certificate on record — size estimated from 6 detached houses on this street; the range is wider.',
      ),
    ).toBeInTheDocument();
    expect(screen.getByText('£4,667,000')).toBeInTheDocument();
  });

  it('handles an estimated home whose size could not be estimated', () => {
    render(
      <PriceResultCard
        result={{ ...ESTIMATED_PRICE, floorAreaSqm: null, habitableRooms: null, estimatedFrom: null }}
        status="ready"
      />,
    );
    expect(screen.getAllByRole('term').map((term) => term.textContent)).toEqual(['Type']);
    expect(screen.getByText(/size could not be estimated; the range is wider/)).toBeInTheDocument();
  });

  it('shows no estimate note for a home with a certificate', () => {
    render(<PriceResultCard result={PRICE} status="ready" />);
    expect(screen.queryByText(/No energy certificate on record/)).not.toBeInTheDocument();
  });

  it('carries no accuracy figure or caveat', () => {
    const { container } = render(<PriceResultCard result={PRICE} status="ready" />);
    expect(container).not.toHaveTextContent(/within 10%/);
    expect(container).not.toHaveTextContent(/screening/i);
    expect(container).not.toHaveTextContent(/not a valuation/i);
    expect(container).not.toHaveTextContent(/Sales we compared it with/);
  });

  it('says when there is no earlier sale, and leaves out a range that was not measured', () => {
    render(<PriceResultCard result={{ ...PRICE, lastSale: null, low: null, high: null }} status="ready" />);
    expect(screen.getByText('No earlier sale on record')).toBeInTheDocument();
    expect(screen.queryByText('Last sold')).not.toBeInTheDocument();
    expect(screen.queryByText('Price range')).not.toBeInTheDocument();
  });

  it('offers the detailed analysis and the rental sums as next steps, and no area or nearby buttons', () => {
    const onWorkOutYield = jest.fn();
    render(<PriceResultCard result={PRICE} status="ready" onWorkOutYield={onWorkOutYield} loadNearby={jest.fn()} />);
    expect(screen.getAllByRole('button').map((button) => button.textContent)).toEqual([
      'Detailed Analysis',
      'Work out rental yield',
    ]);
    fireEvent.click(screen.getByRole('button', { name: 'Work out rental yield' }));
    expect(onWorkOutYield).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('button', { name: 'See the area' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Nearby properties' })).not.toBeInTheDocument();
  });

  it('says when the price rests mainly on the home’s own recent sale', () => {
    render(<PriceResultCard result={ANCHORED_PRICE} status="ready" />);
    expect(screen.getByText('Based mainly on its sale for £2,500,000 in April 2026.')).toBeInTheDocument();
  });

  it('says nothing about an anchor when the model priced the home', () => {
    const { container } = render(<PriceResultCard result={PRICE} status="ready" />);
    expect(container).not.toHaveTextContent(/Based mainly on its sale/);
    const legacy = { ...ANCHORED_PRICE, anchoredOnLastSale: undefined };
    const { container: older } = render(<PriceResultCard result={legacy} status="ready" />);
    expect(older).not.toHaveTextContent(/Based mainly on its sale/);
  });

  it('renders nothing before a request', () => {
    const { container } = render(<PriceResultCard result={null} status="idle" />);
    expect(container).toBeEmptyDOMElement();
  });

  it('does not compare the chosen day with the earliest one', () => {
    const { container } = render(<PriceResultCard result={{ ...PRICE, saleDate: '2026-12-01' }} status="ready" />);
    expect(container).not.toHaveTextContent(/than a sale on|Same as a sale on|not a forecast/);
  });

  it('shows a placeholder card while the price is worked out', () => {
    render(<PriceResultCard result={null} status="loading" />);
    const card = screen.getByRole('status');
    expect(card).toHaveAttribute('aria-busy', 'true');
    expect(screen.getByRole('heading', { name: 'Working out the price…' })).toBeInTheDocument();
  });

  it('offers to change the search after a failure the same request will not fix', () => {
    const onRetry = jest.fn();
    const onEdit = jest.fn();
    render(<PriceResultCard result={null} status="error" errorMessage="No can do." onRetry={onRetry} onEdit={onEdit} />);
    fireEvent.click(screen.getByRole('button', { name: 'Change the search' }));
    expect(onEdit).toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument();
  });

  it('hands the detailed analysis a schools loader that fetches each home once', async () => {
    const loadSchools = jest.fn().mockResolvedValue(NEARBY_SCHOOLS);
    const { rerender } = render(<PriceResultCard result={PRICE} status="ready" loadSchools={loadSchools} />);
    for (let turn = 0; turn < 2; turn += 1) {
      fireEvent.click(screen.getByRole('button', { name: 'Detailed Analysis' }));
        await waitFor(() => expect(screen.getByRole('region', { name: 'Schools' })).not.toHaveAttribute('aria-busy'));
      expect(screen.getByRole('region', { name: 'Schools' })).toBeInTheDocument();
      fireEvent.click(screen.getByRole('button', { name: 'Back to the price' }));
    }
    expect(loadSchools).toHaveBeenCalledTimes(1);
    rerender(<PriceResultCard result={ESTIMATED_PRICE} status="ready" loadSchools={loadSchools} />);
    fireEvent.click(screen.getByRole('button', { name: 'Detailed Analysis' }));
      await waitFor(() => expect(screen.getByRole('region', { name: 'Schools' })).not.toHaveAttribute('aria-busy'));
    expect(loadSchools).toHaveBeenCalledTimes(2);
    expect(loadSchools.mock.calls[1][0]).toEqual(ESTIMATED_PRICE.address);
  });
});
