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
      parserOptions: { projectService: true, tsconfigRootDir: import.meta.dirname },
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

  // BFF server: plain JS with JSDoc, type-checked by `tsc` (checkJs) rather than by typescript-eslint.
  {
    files: ['**/*.mjs'],
    languageOptions: { globals: globals.node },
  },
);
