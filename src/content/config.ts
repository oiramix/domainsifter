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
  }),
});

export const collections = { archive };
