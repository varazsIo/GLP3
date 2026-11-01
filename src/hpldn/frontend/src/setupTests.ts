// Adds jest-dom's DOM matchers, e.g. toBeInTheDocument, to every test.
import '@testing-library/jest-dom';

// jsdom does not scroll; page changes call window.scrollTo.
window.scrollTo = () => {};

// Forms start loading reCAPTCHA, and getting a pass, on the first interaction
// (lib/captcha), which would send a real request from jsdom; the component
// tests have no API to ask. lib/captcha.test.ts unmocks it.
jest.mock('./lib/captcha', () => ({
  ...jest.requireActual('./lib/captcha'),
  primeCaptcha: jest.fn(),
  warmPass: jest.fn(),
}));

// lib/api shares an open GET in flight between callers; a test that leaves one
// hanging must not hand it to the next test.
afterEach(() => {
  // eslint-disable-next-line @typescript-eslint/no-var-requires, global-require
  require('./lib/api').forgetSharedGets();
});
