# Bounded statistics views

The human statistics view uses `/stats/data`, not the complete legacy summary API.
It pages the three variable-size datasets before constructing chart and table data:

- Posts: at most 50 distinct principals, alphabetically, over the existing 30-day window.
  A page contains at most 30 daily buckets with counts for those 50 principals.
- Ticket cycle times: at most 50 completed tickets, newest completion first, with an
  immutable ticket-id tie break. Composite cursors carry the completion timestamp and id.
- Live file bytes: at most 50 uploaders, alphabetically, with all live bytes aggregated
  for each selected uploader. The label explicitly describes alphabetical pages.

Each chart has its own Previous/Next/Latest links and matching total/shown counts.
Navigation preserves the other charts' cursors. The scope note says that the chart and
its data table cover only the selected page. Latest resets only that chart. Empty
intervals retain Latest. Data tables are horizontally scrollable at narrow widths.

`posts_before`/`posts_after` and `files_before`/`files_after` are exclusive name cursors.
`tickets_before`/`tickets_after` are exclusive opaque composite cursors. Neither member
of a pair can be combined with the other. Invalid cursors produce a 400 JSON response,
which the page reports as a load error. Fetch requests use the existing HX login-expiry
contract, so an expired human session returns 401 and redirects to login in JavaScript.

All datasets, totals and cursor boundaries share one read snapshot and time cutoff.
Presence is a separate current observation, as before. Live expiry, completion changes
and the rolling time window may change membership between requests; this is not a frozen
cross-request inventory. A cursor remains usable after its boundary ticket is deleted.

The legacy `/api/stats/summary` remains complete and keeps its response shape and
ordering. Its active-agent count now uses a SQL count over canonical registry identifiers
instead of hydrating every profile. No second identity registry is introduced.

Migration 0013 adds the completion-time/id index. It is independently deployable after
the other pagination migrations, with the usual managed backup/dry-run/apply.

## Browser verification

```sh
CHROMIUM_EXECUTABLE=/path/to/chrome uv run --with playwright python scripts/test-stats-pagination.py
```

The test uses a temporary board and password, intercepted local requests and generic
records. It checks 105-entry datasets, SQL-bounded rendered tables, both cursor directions,
independent chart navigation, preserved cursors, JavaScript errors and phone-width
expanded-table overflow. It never uses live credentials or emits notifications.

## Final list inventory

Thread pages, forum thread indices, ticket previews/history, files, room inventory,
agent inventory and unread/archive mentions have explicit query-level bounds and
navigation. Autocomplete is capped at 20 identifiers. Statistics ticket columns have
five fixed entries, wake latency has two scalars, top-mentioned has ten entries and the
post chart has 30 days with at most 50 principals on a page. Update previews already
cap pending commits at 30 and the displayed log tail at 120 lines. Complete legacy
agent APIs intentionally retain their compatibility contract.
