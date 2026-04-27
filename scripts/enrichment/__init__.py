"""Enrichment plugin package.

Every enrichment source under this package exposes one function:

    def enrich(domain: str, config: dict) -> dict:
        '''Return a dict of fields this source provides for the domain.
        Returns empty dict if the source is unavailable, the domain is
        not found, or any other failure occurs. NEVER raises.'''

The pipeline calls each source for each candidate, merges the returned dicts
into the per-domain record, and never has to know which source produced which
field. Adding a new source = drop a new module here.

The keys each module returns are documented in that module's docstring and
ultimately consumed by filter.py and score.py via config.json keys.

Current spam-blocklist sources (3): safe_browsing, surbl, spamhaus.
If you add or remove one, update the "three spam blocklists" claim in
src/components/Methodology.astro to match.
"""
