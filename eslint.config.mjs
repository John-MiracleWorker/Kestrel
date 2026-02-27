// ESLint v10 flat config
//
// TypeScript files (.ts/.tsx) are excluded here because no TypeScript parser
// (@typescript-eslint/parser) is available in this environment — type-level
// checks are handled by `tsc` instead.
//
// JS/JSX files are linted with a minimal set of rules that catch real bugs
// without producing noise.

export default [
    {
        // Globally ignore TS files and build artefacts so ESLint never tries
        // to parse them without the right parser.
        ignores: [
            '**/*.ts',
            '**/*.tsx',
            '**/node_modules/**',
            '**/dist/**',
            '**/build/**',
            '**/.next/**',
            '**/coverage/**',
        ],
    },
    {
        files: ['**/*.{js,jsx}'],
        rules: {
            // Catch likely bugs
            'no-debugger': 'error',
            'no-duplicate-case': 'error',
            'no-empty': ['warn', { allowEmptyCatch: true }],
            'no-extra-semi': 'warn',
            'no-unreachable': 'warn',
            'no-undef': 'off',
            'no-unused-vars': 'off',
        },
    },
];
