import typography from '@tailwindcss/typography';

/** @type {import('tailwindcss').Config} */
export default {
  content: ['./src/**/*.{astro,html,js,jsx,ts,tsx,vue,svelte}'],
  theme: {
    extend: {
      colors: {
        paper: '#fafaf9',
        ink: '#1a1a1a',
        accent: {
          DEFAULT: '#0d6e6e',
          hover: '#0a5757',
        },
      },
      fontFamily: {
        sans: [
          'Inter',
          'ui-sans-serif',
          'system-ui',
          '-apple-system',
          'Segoe UI',
          'Roboto',
          'Helvetica Neue',
          'Arial',
          'sans-serif',
        ],
      },
      maxWidth: {
        prose: '70ch',
      },
    },
  },
  plugins: [
    // @tailwindcss/typography supplies the `prose` utility used by archive
    // /d/{domain} pages so the Haiku-generated Markdown (real ##/### and
    // blank-line paragraphs) gets visible heading hierarchy and paragraph
    // spacing. Without the plugin, `prose*` classes emit zero CSS — that's
    // the bug that landed the first 33 backfilled pages with collapsed
    // typography on 2026-05-17.
    typography,
  ],
};
