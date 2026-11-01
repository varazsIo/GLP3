import React from 'react';
import TermsAndConditions from '../legal/TermsAndConditions';
import DocumentPage from './DocumentPage';

/**
 * The terms and conditions page, at `/terms-and-conditions`.
 */
export function TermsPage(): React.ReactElement {
  return (
    <DocumentPage
      eyebrow="Legal"
      heading="Terms and conditions"
      lead="What this service is, what it is not, and the limits of what you can rely on."
      related={[
        { page: 'privacy', label: 'Privacy policy' },
        { page: 'avm', label: 'How our estimates work' },
      ]}
    >
      <TermsAndConditions />
    </DocumentPage>
  );
}

export default TermsPage;
