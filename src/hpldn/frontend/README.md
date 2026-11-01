# HPLDN frontend

The React + TypeScript client for HPLDN. One path leads the page: **search an address →
pick a sale date → see a price card** for that home on that day, with its range, specs
and last sale. A home the search cannot find can be estimated from its details instead
(postcode, type, bedrooms, floor area). Area context (a map or a ranked list) and a
rental yield calculator sit one step away, followed by explanatory and legal pages.

Prices are a guide, **not a valuation**, a survey or financial advice. The home page says
so once, in the footer; the AVM and legal pages say it in full.

Scaffolded from Create React App's TypeScript template (`react-scripts` 5.0.1, React 19,
TypeScript 4.9). It is a self-contained npm project; nothing here is packaged into the
`hpldn` Python wheel.

## Contents

- [Running it](#running-it)
- [Live data](#live-data)
- [Pages and routing](#pages-and-routing)
- [Source layout](#source-layout)
- [Directories](#directories)
  - [`area/`](#area) · [`compare/`](#compare) · [`content/`](#content) · [`data/`](#data)
  - [`estimate/`](#estimate) · [`hero/`](#hero) · [`layout/`](#layout) · [`legal/`](#legal)
  - [`lib/`](#lib) · [`pages/`](#pages) · [`price/`](#price) · [`styles/`](#styles) · [`test/`](#test)
  - [`ui/`](#ui) · [`yield/`](#yield)
- [Conventions](#conventions)
- [Accessibility and motion](#accessibility-and-motion)
- [Tests](#tests)

## Running it

All commands run from `src/hpldn/frontend/`:

```bash
npm install                            # once, and after any package.json change
npm start                              # dev server on http://localhost:3000, /api proxied to the local stack
npm run build                          # production bundle into build/ (gitignored)
npm run build:prod                     # the bundle deploy.sh ships: no source maps, stripped by scripts/strip-build.mjs
CI=true npm test -- --watchAll=false   # the unit tests, single run
npm run e2e                            # Playwright end-to-end tests (e2e/), against the local stack
npx tsc --noEmit                       # type check only
```

Start the local stack first: `uv run python -m hpldn.backend.cloud.local up` from the
repository root runs production's nginx, the guard daemon and the API on localhost (see
[`backend/api/README.md`](../backend/api/README.md#local-stack)).
`npm run eject` is irreversible; do not run it without deciding to own the build
configuration.

## Live data

Every figure on the page comes from the read API, which computes it from
`production.london_houses`:

| When | Call | Used by |
| --- | --- | --- |
| Once, when the first page with figures opens | `GET /api/areas` → `{ window, areas }` | `data/AreasProvider.tsx`, wrapping every page and kept across page changes, used by the home and AVM pages: the area search, map, ranked list, stat tiles, the yield calculator's area figures and every data-window sentence |
| Typing a full postcode | `GET /api/addresses?postcode=` → `AddressOption[]` | `price/AddressSearch.tsx`: every home in the postcode |
| Typing anything else (3–64 characters) | `GET /api/addresses/search?q=&limit=8` → `AddressOption[]`, one request per typing pause | `price/AddressSearch.tsx`: suggested homes, best match first, from `production.address_search` on the server. Sent 300 ms after typing pauses and at least 400 ms after the previous search, one in flight at a time, and not again for text the last answer covers. `200 []` reads "No homes found"; a missing route, a 5xx or no connection is a failure with "Search again"; a 429 reads "One moment…" and searches once more after `Retry-After`. There is no browser-side fallback |
| On "Price this home" | `POST /api/price` → `PriceResult` | `pages/HomePage.tsx`: the price card in the result slot, Areas' district and the yield calculator's "This home" |
| On "Nearby properties" (price card) | `POST /api/nearby` → `NearbySales` | `price/NearbyProperties.tsx`: the nearest sales in the latest 12 months with their comparable-sales figures, fetched on first open |
| On "Detailed Analysis" (price card) | `POST /api/schools` → `NearbySchools` | `price/NearbySchools.tsx`, in `price/DetailedAnalysis.tsx`'s Schools section: the three nearest primary and secondary schools within 3 km with the Ofsted rating in force today, each linking to its report; fetched once per home (`PriceResultCard` caches it) |
| On "Estimate value" | `POST /api/estimate` → `EstimateResult` | `pages/HomePage.tsx`: the estimate card in the same slot, and the same follow-ons |

`lib/api.ts` is the only module that calls `fetch`. It sends JSON, gives up after 10 s
(30 s for a price, whose first call after the server starts can wait for the figures to
load), and turns any failure into an `ApiError` carrying a sentence safe to show a
landlord (a 404, 422 or 503 whose `detail` is a sentence carries the server's own words,
e.g. "We do not cover ZZ9 yet" or "Choose a sale date from 15/09/2026 onwards"; a
validation payload never does).

- Areas that are loading or failed show a `DataNotice`; only the first section waiting
  on them offers a retry.
- A price or estimate in flight shows "Working out…"; a failure shows the problem and a
  "Try again" that re-sends the last request; a new request of either kind cancels one
  still in flight, and only the latest answer is shown.
- Address suggestions wait 300 ms after typing stops (`SEARCH_DEBOUNCE_MS`), and a new
  search cancels the last.
- The details form checks postcode shape and coverage against the areas `/api/areas`
  returned before calling the server.

Response shapes are defined in `data/types.ts`, and the backend's schemas match them
field for field (a backend test diffs the two). Nothing on the home page names a model,
table or column.

### The proxy and the API key

The browser only calls same-origin `/api/*` and never holds the API key.

- **Locally**, `src/setupProxy.js` (loaded by `npm start`) forwards the routes production
  exposes (`/api/session`, `/api/session/config`, `/api/areas`,
  `/api/areas/{code}[/comparables]`, `/api/inflation`, `/api/estimate`, `/api/addresses`,
  `/api/addresses/search`, `/api/price`, `/api/nearby`, `/api/schools`), unchanged, to the
  local stack's nginx at `HPLDN_EDGE_URL` (default `http://127.0.0.1:8080`). That nginx
  runs production's `hpldn.conf`: it strips `/api`, adds the API key and applies every
  rate limit and ban. The proxy only sets `CF-Connecting-IP` to the browser's address, as
  Cloudflare does. Any other `/api` path is a 404, and no key ever reaches the browser
  or the bundle.

  At startup it probes `GET /api/areas` once and logs the live data window or why it is
  unavailable; an unreachable stack answers `502` with a JSON `detail`, which
  `lib/api.ts` turns into the usual server-error notice.
- **In production**, nginx on the serving host proxies the same routes and serves
  `build/` with an `index.html` fallback (`backend/cloud/vps/nginx/hpldn.conf`).
- The gated routes need a reCAPTCHA pass: `lib/captcha.ts` gets one from
  `POST /api/session` and `lib/api.ts` sends it in `X-HPLDN-Pass`.

`REACT_APP_API_BASE` overrides the `/api` base path at build time. It holds a path, never
a key.

## Pages and routing

There is no router dependency. Every path serves the same bundle, `lib/routes.ts` maps
the pathname to a page, and `App.tsx` renders it and sets the document title.

Moving between pages never reloads the document (`lib/navigation.ts`). `App` listens for
clicks on the whole document: a plain left click on a link to another page of the site
pushes a history entry and swaps the page in place, inside `document.startViewTransition`
where the browser has it (a 180 ms cross-fade, skipped with reduced motion). The new page
opens at its fragment or its top, focus moves to `<main>` (or the address search, from the
header's "Price a home"), and Back and Forward swap pages the same way, returning to the
scroll position left. Modified and middle clicks, `target` and `download` links, other
sites and fragments of the page already showing are left to the browser.
`public/index.html` paints the page background before any CSS loads, and `index.css`
opts cross-document loads into view transitions, so even a full load does not flash white.

| Path | Page | Live figures |
| --- | --- | --- |
| `/` (and anything unrecognised) | `HomePage` — the tool | yes |
| `/avm` | `AvmPage` — how estimates are made | yes (data window) |
| `/terms-and-conditions` | `TermsPage` | no |
| `/privacy-policy` | `PrivacyPage` | no |

The home page is one scrolling page of anchored sections:

| Anchor | Section | Lives in |
| --- | --- | --- |
| `#top` | Hero: "What’s a London home worth?", the address search, sale date and "Price this home", and the folded "Can’t find the address? Estimate from details" form | `hero/`, `price/` |
| `#result` | Only once asked: the price card or the details estimate, whichever came last, with "See the area" and "Work out rental yield" | `price/`, `estimate/` |
| `#areas` | Explore an area: area search, Map \| Ranked, four stat tiles | `area/` |
| `#yield` | Could it pay as a rental? | `yield/` |
| `#how` | How it works, in three steps | `content/HowItWorks.tsx` |
| `#sources` | A thin strip naming the sources | `content/TrustBand.tsx` |
| `#faq` | Common questions | `content/FaqAccordion.tsx` |

## Source layout

```
src/
├── index.tsx, index.css     # entry point; index.css imports tokens and reset
├── App.tsx                  # picks the page from the path, sets the title
├── App.test.tsx             # end-to-end page tests
├── setupProxy.js            # dev proxy: /api -> the local stack's nginx (HPLDN_EDGE_URL)
├── setupTests.ts            # registers jest-dom matchers
├── reportWebVitals.ts       # Create React App web-vitals hook
├── area/                    # explore an area: search, map, ranked list, stat tiles
├── compare/                 # ranking chips
├── content/                 # how it works, sources strip, FAQ
├── data/                    # shared types, AreasProvider, generated map geometry
├── estimate/                # details estimate card and its parts
├── hero/                    # hero, details form, terraces illustration
├── layout/                  # page shell, header, footer, section band
├── legal/                   # terms and privacy policy content
├── lib/                     # API client, reCAPTCHA pass, call log, CPIH, postcode helpers, formatters, routes, navigation
├── pages/                   # the four pages and the long-form page frame
├── price/                   # address search, price form and price card
├── styles/                  # design tokens and reset
├── test/                    # hand-written API fixtures
├── ui/                      # shared primitives: button, field, combobox, date field, toggles, notices, motion
└── yield/                   # yield calculator and its cost assumptions
```

Components live beside a `.module.css` file of the same name. Address suggestions are the
server's (`GET /api/addresses/search`); the page ships no address data of its own.

## Directories

### `area/`

The `#areas` section, "Explore an area": where an area is and what it is like.

| File | Exports | Role |
| --- | --- | --- |
| `AreaSnapshot.tsx` | `AreaSnapshot`, `areaLabel`, `matchAreas` | A top bar with the area search (a `Combobox` matching code or name, e.g. "SW11" or "Battersea") and a Map \| Ranked toggle; below, one column at every width: the map and legend (or the ranked list) across the full width, then the area's name, sales count, "282 sales, January to July 2026" (`describeFiguresPeriod`) and four tiles (the year's average price with its change from the year before, the change from five years before, transport with the walk, and the deprivation score to three significant figures, lower is better). Picking an area anywhere only changes the figures; the page never jumps. Unknown figures read "Not enough sales", "Not known" or "Not available here", and a `lowData` area carries a small badge |
| `RankedAreas.tsx` | `RankedAreas`, `SHORTLIST` | Areas ranked by `SortChips` (cheapest first by default, unknown figures last): the top 10 rows (code · name, price, the sorted figure), always including the selected area, with "Show all N areas". The full list keeps the section its size: above 640 px it scrolls inside a focusable box about ten rows tall; at 640 px and below (phones, watches) it pages ten at a time with Previous, "Page X of Y" and Next. A new ranking starts from the top |
| `LondonMap.tsx` | `LondonMap` | Choropleth of Greater London by postcode area, usable by mouse, keyboard and touch |
| `MapLegend.tsx` | `MapLegend`, `PRICE_BREAKS`, `BAND_LABELS`, `RAMP`, `rampIndexFor` | Price bands shared by the map and legend: fixed bands <£450K, £450K–£600K, £600K–£800K, £800K–£1M, £1M–£1.2M and £1.2M+, labelled in the legend by each band's upper edge (<£450K, £600K … £1.2M, £1.2M+), six colour steps from `styles/tokens.css` |
| `StatTile.tsx` | `StatTile` | Label, large value, optional note and signed delta (arrow and sign, never colour alone); a `null` delta shows `deltaMissing` in words |
| `TransportMeter.tsx` | `TransportMeter` | A 1–5 transport score as five dots plus a word (Limited … Excellent), or "Not known" for `null` |

**The map.** `LondonMap` draws `data/londonGeo.ts` shapes shaded by each area's average
price, with the Thames on top. Clicking or pressing Enter/Space on a covered area selects
it. Five zoom steps (`ZOOM_STEPS`) keep the selection centred, the last
two separating EC1–EC4 and WC1/WC2, and a reset button returns to the full view. Phones
(≤ 640 px) open at step 2. The view pans by dragging, zooms by pinching, double-tapping or
the 44 × 44 px buttons, and a touch picks the covered area whose label point is within
16 px, so the 2–4 px central shapes can be chosen. Covered shapes are buttons with hover
and focus tooltips, kept inside the frame (flipped below near the top edge, wrapped at
16 rem); a touch never shows one. A shape with no price from `/api/areas` is drawn in the
idle fill, is neither a button nor a tab stop, and its tooltip reads e.g. "TN · not
covered".

**The area model.** The page covers the codes `/api/areas` returns, out of 132: every inner
London postcode district, with sub-districts folded into their parent (SW1V into SW1; EC
and WC by line), and one area per outer London postcode area (BR, CR, DA, EN, HA, IG, KT,
RM, SM, TW, UB, WD). An area with fewer than 10 sales in the window is left out (WD
today), and one with fewer than 50 is flagged `lowData`. `lib/estimate.ts` `areaOf` applies the server's rule, so
"SW1V 4AB" is SW1 and "TN16 3AA" is not covered. Each area's rent is the ONS average
private rent for its most common borough.

### `compare/`

The ranking chips, used by `area/RankedAreas.tsx`.

| File | Exports | Role |
| --- | --- | --- |
| `SortChips.tsx` | `SortChips`, `SORT_MODES` | The four rankings in sentence case: Cheapest (the default), Fastest growing, Best connected, Highest yield (`SortMode`) |

### `content/`

Static explanatory sections of the home page.

| File | Exports | Role |
| --- | --- | --- |
| `HowItWorks.tsx` | `HowItWorks` | Three short steps (search the address, pick the sale date, see the price, the area and the rental sums) and a link to the AVM page |
| `TrustBand.tsx` | `TrustBand` | The thin `#sources` strip: one sentence and an inline list (HM Land Registry · Energy certificates · Transport for London · ONS rents · Refreshed monthly); no disclaimer |
| `FaqAccordion.tsx` | `FaqAccordion`, `FaqItem` | "Common questions": five items in plain words, all closed to start. The accuracy answer is one sentence linking to the AVM page's figures |

### `data/`

Types and data shared across the app.

| File | Exports | Role |
| --- | --- | --- |
| `types.ts` | `PropertyType`, `PROPERTY_TYPE_NAMES`, `PROPERTY_TYPES`, `EstimateRequest`, `EstimateResult`, `Driver`, `DriverTone`, `Comparable`, `Confidence`, `AddressOption`, `PriceRequest`, `PriceResult`, `LastSale`, `DataWindow`, `AreasResponse`, `AreaStats`, `DistrictShape`, `SortMode` | The API wire format and shared shapes. `PriceResult.habitableRooms` comes from the same certificate as `energyRating`; an API that predates it leaves it out, which reads as not known. `PriceResult.epcSource` is `'certificate'` or `'estimated'` (no certificate: `floorAreaSqm` and rooms estimated from `estimatedFrom`, `floorAreaSqm` null when they could not be, no `energyRating`) |
| `AreasProvider.tsx` | `AreasProvider`, `useAreas`, `AreasState`, `AreasStatus`, `DEFAULT_DISTRICT` | Loads `GET /api/areas` once (again on `retry`), cancels a stale request, and exposes `status`, `areas`, `areaByCode`, `window` and `errorMessage`. `useAreas` throws outside the provider. The default area is SW11 |
| `londonGeo.ts` | `MAP_VIEW_BOX`, `DISTRICT_SHAPES`, `DISTRICT_SHAPE_BY_CODE`, `THAMES_PATH` | **Generated** map geometry |

`types.ts` is the contract with `backend/api/schemas.py`; change both together.

**The geometry** is generated by `python -m hpldn.backend.scripts.build_london_map`
(read-only, against `hpdbldn`); re-run it rather than editing the file. Inner districts
come from Doogal's postcode district polygons, outer postcode areas from Voronoi cells
around Doogal's in-use postcodes unioned per letter code, all clipped to the union of
the ONS LSOA 2011 boundaries, cleaned into an exact coverage, simplified together at
about 40 m so neighbours share edges, and projected to a 640-wide viewBox. The Thames is
from OS Open Rivers. Outlines are indicative, not survey accurate.

### `estimate/`

The details estimate card, shown in `#result`.

| File | Exports | Role |
| --- | --- | --- |
| `EstimateResult.tsx` | `EstimateResult`, `EstimateStatus` | Nothing while idle; then loading, error (with retry) or the card: value and range, the three most important reasons under "Why this price", and "Nearby sales" (three, then "Show all N"), with "See the area" and "Work out rental yield". On every answer or failure, focus moves to the card's heading and it scrolls into view |
| `ValueHeadline.tsx` | `ValueHeadline` | The large figure, counting up over 600 ms; the final value is in the DOM from the first render, and reduced motion skips the animation |
| `RangeBand.tsx` | `RangeBand` | The low–high range as one band with both ends labelled; `labelId` keeps two bands on one page distinct |
| `DriverChip.tsx` | `DriverChip` | One reason (label over detail, wrapping, never truncated), with a screen-reader word for its tone |
| `ComparableRow.tsx` | `ComparableRow`, `describeComparable` | One nearby sale: street, "Flat, 130 m²" (plus "(other type)" for `matchType: "broader"`), sale month, price and distance in miles. `EstimateResult` says so when there are none |

Reasons arrive ordered nearby sales, price momentum, transport, size, so the card shows
the first three. The range is shown as a minimum and maximum; confidence is not shown as
a pill.

### `hero/`

The `#top` opening screen.

| File | Exports | Role |
| --- | --- | --- |
| `Hero.tsx` | `Hero` | "What’s a London home worth?", the sub-heading, `price/PriceForm`, and the "Can’t find the address? Estimate from details" disclosure that unfolds `EstimateForm` (it also opens when the server rejects that form's postcode), over a dark illustration and scrim. The hero sits above the sections after it, so the address list and calendar can hang over them |
| `EstimateForm.tsx` | `EstimateForm` | The details form and its validation |
| `Terraces.tsx` | `Terraces` | A deterministic SVG row of London terraces, drawn so the page loads no image |

**The details form.** Postcode accepts a full postcode or a bare district; property type
offers Flat (default), Maisonette, Terraced house, Semi-detached house and Detached house;
bedrooms 1 to "6 or more"; floor area is a number with an attached `m²` box, valid from 15
to 1,000 m². Errors name the
problem ("Invalid UK Postcode", "We do not cover TN16 yet. Try a London postcode."), focus
moves to the first invalid field, and a status line counts the fields to fix. A 404 from
the server is shown on the postcode field and focuses it. "Estimate value" is rectangular
and sized to match the controls beside it.

**Form rows.** Both forms' grids use `align-items: end` and one-line labels, so controls
share a baseline however long a label is. While any field in a row shows a hint or error,
the other cells keep a line of the same height (a `:has()` rule, above 640 px) so the
controls stay level.

### `layout/`

The frame shared by every page.

| File | Exports | Role |
| --- | --- | --- |
| `PageShell.tsx` | `PageShell` | Skip link, header, `<main id="main">`, footer |
| `Header.tsx` | `Header` | Wordmark, four section links (Price a home, Areas, Yield, How it works), a primary "Price a home" button, and a mobile menu with the same links and button that closes above 900 px, on Esc or on a tap outside; it scrolls inside itself on short screens, holds the page still, focuses its first link and returns focus to Menu. Transparent over the home hero until scrolled 80 px; its translucent background has a solid fallback for browsers without `color-mix()` |
| `Footer.tsx` | `Footer` | Two link groups, Tools (Price a home, Areas, Yield) and About (How estimates work, Terms, Privacy), and the site's one "not a valuation" sentence, linking to the terms |
| `Section.tsx` | `Section` | A page band with optional eyebrow, heading and lead, `default`/`sunk`/`dark` tone and consistent padding and width. The home page uses headings only |

On the home page `onGetEstimate` scrolls to the address search and focuses it; elsewhere
the action opens the home page in place (`lib/navigation`) and focuses it there.

### `legal/`

Long-form legal content, rendered inside `pages/DocumentPage`.

| File | Exports | Role |
| --- | --- | --- |
| `TermsAndConditions.tsx` | `TermsAndConditions` | Terms led by the `#legal-advice` disclaimer and its regulatory points |
| `PrivacyPolicy.tsx` | `PrivacyPolicy` | Privacy policy written to the transparency duties in Articles 13 and 14 |

Clauses are data arrays rendered with the shared prose styles in `ui/Prose.module.css`.
The footer's "terms" link points at `/terms-and-conditions#legal-advice`.

### `lib/`

Framework-free helpers, each with a unit test file.

| File | Exports | Role |
| --- | --- | --- |
| `api.ts` | `request`, `getAreas`, `requestEstimate`, `getAddresses`, `searchAddresses`, `searchQueryFor`, `searchKey`, `requestPrice`, `ApiError`, `isAbort`, `REQUEST_TIMEOUT_MS`, `PRICE_TIMEOUT_MS`, `ADDRESS_SUGGESTION_LIMIT`, `MIN_SEARCH_LENGTH`, `MAX_SEARCH_LENGTH` | The API's only `fetch` caller: JSON, 10 s timeout (30 s for a price; also while the body is read), caller cancellation, landlord-safe errors, `ApiError.missingRoute` for a route the server does not have, `retryable`, and `rateLimited` with `retryAfterS` for a 429. `searchAddresses` mirrors the server's bounds before sending (3–64 characters; characters the server refuses become spaces), so typing never earns a 422. Callers decide a rejection was a cancellation from their own `signal.aborted`, never from the error's name |
| `estimate.ts` | `normalisePostcode`, `areaOf`, `districtOf`, `isPostcodeShaped`, `isCoveredPostcode`, `OUTER_AREAS` | Postcode shape, canonical form and coverage, mirroring `backend/api/market/districts.py` |
| `format.ts` | `formatGBP`, `formatGBPCompact`, `formatPercent`, `formatWholePercent`, `formatArea`, `formatDistance`, `formatSoldDate`, `formatLongDate`, `formatCount`, `formatBedrooms`, `stripNonDigits`, `groupDigits` | Hand-rolled, byte-stable formatting (no `Intl`), e.g. "£520,000", "£1.34m", "+4.0%", "72 m²", "0.4 miles", "March 2025", "15 September 2026"; typed money is kept to digits and grouped as it is typed ("425,000") |
| `price.ts` | `isFullPostcode`, `hasHouseNumber`, `isoDate`, `addDays`, `earliestSaleDate`, `latestSaleDate`, `saleDateError`, `formatUkDate`, `parseUkDate`, `maskUkDate`, `readable`, `describeAddress`, `describeFullAddress`, `addressKey`, `MAX_HORIZON_DAYS` | Sale-date limits (tomorrow to five years, compared as ISO days, messages in DD/MM/YYYY), UK date formatting, parsing and input masking (form state stays ISO), full-postcode check, whether typed text starts with a house number the way the server parses it (so "barclay road cr0" still gets the narrow-down hint), and how a register address reads ("18 Barclay Road, Croydon, CR0 1JN") |
| `navigation.ts` | `navigate`, `followLink`, `subscribeToNavigation` | Page changes without a reload: history entries with their scroll position, view transitions, focus and scroll on arrival, and which link clicks to take over |
| `captcha.ts` | `ensurePass`, `warmPass`, `dropPass`, `primeCaptcha`, `loadCaptchaConfig`, `CaptchaError` | Google reCAPTCHA v3 and the pass the API hands out for it (`POST /api/session`); `api.ts` sends the pass in `X-HPLDN-Pass` |
| `callLog.ts` | `newRequestId`, `logCall`, `apiLogEnabled` | A fresh `X-Request-ID` per call (nginx and the API log it as `rid=`), and one console line per call in development |
| `inflation.ts` | `cpihIndexAt`, `isProjectedMonth`, `inflate` | Moving pounds between months along CPIH, carrying the latest month forward at its 12-month rate |
| `routes.ts` | `PageKey`, `hrefFor`, `pageFor`, `matchPage` | Page paths (respecting `PUBLIC_URL`), path-to-page lookup, and whether the site serves a path at all |

### `pages/`

| File | Exports | Role |
| --- | --- | --- |
| `HomePage.tsx` | `HomePage` | Composes every home section. Holds the latest request (a price or a details estimate) with its status, answer and error, the focused area, and the requests to move to Areas or Yield; cancels a request in flight when another is made; moves Areas to the answer's district; routes an estimate's 404 to its postcode field; builds the yield calculator's "This home" and "Area typical" figures |
| `AvmPage.tsx` | `AvmPage` | The AVM explainer: what it is, method, data, features, accuracy, area figures, yield calculator and updates. Clauses are built from the live data window and omit numbers until it loads |
| `TermsPage.tsx`, `PrivacyPage.tsx` | `TermsPage`, `PrivacyPage` | The legal documents on their own pages |
| `DocumentPage.tsx` | `DocumentPage` | Frame for long-form pages: eyebrow, heading, lead, content and related-page links |

### `price/`

Pricing one specific home for a sale on a chosen day: the hero's form and the card in
`#result`. The server does the work (`backend/scripts/hpp_inference.py` through
`POST /price`); nothing here names the model.

| File | Exports | Role |
| --- | --- | --- |
| `AddressSearch.tsx` | `AddressSearch`, `SEARCH_DEBOUNCE_MS`, `MIN_QUERY_LENGTH` | The "Address" combobox. After 300 ms and 3 characters it lists every home in a full postcode (`getAddresses`) or searches addresses (`searchAddresses`), cancelling the search before. Up to 8 suggestions, the street line bold over the postcode; "Searching…", "No homes found. Try the postcode" or the server's message in their place, and "Add the house number or postcode to narrow the list" above a full list typed without a number. Choosing one fills the field with the one-line address; editing the text clears the choice |
| `PriceForm.tsx` | `PriceForm`, `ADDRESS_INPUT_ID` | [Address][Sale date][Price this home] on one row (the button wraps below 900 px; one column below 640 px). The date is a `ui/DateField`, tomorrow by default. Checks a chosen home ("Choose a home from the suggestions.") and the date before calling the server, focusing the first invalid field |
| `PriceResultCard.tsx` | `PriceResultCard`, `PriceStatus`, `homeSpecs`, `isEstimated`, `estimateNote` | Nothing until asked; then loading, an error with the server's sentence and "Try again", or the card: the address, "Price if sold on 15 September 2026" beside the range when it has been measured, a spec strip (Type · Floor area · Rooms · Energy rating, leaving out any not known; for an estimated home "Floor area (est.)" and "Rooms (est.)", no rating, and the note "No energy certificate on record — size estimated from {estimatedFrom}; the range is wider."), the last sale, "Based on sales recorded up to July 2026", and the next steps |
| `DetailedAnalysis.tsx` | `DetailedAnalysis`, `priceFigures`, `perSqmFigures`, `nearbyFigures`, `inflationNote` | The price card's "Detailed Analysis" view: price, last sale, specs, £/m², nearby and rent figures, inflation-corrected to the sale date with CPIH (`GET /api/inflation`), the Schools section and the nearby sales at its foot |
| `NearbySchools.tsx` | `NearbySchools`, `schoolRows`, `inspectedText` | The nearest primary and secondary schools within 3 km with the Ofsted rating in force (`POST /api/schools`), each linking to its report |
| `NearbyProperties.tsx` | `NearbyProperties`, `saleFigures` | The nearest sales in the latest 12 months with their comparable-sales figures (`POST /api/nearby`) |
| `NextSteps.tsx` | `NextSteps` | "See the area" (selects the home's district in Areas and moves there) and "Work out rental yield" (switches the calculator to this home and moves there), shared with the estimate card |

### `styles/`

Global CSS, imported once by `index.css`. There is no CSS framework or styling library.

| File | Role |
| --- | --- |
| `tokens.css` | The single source of design tokens as CSS custom properties: colours (canvas, ink, accent, clay for cautions, the dark band, the six-step map ramp), type scale and weights, 8 px spacing scale, content widths, radii, shadow, motion durations and easing, header height. Dark-mode values under `prefers-color-scheme: dark`; smaller type and padding at 900 px, 640 px and 360 px (small phones and watches: 0.75 rem gutter, 56 px header) |
| `reset.css` | A small reset, focus-visible outline, selection colour, the `.srOnly` helper, and the `prefers-reduced-motion` block that disables transitions and animations |

Every colour, size and duration in a component's `.module.css` should come from a token.

### `test/`

| File | Exports | Role |
| --- | --- | --- |
| `fixtures.ts` | `WINDOW`, `SW11`, `SE15`, `BR`, `W8`, `AREAS`, `AREAS_RESPONSE`, `ESTIMATE`, `estimateFor`, `ADDRESSES`, `PRICE`, `priceFor` | Hand-written API responses: all 132 areas in server order (four with figures tests assert on, the rest placed so they never displace them), one data window, one estimate, and the homes and price for one Croydon postcode (2 habitable rooms, as its latest certificate records) |

`App.test.tsx` mocks `lib/api` with these fixtures.

### `ui/`

Shared primitives with no page knowledge.

| File | Exports | Role |
| --- | --- | --- |
| `Button.tsx` | `Button` | The only styled button: `primary`/`secondary`/`ghost`, `sm`/`md`/`lg`/`field` sizes, `pill`/`rect` shapes |
| `Field.tsx` | `Field` | One-line label, control (a render prop receiving id, class and ARIA attributes), hint and error |
| `Combobox.tsx` | `Combobox` | A generic editable combobox (WAI-ARIA 1.2): `aria-expanded`, `aria-activedescendant`, a listbox, ↑/↓/Enter/Esc, and presses that choose without taking focus from the input. The caller owns the text and options, so it can search a server (`AddressSearch`) or filter a list (the area search) |
| `DateField.tsx` | `DateField` | A text field masked to DD/MM/YYYY (`inputMode="numeric"`) with a calendar button opening a `react-day-picker` popover: single day, en-GB, Monday first, month and year dropdowns, days outside `min`–`max` disabled. Esc or a press outside closes it and returns focus to the input. Its value is ISO |
| `Segmented.tsx` | `Segmented` | Joined toggle buttons (`aria-pressed`) for a few exclusive choices: Map \| Ranked, and the yield calculator's figures source |
| `assignRef.ts` | `assignRef` | Assigns a node to a callback or object ref, so a component can keep its own ref and forward one |
| `DataNotice.tsx` | `DataNotice` | Loading (`aria-busy`) or error notice with optional retry |
| `RecaptchaNotice.tsx` | `RecaptchaNotice` | The line Google requires wherever its reCAPTCHA badge is hidden, under each form that sends a protected request |
| `Reveal.tsx` | `Reveal` | Fades content in on first scroll into view; visible immediately without `IntersectionObserver` |
| `useMediaQuery.ts` | `useMediaQuery` | Tracks a media query, false where it cannot be queried; the ranked list uses it to page on small screens |
| `useReducedMotion.ts` | `useReducedMotion` | Tracks `prefers-reduced-motion`, defaulting to reduced when it cannot be queried |
| `Prose.module.css` | — | Long-form text styles for the legal and AVM pages |
| `motion.test.tsx` | — | Tests for `Reveal` and `ValueHeadline` |

### `yield/`

| File | Exports | Role |
| --- | --- | --- |
| `YieldCalculator.tsx` | `YieldCalculator`, `YieldFigures`, `YieldSource`, `readMoney`, `DEPOSIT_MIN`, `DEPOSIT_MAX`, `PRICE_MIN`, `PRICE_MAX`, `RENT_MIN`, `RENT_MAX` | "Could it pay as a rental?": a This home · Area typical · My own figures toggle, £ price (£1 to £100,000,000) and rent (£1 to £1,000,000) fields grouped as typed, kept to 12 digits and saying so when out of bounds (an empty field only once left), and a 10–100 % deposit slider in 1 % steps. Until both figures are in bounds the results show a dash. The monthly position leads, with gross and net yield beside it, each with a one-line explanation; every figure stays on one line, sign and all, shrinking to fit its box through container units and a `--chars` length; negative results are highlighted, and the costs sit in a "What’s included" fold linking to the AVM page. Typing switches to the landlord's own figures, which nothing overwrites until they pick another source or ask to work out a home's yield; a new price or estimate otherwise becomes "This home" |
| `assumptions.ts` | `LETTING_FEE`, `MAINTENANCE`, `INSURANCE_A_YEAR`, `mortgageRateFor` | Assumed costs: 10 % letting fee, 1 % yearly upkeep, £480 insurance. The interest-only rate is the served `mortgageRates` rate for the lowest LTV band covering the borrowing (25 % deposit → 75 % LTV). The AVM page prints the same figures |

Net yield is `(annual rent − costs) / price`; monthly position is rent minus monthly costs
and interest on the borrowed amount.

## Conventions

- **Documentation** follows the Angular style: `/** */` JSDoc on every exported component,
  function, type and constant, and on non-obvious props. The first sentence is a summary;
  longer notes follow a blank line; `@param`, `@returns` and `@throws` describe
  non-obvious signatures. Props interfaces are documented as `Props for \`Component\``,
  with a doc comment on each field that needs one. Inline `//` comments are rare and
  short. `data/londonGeo.ts` is generated and keeps its generator header.
- **Styling** is CSS Modules, one `.module.css` beside each component, values from
  `styles/tokens.css`.
- **Language** on screen is landlord-facing: no model, table or column names outside the
  AVM page.
- **Data** comes only through `lib/api.ts` and `AreasProvider`; components never fetch.

## Accessibility and motion

Every control, including the map, the address and area comboboxes and the calendar, is
reachable and operable from the keyboard. A skip link leads each page. Submitting either
form moves focus to the result heading, and invalid fields carry `aria-invalid` and
`aria-describedby`. Tone is never carried by colour alone:
reasons carry a screen-reader word, deltas an arrow and sign, and the transport score a
word. The headline count-up writes its final value on the first render, and
`ui/useReducedMotion.ts` plus the `prefers-reduced-motion` block in `styles/reset.css`
switch animation off for anyone who asks.

## Layout at every width

Text wraps rather than shrinking out of sight or being clipped: headline,
reason chips and the map caption scale with container units (`cqi`) down to a floor
(28 px, 15 px, 14–15 px, 12 px) and wrap below it. Every such rule sets a plain size
first and the container-unit size inside `@supports (font-size: 1cqi)`, so browsers
without container queries (iOS 15 Safari, older Samsung Internet and WebViews) get a
readable wrapping size, and `body` does not hide horizontal overflow, so a regression
shows. jsdom cannot see CSS: confirm layout changes at 200, 320, 375, 390, 768, 1024 and
1440 px, and on WebKit for iOS 15 and 17.

## Tests

Unit tests are Jest and React Testing Library, run from this directory; `pytest` never
collects them. The end-to-end tests are Playwright, in `e2e/` (see below).

```bash
CI=true npm test -- --watchAll=false
```

`react-day-picker` and `date-fns` ship ES modules that Jest 27 cannot load, so
`package.json`'s `jest.moduleNameMapper` points them at their CommonJS builds (and at the
calendar's stylesheet). The date field imports `enGB` from `date-fns/locale/en-GB`, not
`react-day-picker/locale`, which pulls in more ESM.

The suites, with `lib/api` mocked from `src/test/fixtures.ts` wherever a page is
rendered:

| Suite | Covers |
| --- | --- |
| `src/App.test.tsx` | The site end to end: the hero, header, footer and the single disclaimer; loading and error states with retry; pricing a home (postcode and street searches, the default and typed UK dates, refusing today, choosing from the suggestions, a postcode with nothing to price, a record problem with retry, missing range, rooms and last sale, "See the area", "Work out rental yield", only the latest result); the details estimate (three reasons, nearby sales and "Show all", cancelling in flight, validation and error focus, a server 404 on the postcode field); exploring an area (search by code or name, Map \| Ranked, re-ranking, picking in place, the map including a touch tap, unknown figures in words, the deprivation score card); the full ranked list scrolling on a large screen and paging on a small one; the rental sums (the source toggle, typed-value protection, the costs fold, the 10–100 % deposit, price and rent bounds, a loss kept in one figure); how it works, the sources strip and the FAQ; the standalone pages, moving between them in place and back without reloading the figures, and the details toggle closing over a server error; no backend vocabulary on screen |
| `src/lib/api.test.ts` | Success, non-2xx to `ApiError`, network failure, the 10 s timeout (also during the body read), a non-JSON body, caller cancellation, that no `X-API-Key` header is ever sent, and the pricing calls: the address and search query strings, search text outside the server's bounds never sent, refused characters sent as spaces, the normal form, a 429 with `Retry-After`, which failures are retryable, a cancelled search, the price body, the server's sentence on a 422 or 503, the longer price timeout, a missing route told from an empty answer, and the nearby-sales and schools bodies (a flat's unit sent, a house's left out) |
| `src/lib/estimate.test.ts` | District extraction, area grouping, canonical form, shape and coverage |
| `src/lib/format.test.ts` | Every formatter (the short month, "Mar 2023", among them), byte for byte, grouped large percentages, and typed-number grouping |
| `src/lib/navigation.test.ts` | Which paths are pages, history entries with scroll positions, no page swap between fragments, and which link clicks are taken over or left to the browser |
| `src/lib/price.test.ts` | Sale-date limits and their DD/MM/YYYY messages, `formatUkDate`/`parseUkDate` including invalid dates and the window's bounds, the input mask, full postcodes, house numbers told from outward codes, and how register addresses read |
| `src/ui/Combobox.test.tsx` | ARIA attributes, arrow keys wrapping, Enter, Esc, choosing with a press without losing focus, the message row shown at once and announced once settled, the suggestion count, `aria-busy`, and a retry reached by press or Tab without the list closing |
| `src/ui/DateField.test.tsx` | DD/MM/YYYY display and masking with ISO state, a Monday-first calendar with past days disabled, choosing a day, closing on Esc or a press outside with focus returned |
| `src/price/AddressSearch.test.tsx` | The request budget: one call for an address typed at normal speed and none to the postcode route (the fan-out regression test), at most one per pause and never two within 400 ms, no call outside the 3–64 bounds and `maxLength`, no repeat for text already answered; the endpoint by query shape, `aria-busy`, cancelling a stale search and on unmount, the two-line suggestion, choosing by press and by keyboard, nothing found as `200 []`, a missing route, 5xx or no connection as a failure with a working "Search again" (reachable by Tab), a 429 as "One moment…" with one retry after `Retry-After`, the house-number hint with an outward code typed, a postcode with nothing to price, and a settled screen-reader announcement |
| `src/price/PriceForm.test.tsx` | No Find button or home list, the address check, a typed UK date, date errors |
| `src/price/PriceResultCard.test.tsx` | The spec strip with rooms, rooms and rating hidden when unknown, an estimated home's labels and note, an estimate with no floor area, no note with a certificate, no accuracy or caveat text, no earlier sale and no range, the next steps, nothing while idle, and the schools loader fetching each home once across turns |
| `src/price/DetailedAnalysis.test.tsx` | Turning to the analysis and back with focus; the price, last sale, specs, per-m², nearby and rent figures inflation corrected; the Schools section (one row per school, an all-through school once, "Good · inspected Mar 2023 · 420 m away", a predecessor's rating marked, links to the Ofsted report, the key-judgements/report-card and catchment footnote, none in range, no section without a loader, a failed fetch retried); the nearby sales at the foot, after the schools; nearby sales, schools and CPIH each fetched once |
| `src/area/areaFigures.test.ts` | The map's fixed price bands, the figures period's part year, and the mortgage rate for a deposit |
| `src/hero/EstimateForm.test.tsx` | The details form's validation: errors on an empty submit, cleared as fields are corrected, re-checked on leaving after a submit, a server postcode error hidden once edited |
| `src/lib/captcha.test.ts` | The pass with the gate off, loading Google's script on first interaction with the API's site key, a blocked script, and pass reuse and refresh |
| `src/lib/inflation.test.ts` | `cpihIndexAt` (published, carried-forward and missing months) and `inflate` |
| `src/yield/YieldCalculator.test.tsx` | Caret placement in the money fields as digits are regrouped, deleted or typed |
| `src/yield/assumptions.test.ts` | A home's monthly rent from its borough and implied bedrooms, its fallbacks, and where it comes from |
| `src/ui/motion.test.tsx` | Content renders without an `IntersectionObserver`, and the headline figure is in the DOM immediately, with or without a note |

### End-to-end tests (`e2e/`)

Playwright runs against the local stack: production's nginx, the guard daemon and the API
under gunicorn with the reCAPTCHA gate on. `playwright.config.ts` starts it with
`uv run python -m hpldn.backend.cloud.local up --fake-google --build --foreground` unless
one already answers on `E2E_BASE_URL` (default `http://localhost:8080`). Only Google is
replaced: the page gets a stub `grecaptcha` (`e2e/fixtures.ts`) whose tokens a fake
siteverify on the loopback answers, so the API's own check, its pass and every limit run
for real. Each test is a fresh visitor with its own `CF-Connecting-IP`.

```bash
npm run e2e        # every project
npm run e2e:ui     # Playwright's UI mode
```

| Path | Covers |
| --- | --- |
| `e2e/home.spec.ts` | The page end to end against the live stack |
| `e2e/api/*.spec.ts` | One file per exposed `/api` route group (address search, areas, captcha, detailed analysis, estimate, price), each test tagged `@valid`, `@invalid` or `@redteam`; `@ui` tests also run in the 390 px `mobile` project |
| `e2e/coverage.spec.ts` | Every exposed `/api` route has a `@valid`, an `@invalid` and a `@redteam` test |
| `e2e/global-setup.ts`, `e2e/support/api.ts` | Fails fast when the stack is not the expected one; shared request helpers |

The specs that need Google's real script run only with `E2E_LIVE_GOOGLE=1`, against a
stack started without `--fake-google`.
