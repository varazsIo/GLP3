import { fireEvent, render, screen } from '@testing-library/react';
import { caretFor, YieldCalculator } from './YieldCalculator';

const area = { key: 'a', description: 'the area', purchasePrice: 425_000, monthlyRent: 2_632 };

/** Edits a field as the browser does: new text, the caret where the edit left it, then an input event. */
function edit(input: HTMLInputElement, text: string, caret: number, inputType = 'insertText'): void {
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, text);
  input.setSelectionRange(caret, caret);
  fireEvent.input(input, { inputType });
}

describe('caretFor', () => {
  it('keeps the caret after the same digits once the text is regrouped', () => {
    expect(caretFor(',632', 0, '632')).toBe(0);
    expect(caretFor('2632', 1, '2,632')).toBe(1);
    expect(caretFor('2,6352', 5, '26,352')).toBe(5);
    expect(caretFor('1425,000', 1, '1,425,000')).toBe(1);
    expect(caretFor('0,632', 1, '632')).toBe(0);
  });
});

describe('money fields', () => {
  it('keep the caret at the start when the rent’s first digit is deleted', () => {
    render(<YieldCalculator home={null} area={area} />);
    const rent = screen.getByLabelText('Monthly rent') as HTMLInputElement;
    expect(rent.value).toBe('2,632');
    rent.focus();
    // Backspace after the "2" and Delete before it both leave ",632" with the caret at 0.
    edit(rent, ',632', 0);
    expect(rent.value).toBe('632');
    expect(rent.selectionStart).toBe(0);
    expect(rent.selectionEnd).toBe(0);
  });

  it('take the digit before a separator along with it on Backspace', () => {
    render(<YieldCalculator home={null} area={area} />);
    const rent = screen.getByLabelText('Monthly rent') as HTMLInputElement;
    const price = screen.getByLabelText('Purchase price') as HTMLInputElement;
    rent.focus();
    // Backspace just after the comma in "2,|632".
    edit(rent, '2632', 1, 'deleteContentBackward');
    expect(rent.value).toBe('632');
    expect(rent.selectionStart).toBe(0);
    price.focus();
    // Backspace just after the second comma in "425,|000".
    edit(price, '425000', 3, 'deleteContentBackward');
    expect(price.value).toBe('42,000');
    expect(price.selectionStart).toBe(2);
  });

  it('leave a separator in place on Delete', () => {
    render(<YieldCalculator home={null} area={area} />);
    const rent = screen.getByLabelText('Monthly rent') as HTMLInputElement;
    rent.focus();
    // Delete just before the comma in "2|,632".
    edit(rent, '2632', 1, 'deleteContentForward');
    expect(rent.value).toBe('2,632');
    expect(rent.selectionStart).toBe(1);
  });

  it('keep the caret after a digit typed in front of a figure that gains a separator', () => {
    render(<YieldCalculator home={null} area={area} />);
    const price = screen.getByLabelText('Purchase price') as HTMLInputElement;
    price.focus();
    edit(price, '1425,000', 1);
    expect(price.value).toBe('1,425,000');
    expect(price.selectionStart).toBe(1);
  });
});
