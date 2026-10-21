// Written by `plumbcheck sync` from plumbcheck's defaults.
// Only settings that differ from ESLint's defaults, each with its reason.
import js from '@eslint/js';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  // Bundles and build output are generated (node_modules is ignored by default).
  {ignores: ['dist/**', 'build/**']},
  // ESLint has no rules by default: its recommended ones,
  js.configs.recommended,
  // and typescript-eslint's for TypeScript.
  {files: ['**/*.ts', '**/*.tsx'], extends: [tseslint.configs.recommended]},
);
