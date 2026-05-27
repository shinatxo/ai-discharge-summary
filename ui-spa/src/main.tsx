import React from 'react';
import ReactDOM from 'react-dom/client';
import { Authenticator } from '@aws-amplify/ui-react';
import '@aws-amplify/ui-react/styles.css';

import './amplify-config'; // calls Amplify.configure as a side effect
import { App } from './App';
import './styles.css';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    {/* Authenticator wraps the app; the user only sees <App /> after a
        successful Cognito sign-in. It also handles the new-password-required
        challenge + TOTP MFA enrolment for free, matching the User Pool's
        MfaConfiguration: OPTIONAL + SOFTWARE_TOKEN_MFA. We deliberately turn
        off the sign-up tab — the pool has AllowAdminCreateUserOnly: true. */}
    <Authenticator hideSignUp={true}>
      {({ signOut, user }) => <App signOut={signOut} user={user} />}
    </Authenticator>
  </React.StrictMode>
);
