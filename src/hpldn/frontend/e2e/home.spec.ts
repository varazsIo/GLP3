/**
 * End-to-end checks of the home page's price and estimate journeys against the
 * local stack: production's nginx in front of the API, the reCAPTCHA gate on.
 * Every gated call goes through the real pass (earned with the fake Google's
 * `e2e-ok`, `fixtures.ts`) and nginx's limits, as a fresh visitor per test.
 *
 * `E2E_ADDRESS` is the text typed into the address search (the first
 * suggestion is chosen); `E2E_POSTCODE` is the details form's postcode.
 */
import { expect, test } from './fixtures';
import type { Page, Response } from '@playwright/test';

const ADDRESS_QUERY = process.env.E2E_ADDRESS ?? '18 Barclay Road';
const ESTIMATE_POSTCODE = process.env.E2E_POSTCODE ?? 'CR0 1JN';

/** Waits for the next answer to an `/api` call, e.g. `POST /price`. */
function apiResponse(page: Page, method: string, path: string): Promise<Response> {
  return page.waitForResponse(
    (response) => response.request().method() === method && new URL(response.url()).pathname === '/api' + path,
  );
}

/** Formats a day as the date fields show it, DD/MM/YYYY. */
function ukDate(day: Date): string {
  const dd = String(day.getDate()).padStart(2, '0');
  const mm = String(day.getMonth() + 1).padStart(2, '0');
  return dd + '/' + mm + '/' + day.getFullYear();
}

/**
 * Today in the browser's clock, and the first day the forms accept: they price
 * a sale from tomorrow onwards, so today itself is rejected.
 */
async function saleDayFromToday(page: Page): Promise<{ today: Date; saleDay: Date }> {
  const [y, m, d] = await page.evaluate(() => {
    const now = new Date();
    return [now.getFullYear(), now.getMonth(), now.getDate()];
  });
  return { today: new Date(y, m, d), saleDay: new Date(y, m, d + 1) };
}

/** Searches for `ADDRESS_QUERY`, picks the first suggestion, sets the date and presses "Price this home". */
async function priceHome(page: Page): Promise<{ status: number; saleDay: Date }> {
  const form = page.getByRole('form', { name: 'Home to price' });
  const { saleDay } = await saleDayFromToday(page);

  const search = apiResponse(page, 'GET', '/addresses/search');
  await form.getByRole('combobox', { name: 'Address' }).fill(ADDRESS_QUERY);
  expect((await search).status()).toBe(200);
  await page.getByRole('option').first().click();

  const dateInput = form.getByRole('textbox', { name: 'Sale date' });
  await dateInput.fill(ukDate(saleDay));
  await expect(dateInput).toHaveValue(ukDate(saleDay));

  const price = apiResponse(page, 'POST', '/price');
  await form.getByRole('button', { name: 'Price this home' }).click();
  return { status: (await price).status(), saleDay };
}

test.beforeEach(async ({ page }) => {
  await page.goto('/');
});

test('GET /areas answers 200 once the page has loaded', async ({ page }) => {
  // Let the first load's call finish, then reload so the listener sees only the new one.
  await page.waitForLoadState('networkidle');
  const areas = apiResponse(page, 'GET', '/areas');
  await page.reload({ waitUntil: 'load' });
  const response = await areas;

  expect(response.status()).toBe(200);
  const body = await response.json();
  expect(Array.isArray(body.areas)).toBe(true);
  expect(body.areas.length).toBeGreaterThan(0);
  await expect(page.getByRole('heading', { level: 1, name: 'What’s a London home worth?' })).toBeVisible();
});

