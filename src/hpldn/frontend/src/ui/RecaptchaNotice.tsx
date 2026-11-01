import React from 'react';
import styles from './RecaptchaNotice.module.css';

/**
 * The line Google requires wherever its reCAPTCHA badge is hidden
 * (`.grecaptcha-badge` in `index.css`): shown under each form that sends a
 * protected request.
 */
export function RecaptchaNotice(): React.ReactElement {
  return (
    <p className={styles.notice}>
      Protected by reCAPTCHA. The Google{' '}
      <a className={styles.link} href="https://policies.google.com/privacy" rel="noreferrer noopener" target="_blank">
        Privacy Policy
      </a>{' '}
      and{' '}
      <a className={styles.link} href="https://policies.google.com/terms" rel="noreferrer noopener" target="_blank">
        Terms of Service
      </a>{' '}
      apply.
    </p>
  );
}

export default RecaptchaNotice;
