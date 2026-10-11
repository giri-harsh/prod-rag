import js from '@eslint/js';
import { defineConfig, globalIgnores } from 'eslint/config';
import reactHooks from 'eslint-plugin-react-hooks';
import globals from 'globals';
import tseslint from 'typescript-eslint';

export default defineConfig(
  globalIgnores(['dist', 'node_modules', 'playwright-report', 'test-results', '.e2e']),
  js.configs.recommended,

  // Browser app + unit tests + node-side TypeScript: strict, type-aware rules.
  {
    files: ['**/*.ts', '**/*.tsx'],
    extends: [tseslint.configs.strictTypeChecked, tseslint.configs.stylisticTypeChecked],
    languageOptions: {
      parserOptions: {
        // The project service resolves a file by walking UP for the nearest tsconfig.json. web/tsconfig.json
        // only includes "src", so anything outside it was reported as "not found by the project service" and
        // was never actually linted - the type-aware rules silently did nothing on 28 real findings in
        // test/. Subdirectories therefore get their own tsconfig.json (test/, e2e/); root-level config files
        // are handled by the disableTypeChecked block at the bottom.
        projectService: { allowDefaultProject: ['vite.config.ts', 'playwright.config.ts'] },
        tsconfigRootDir: import.meta.dirname,
      },
    },
    rules: {
      '@typescript-eslint/consistent-type-imports': 'error',
      '@typescript-eslint/restrict-template-expressions': ['error', { allowNumber: true }],
    },
  },
  {
    files: ['src/**/*.{ts,tsx}'],
    extends: [reactHooks.configs.flat.recommended],
    languageOptions: { globals: globals.browser },
  },
  {
    files: ['server/**/*.mjs', 'e2e/**/*.ts', 'test/**/*.ts', '*.ts', '*.js'],
    languageOptions: { globals: globals.node },
  },

  // Plain browser script served as a static file (the CSP forbids inline scripts, so the pre-paint theme
  // applier cannot be inlined). It is not part of the TS app, so it needs browser globals declared here.
  {
    files: ['public/**/*.js'],
    languageOptions: { globals: globals.browser, ecmaVersion: 2022, sourceType: 'script' },
  },

  // Tests assert their own preconditions (`expect(x).toBeTruthy()` immediately before using `x`), which is
  // what a non-null assertion expresses on the next line. Requiring a runtime fallback instead would add
  // branches that can never be taken and would hide the failure the assertion is there to surface.
  {
    files: ['test/**/*.ts', 'e2e/**/*.ts'],
    rules: { '@typescript-eslint/no-non-null-assertion': 'off' },
  },

  // BFF server: plain JS with JSDoc, type-checked by `tsc` (checkJs) rather than by typescript-eslint.
  {
    files: ['**/*.mjs'],
    languageOptions: { globals: globals.node },
  },

  // Root-level config files (vite.config.ts, playwright.config.ts, eslint.config.js). They resolve to an
  // INFERRED project - not a real tsconfig - so the type-aware rules cannot see @types/node and reported
  // every `process.env` as an unresolvable type. Their types are still checked for real: both .ts files are
  // in tsconfig.node.json's include, so `npm run typecheck` covers them. Only the type-aware lint rules are
  // switched off, and only for files matched at the root (no `**/` in these patterns).
  {
    files: ['*.ts', '*.js'],
    extends: [tseslint.configs.disableTypeChecked],
  },
);