test('pricing a searched address from today renders the price card', async ({ page }) => {
  const { status, saleDay } = await priceHome(page);
  expect(status).toBe(200);

  const heading = page.locator('#result-heading');
  await expect(heading).toContainText(/barclay road/i);
  const card = page.locator('div').filter({ has: heading }).last();

  await expect(card.getByText('Price if sold on')).toBeVisible();
  const longDate = saleDay.toLocaleDateString('en-GB', { day: 'numeric', month: 'long', year: 'numeric' });
  await expect(card.getByText(longDate)).toBeVisible();
  await expect(card.locator('strong').first()).toHaveText(/^£[\d,]+$/);
  await expect(card.getByRole('definition').first()).toBeVisible();
  await expect(card.getByText(/Based on sales recorded up to/)).toBeVisible();
  await expect(card.getByRole('button', { name: 'Detailed Analysis' })).toBeVisible();
});

test('Detailed Analysis loads nearby sales and schools', async ({ page }) => {
  const { status } = await priceHome(page);
  expect(status).toBe(200);
  const analysisButton = page.getByRole('button', { name: 'Detailed Analysis' });
  await expect(analysisButton).toBeVisible();

  const nearby = apiResponse(page, 'POST', '/nearby');
  const schools = apiResponse(page, 'POST', '/schools');
  await analysisButton.click();
  const [nearbyResponse, schoolsResponse] = await Promise.all([nearby, schools]);
  expect(nearbyResponse.status()).toBe(200);
  expect(schoolsResponse.status()).toBe(200);
  const nearbyBody = await nearbyResponse.json();
  const schoolsBody = await schoolsResponse.json();

  await expect(page.getByText('Detailed Analysis', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Back to the price' })).toBeVisible();
  await expect(page.getByRole('heading', { level: 3, name: 'Nearby' })).toBeVisible();
  await expect(page.getByRole('alert')).toHaveCount(0);

  // Houses nearby: one row per sale the API returned, each with an address and price.
  const sales = page.getByRole('region', { name: 'Nearby sales' });
  await expect(sales).toBeVisible();
  if (nearbyBody.sales.length > 0) {
    await expect(sales.getByText(/^What the nearest homes sold for/)).toBeVisible();
    const rows = sales.getByRole('listitem');
    await expect(rows).toHaveCount(nearbyBody.sales.length);
    await expect(rows.first()).toContainText(/£[\d,]+/);
    await expect(rows.first()).toContainText(/Sold /);
  } else {
    await expect(sales.getByText(/^No sales near this home/)).toBeVisible();
  }

  // Schools nearby: every primary and secondary school, each linking to its Ofsted report.
  const schoolSection = page.getByRole('region', { name: 'Schools' });
  await expect(schoolSection).toBeVisible();
  const schoolCount = schoolsBody.primary.length + schoolsBody.secondary.length;
  if (schoolCount > 0) {
    const list = schoolSection.getByRole('list', { name: 'Nearby schools' });
    await expect(list.getByRole('listitem')).toHaveCount(schoolCount);
    await expect(list.getByRole('link').first()).toHaveAttribute('href', /ofsted/i);
  } else {
    await expect(schoolSection.getByText(/^No rated state schools/)).toBeVisible();
  }
});

test('Estimate from details renders the estimate card', async ({ page }) => {
  await page.getByRole('button', { name: /Estimate from details/ }).click();
  const form = page.getByRole('form', { name: 'Property details' });
  await expect(form).toBeVisible();

  await form.getByRole('textbox', { name: 'Postcode' }).fill(ESTIMATE_POSTCODE);
  await form.getByRole('combobox', { name: 'Property type' }).selectOption({ index: 0 });
  await form.getByRole('combobox', { name: 'Bedrooms' }).selectOption('2');
  await form.getByRole('spinbutton', { name: 'Floor area' }).fill('72');

  const estimate = apiResponse(page, 'POST', '/estimate');
  await form.getByRole('button', { name: 'Estimate value' }).click();
  expect((await estimate).status()).toBe(200);

  const heading = page.locator('#result-heading');
  await expect(heading).toContainText(ESTIMATE_POSTCODE);
  const card = page.locator('div').filter({ has: heading }).last();
  await expect(card.getByText('Estimated value')).toBeVisible();
  await expect(card.locator('strong').first()).toHaveText(/^£[\d,]+$/);
  await expect(card.getByText('72 m², 2 bedrooms')).toBeVisible();
});
