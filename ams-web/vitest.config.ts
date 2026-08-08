import { defineConfig } from 'vitest/config';
import { fileURLToPath } from 'node:url';

const src = fileURLToPath(new URL('./src', import.meta.url));

export default defineConfig({
  resolve: {
    alias: {
      // `server-only` throws when imported outside an RSC graph; in unit tests
      // we import the server modules directly, so stub it to a no-op. The real
      // guarantee (client components cannot import these) is enforced by the
      // Next build and by source-isolation.test.ts.
      'server-only': fileURLToPath(new URL('./test/stubs/server-only.ts', import.meta.url)),
      '@': src,
    },
  },
  test: {
    environment: 'node',
    globals: false,
    setupFiles: ['./test/setup.ts'],
    include: ['test/**/*.test.ts'],
  },
});
