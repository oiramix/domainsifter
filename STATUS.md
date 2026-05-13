# DomainSifter — Build Status

_Last updated: 2026-04-26_

## Where we are

**Production homepage v1 is shipped and live on Cloudflare Pages.** Static site, sample data, no real pipeline yet.

- Live URL: https://domainsifter.pages.dev
- Last deploy: https://6cb2b084.domainsifter.pages.dev
- Repo: https://github.com/oiramix/domainsifter
- Latest commit on `main`: `49dfbbd` — "Production v1: SEO foundation, DS monogram logo, sample data, 404 page, sitemap, robots.txt"

## Done

### Site build (Astro 4 + Tailwind, static output)
- [x] Project scaffold: `package.json`, `astro.config.mjs`, `tailwind.config.mjs`, `tsconfig.json`, `.gitignore`
- [x] Layout with Inter font, semantic meta, color palette (`#fafaf9` paper / `#1a1a1a` ink / `#0d6e6e` accent)
- [x] Header — DS monogram (white-on-accent rounded square), nav links, "Get the daily list" CTA
- [x] Hero — "Expired domains, sifted daily." headline, pre-launch disclaimer, scroll CTA
- [x] Domain table (the centerpiece):
  - 7 columns: Domain · TLD · Age · Wayback · OpenPageRank · Verdict · Register
  - 20 invented sample domains in `src/data/sample-domains.json`
  - Color-coded TLD and Verdict badges
  - OpenPageRank visualized as horizontal bar
  - Sortable column headers (asc/desc with indicator)
  - Search input (filters by domain name as you type)
  - TLD filter dropdown
  - Mobile card layout at <768px (no horizontal scroll)
  - Empty state when filters match nothing
  - Result count + "Last updated: Preview data" line above table
- [x] Methodology section (`#methodology`) — 3-step visual with inline Lucide-style icons
- [x] Email signup (`#signup`) — client-only, prevents default, shows inline success message
- [x] About section (`#about`) — two paragraphs, no fake metrics
- [x] Footer — copyright, "Built in Estonia 🇪🇪", nav links, ICANN/CZDS disclaimer

### SEO foundation
- [x] `<title>` and `<meta description>` configurable via Layout props
- [x] Canonical URL (`<link rel="canonical">`) generated from `Astro.url.pathname`
- [x] Open Graph: type, title, description, url, site_name, image
- [x] Twitter Card: summary_large_image, title, description, image
- [x] JSON-LD structured data — `WebSite` (with `SearchAction` targeting the table) + `Organization`
- [x] `public/robots.txt` — allows all, points at sitemap-index
- [x] `@astrojs/sitemap` (pinned to `3.2.1` — `3.7.x` is broken on Astro 4) — auto-generates `sitemap-index.xml` + `sitemap-0.xml`
- [x] Custom 404 page (`src/pages/404.astro`) — uses Layout, has `noindex`, "This page got filtered out." copy + back link
- [x] `<html lang="en">`, viewport meta, theme-color, color-scheme

### Deployment
- [x] Repo initialized, `.gitignore` excludes `.claude/`, `*.report.html`, `claude.md`, `node_modules/`, `dist/`, `.astro/`, `.wrangler/`
- [x] Pushed to `github.com/oiramix/domainsifter` on `main`
- [x] Deployed to Cloudflare Pages via Wrangler
- [x] Smoke tests pass: `/` → 200, `/sitemap-index.xml` → 200, `/robots.txt` → 200, `/nonexistent` → 404

## Not yet done — pick up here next session

### Custom domain (immediate next step)
- [ ] Wire `domainsifter.com` (and optionally `www.domainsifter.com`) to the Pages project via the Cloudflare dashboard → Pages → `domainsifter` → Custom domains. Done by hand, not via Wrangler.
- [ ] After DNS propagates, re-run the four curl checks against `https://domainsifter.com/` to confirm parity with `*.pages.dev`.
- [ ] Spot-check that JSON-LD, OG tags, and canonical URLs all resolve to `https://domainsifter.com/...` once the custom domain is live (they already point there in the build, but worth confirming end-to-end).

### Real data pipeline (the actual product)
The whole site is currently driven by 20 hardcoded entries. None of this exists yet:
- [ ] CZDS registration + access to ICANN zone files (.com, .net, .org at minimum)
- [ ] Daily diff job to detect drops (yesterday's zone vs today's zone)
- [ ] Enrichment: Wayback Machine snapshot count, OpenPageRank API, Google Safe Browsing API, language detection
- [ ] Spam/malware/junk filter — score each candidate against the "12+ signals" claim in the methodology copy and reject ~95%
- [ ] Storage layer (Cloudflare D1? KV? R2 + JSON?) and a way to publish a daily snapshot
- [ ] Build-time data load: replace `src/data/sample-domains.json` import with whatever the pipeline produces
- [ ] Swap the "Last updated: Preview data" string for a real ISO timestamp

