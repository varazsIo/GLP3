import React from 'react';
import PrivacyPolicy from '../legal/PrivacyPolicy';
import DocumentPage from './DocumentPage';

/**
 * The privacy policy page, at `/privacy-policy`.
 */
export function PrivacyPage(): React.ReactElement {
  return (
    <DocumentPage
      eyebrow="Legal"
      heading="Privacy policy"
      lead="What we do with personal data, and the rights you have over it under UK data protection law."
      related={[
        { page: 'terms', label: 'Terms and conditions' },
        { page: 'avm', label: 'How our estimates work' },
      ]}
    >
      <PrivacyPolicy />
    </DocumentPage>
  );
}

export default PrivacyPage;
