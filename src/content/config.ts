// Astro Content Collections schema for the archive subsystem.
//
// archive_generator.py writes one Markdown file per qualifying domain to
// src/content/archive/{name}.md with the frontmatter below. The body is
// Haiku-generated and lives below the frontmatter as plain Markdown.
//
// Why a separate collection rather than reading the .md files raw: Content
// Collections gives the dynamic route a typed enumeration via
// getCollection('archive'), keeps the schema honest (zod validates each
// frontmatter at build time — a missing field fails the build loud
// instead of producing a half-rendered page), and lets the archive index
// page share the same query shape.

import { defineCollection, z } from 'astro:content';

const archive = defineCollection({
  type: 'content',
  schema: z.object({
    name: z.string(),
    tld: z.string(),
    verdict: z.enum(['Clean', 'Promising']),
    score: z.number(),
    dropped_date: z.string(),          // ISO YYYY-MM-DD
    archived_date: z.string(),         // ISO YYYY-MM-DD — when this entry was generated
    wayback_snapshots: z.number().nullable(),
    wayback_last_snapshot: z.string().nullable(),
    open_page_rank: z.number().nullable(),
    cc_source_domain_count: z.number().nullable(),
    cert_history: z.boolean().nullable(),
    first_seen_date: z.string().nullable().optional(),
    availability_verified_at: z.string().nullable().optional(),

    // --- Evidence block (added 2026-09-19) ---------------------------------
    // Why these live here rather than being read live from
    // src/data/daily-domains.json and src/data/wayback_excerpts.json: the
    // daily list is a 14-day rolling window and an archive page is permanent.
    // None of the 113 pages written before this date still appear in the
    // daily JSON, so a live read renders the evidence for about two weeks and
    // then silently drops it on the next rebuild. archive_generator captures
    // it into the Markdown once, at the moment it was true.
    //
    // EVERY key below is .optional() AND .nullable(), and must stay that way:
    // those 113 existing files have none of them, and one required field
    // fails `npm run build` for the entire site rather than for one page.
    // The generator omits a key entirely when it has no value, so `absent`
    // is the normal shape — nullable() only guards a hand-edited file.
    //
    // excerpt_* is untrusted third-party text scraped from archived pages:
    // frequently spam, frequently non-English. archive_generator strips
    // control/bidi characters, collapses whitespace and caps lengths before
    // writing, but the page must still interpolate it with {} (never
    // set:html) so Astro escapes it.
    phase2_reason: z.string().nullable().optional(),
    snapshot_category: z.string().nullable().optional(),
    excerpt_title: z.string().nullable().optional(),
    excerpt_meta_description: z.string().nullable().optional(),
    excerpt_h1: z.array(z.string()).nullable().optional(),
    excerpt_h2: z.array(z.string()).nullable().optional(),
    // Wayback capture time (14 digits, YYYYMMDDhhmmss) for the excerpt
    // above. Written only when there is excerpt text for it to date, so the
    // page can always attribute the quoted content to a capture and a date
    // rather than presenting undated third-party text as evidence.
    excerpt_snapshot_timestamp: z.string().nullable().optional(),
  }),
});

export const collections = { archive };