### Email backend
- [x] Signup form wired to Buttondown's public embed endpoint (`https://buttondown.com/api/emails/embed-subscribe/domainsifter`) — committed 2026-04-30, account created and validated end-to-end 2026-05-13 (first organic subscriber confirmed via double opt-in). No API key in client code; honeypot field + inline JS fetch intercept; `target="_blank"` is the no-JS fallback.
- [x] Decided: pure form POST to provider's hosted embed endpoint. No Worker proxy needed — Buttondown handles spam protection server-side and the embed endpoint is API-key-less by design.

### Affiliate links
- [ ] Apply for and add real affiliate IDs for Namecheap, Dynadot, Porkbun.
- [ ] Replace the placeholder `https://namecheap.com/domains/registration/results/?domain={name}` with the real affiliate URL format.
- [ ] Add a registrar picker (dropdown on the Register button), or rotate, or pick best-price — design call still open.

### Per-domain detail pages
- [ ] Currently every domain link points at `/domain/<name>` which 404s. Either:
  - Build the detail page (Wayback timeline preview, archive screenshot, registrar comparison), or
  - Switch the link target to the affiliate URL directly until the detail page exists.

### Privacy / legal
- [x] Real `/privacy` page exists at [`src/pages/privacy.astro`](src/pages/privacy.astro) — covers what we collect (email + standard logs), data processors (Buttondown for newsletter delivery, Postmark as Buttondown's email infrastructure), and how to delete. Footer link points there.
- [ ] Decide whether the Cloudflare CZDS terms require a specific attribution beyond the footer line.

### Polish / open issues
- [ ] **Logo** — currently a "DS" monogram fallback. The original spec wanted a beagle silhouette; my two SVG attempts didn't read as a dog. Either commission/source a real beagle mark, or keep the monogram and call it done.
- [ ] **Lighthouse SEO score** — claimed 100 in the Definition of Done but never actually measured against the live site. Worth running `npx lighthouse https://domainsifter.com --view` once the custom domain is up.
- [ ] **OG image** — currently falling back to `/favicon.svg`, which most social platforms render poorly. Need a real 1200×630 OG image (PNG/JPG).
- [ ] **`@astrojs/sitemap` upgrade** — pinned to `3.2.1` because `3.7.x` crashes on Astro 4 (`Cannot read properties of undefined (reading 'reduce')`). Bump when upstream fixes it.
- [ ] **6 npm audit vulnerabilities** (4 moderate, 2 high) flagged at install time. Worth a `npm audit` review before anything sensitive ships.

## File layout reference

```
domainsifter/
├── .gitignore
├── README.md
├── STATUS.md                ← this file
├── package.json
├── package-lock.json
├── astro.config.mjs
├── tailwind.config.mjs
├── tsconfig.json
├── public/
│   ├── favicon.svg          (DS monogram on accent square)
│   └── robots.txt
└── src/
    ├── data/
    │   └── sample-domains.json  (20 invented entries)
    ├── layouts/
    │   └── Layout.astro     (SEO meta, OG, Twitter, JSON-LD, font)
    ├── components/
    │   ├── Header.astro
    │   ├── Hero.astro
    │   ├── DomainTable.astro    (vanilla JS sort/filter/search + mobile cards)
    │   ├── Methodology.astro
    │   ├── EmailSignup.astro
    │   ├── About.astro
    │   └── Footer.astro
    └── pages/
        ├── index.astro
        └── 404.astro
```

## Useful commands

```bash
npm run dev              # http://localhost:4321
npm run build            # → dist/
npm run preview          # serve dist/ via Astro
npm run pages:dev        # serve dist/ via wrangler (Cloudflare runtime)
npm run pages:deploy     # deploy dist/ to Cloudflare Pages
```

## Decisions worth remembering

- **Astro static, no SSR, no client hydration.** Vanilla JS in `<script>` tags only. No React/Vue/Svelte.
- **Accent color: `#0d6e6e`** (deep teal). Not forest green. Used for CTAs, badges, focus rings, the monogram, the JSON-LD scaffolding.
- **Inter via Google Fonts**, weights 400 + 600 only. System fallback in Tailwind config.
- **Single source of sample data:** `src/data/sample-domains.json`. Imported directly into `DomainTable.astro`. When the pipeline lands, it just needs to produce JSON with the same shape.
- **Hard rules from the spec, still in force:** no fake testimonials/user counts/launch dates, no stock photos, no pricing page, no chatbot, no cookie banner, no popups. Real domains in sample data are forbidden.
