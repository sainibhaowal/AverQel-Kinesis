# 02. Metrics and tenant analytics

## Use case

Operators need service-wide health counters, while tenant administrators need
limited usage trends for their own tenant. These are separate views with
different access rules.

## Why it matters

Raw Prometheus metrics include operational labels and stay available to the
configured scraper. The JSON metrics summary is more restricted because it
combines service-wide counters. Tenant analytics can reveal activity patterns,
so the API suppresses small groups and never returns query text or user IDs.

## How it works

- `GET /api/v1/metrics` serves the Prometheus scrape endpoint without user
  authentication for scraper compatibility.
- `GET /api/v1/metrics/summary` returns only request, error, and database-query
  totals. It requires an admin account whose email is in the platform
  administrator allowlist.
- `GET /api/v1/analytics/dashboard` requires `admin:analytics:read` and uses
  the authenticated tenant context. It covers the rolling 30-day window.
- The analytics response suppresses the total until at least five distinct
  tenant users contributed. Daily counts are shown only when that day also has
  at least five distinct contributors. It never includes prompts, answers, or
  user identifiers.

## What to watch for

- Keep tenant filters in every analytics query in addition to the database
  tenant context.
- Keep the suppression threshold on both overall and daily aggregates; a safe
  total does not make a small daily group safe to disclose.
- Do not expose service-wide counters through the tenant analytics endpoint.
- The raw Prometheus endpoint is intentionally unauthenticated. Restrict its
  network reachability to the deployment's scraper where the environment
  supports that control.
