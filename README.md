# DomainSifter

> Daily-curated list of expired domains worth registering.

## What this is

DomainSifter is a free public tool that publishes a daily-curated list of recently-expired domains worth re-registering. We pull from ICANN zone files, enrich each candidate with public metadata (Wayback history, OpenPageRank, Google Safe Browsing, language detection), and aggressively filter out spam, malware, adult content, and junk patterns.

## Status

**Pre-launch.** The list shown on the site is preview data — 20 invented sample domains used to demonstrate the layout and interactions. Real daily lists begin once the data pipeline is live.

## How it works

See [/#methodology](https://domainsifter.com/#methodology) for the three-step pipeline (catch the drop → filter the noise → publish what's left).

## Tech stack

Astro 4 (static output) · Tailwind CSS · vanilla JS · deployed to Cloudflare Pages via Wrangler.

## Local development

```bash
npm install
npm run dev          # http://localhost:4321
```

To preview the production build locally with the Cloudflare Pages runtime:

```bash
npm run build
npm run pages:dev    # serves dist/ via wrangler
```

## Deployment

Push to `main` → Cloudflare Pages auto-deploys from `dist/`. The build command is `npm run build` and the output directory is `dist`.

For a manual deploy from your machine:

```bash
npm run build
npm run pages:deploy
```

## License

MIT.

## Contact

[hello@domainsifter.com](mailto:hello@domainsifter.com)
