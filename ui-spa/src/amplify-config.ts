import { Amplify } from 'aws-amplify';

/**
 * Amplify v6 configuration for the slice-4b SPA.
 *
 * Identity comes from the SAME Cognito User Pool + App Client the API Gateway
 * JWT authoriser is configured against. Amplify uses USER_SRP_AUTH by default,
 * which means the password is never sent over the wire — only an SRP verifier
 * derived from it. That matches the App Client's `ALLOW_USER_SRP_AUTH` flow
 * in infra/template.yaml.
 *
 * No OAuth / Hosted UI here: we use the Amplify React `Authenticator`
 * component (a simple in-app form) so the demo is single-page and the demo
 * recording doesn't bounce through a separate Cognito-hosted page.
 */
const region = import.meta.env.VITE_AWS_REGION || 'eu-west-2';
const userPoolId = import.meta.env.VITE_USER_POOL_ID;
const userPoolClientId = import.meta.env.VITE_USER_POOL_CLIENT_ID;

if (!userPoolId || !userPoolClientId) {
  // Surface the misconfig immediately, not as a confusing runtime auth error
  // 30 seconds later when the user types a password.
  // eslint-disable-next-line no-console
  console.error(
    '[amplify-config] VITE_USER_POOL_ID and VITE_USER_POOL_CLIENT_ID must be set.',
    'Copy .env.example to .env.local and fill in the CFN stack outputs.'
  );
}

Amplify.configure({
  Auth: {
    Cognito: {
      userPoolId,
      userPoolClientId,
      // Public SPA client — no secret. Matches `GenerateSecret: false` in CFN.
      signUpVerificationMethod: 'code',
      loginWith: { email: true },
    },
  },
});

export const apiBase = (import.meta.env.VITE_API_BASE || '').replace(/\/$/, '');
export const awsRegion = region;
