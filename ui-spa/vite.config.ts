import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Slice 4b: dev-only. Slice 4c will move this build to S3 + CloudFront.
// We deliberately KEEP the dev origin (http://localhost:5173) as the only
// CORS-allowed origin in infra/template.yaml until then.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
  },
});
