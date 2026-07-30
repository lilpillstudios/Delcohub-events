# delcohub-events

Nightly event feed for **DelcoHub**. — Lil Pill Studios

The app used to ship 28 hand-written events for an entire county. This repo
replaces that with a scraper that reads the [Delco Times events
calendar](https://www.delcotimes.com/events/) every morning, keeps only what's
actually inside Delaware County, and commits `events.json`. DelcoHub fetches
that file at its raw URL, so new events reach installed phones without a store
release.

Typical run: **~290 events** across the next six months.

## How it works

```
Delco Times calendar (CitySpark portal API)   ~6,800 raw occurrences
        |  point-in-polygon vs. the county boundary
        v
   inside Delaware County                     ~560
        |  drop civic/business/school-calendar noise
        |  collapse repeat occurrences into one event each
        v
   distinct events                            ~270
        |  merge manual_events.json (curated, wins ties)
        |  drop anything already past
        v
   events.json                                ~290
```

**Why the polygon.** The calendar API only searches by radius, and a radius
wide enough to cover Delco also reaches Philadelphia, Wilmington, South Jersey
and half of Chester and Montgomery counties — about 92% of what comes back.
`delco_boundary.json` is the Census cartographic boundary for FIPS 42045; every
event is tested against it by ray-cast. Spot-checked against 23 towns including
the awkward ones (Bryn Mawr, Wayne, Villanova, Chadds Ford, Cobbs Creek) with
no misclassifications.

**Why the grouping.** The source publishes one record per date, so a show with
a two-month run arrives as sixty near-identical rows. Those collapse into one
listing with a human schedule label — "Thursdays, Aug 14 – Oct 2" — and a
`nextDate` for sorting.

## Files

| File | What it is |
|------|-----------|
| `scrape_events.py` | The scraper. Standard library only — no pip install. |
| `delco_boundary.json` | Delaware County boundary polygon (Census FIPS 42045). |
| `manual_events.json` | Curated evergreen events. Always included, and they win any dedup tie. |
| `blocked_venues.json` | Venues to drop entirely. Add a line when somewhere dumps its internal calendar. |
| `events.json` | **The output.** What the app fetches. |
| `.github/workflows/scrape-events.yml` | Nightly run at 5:20 AM Eastern. |

## Running it

```bash
python scrape_events.py --dry-run --horizon-days 30 -v   # look before you write
python scrape_events.py                                  # writes events.json
```

| Flag | Default | Notes |
|---|---|---|
| `--horizon-days` | `180` | How far ahead to scrape. |
| `--output` | `events.json` | |
| `--listings` | `../DelcoHub/src/data/listings.json` | Used to link events to a venue listing. Pass `""` when the app isn't checked out next door. |
| `--dry-run` | | Print the calendar, write nothing. |
| `--verbose` | | Per-window fetch counts. |

## Tuning the noise filters

Everything drops out at one of four gates in `scrape_events.py`:

- `NOISE_TAGS` / `NOISE_TITLE` — township meetings, zoning hearings, realtor
  open houses, chamber mixers, "vote for Best Of" promos.
- `OBSERVANCE` — bare awareness days and holidays ("Yom Kippur", "World Mental
  Health Day"). A *named event* on that day still comes through.
- `SCHOOL_NOISE` / `SCHOOL_KEEP` — private schools post their whole internal
  calendar here (chapel, photo day, dress-down days). Public-facing school
  events — fairs, markets, plays, college athletics — are explicitly kept.
- `blocked_venues.json` — the blunt instrument. Add a venue name, re-run.

If something junky slips through, prefer adding its venue to
`blocked_venues.json` over widening a regex.

## Curated events

`manual_events.json` holds evergreen entries — Dining Under the Stars, the
farmers markets, the Halloween parade. They're always included, they beat
scraped duplicates, and their `nextDate` **rolls forward automatically**: a
weekly event advances a week, an annual event advances a year. That's why they
never go stale even though nobody edits them.

Add one by copying an existing object. Fields follow
[`schema.json`](../DelcoHub/src/data/schema.json) — the same shape as any other
DelcoHub listing, with `category: "events"`.

## Setup (one time)

1. Create a **public** GitHub repo named `delcohub-events` under
   `lilpillstudios` and push this folder to it.
2. Confirm the raw URL resolves — it's what the app reads, and it must match
   `EVENTS_URL` in `DelcoHub/src/useRemoteEvents.js`:
   ```
   https://raw.githubusercontent.com/lilpillstudios/delcohub-events/main/events.json
   ```
3. Actions tab → **Scrape DelcoHub Events** → *Run workflow* to prove it out.
   After that it runs itself.

The workflow refuses to commit a feed with fewer than 40 events or any
malformed record, so an upstream outage can't wipe the Events tab.

## Notes

- No API key, no account, no paid service. The calendar's read endpoint is
  public and unauthenticated; the scraper identifies itself in its User-Agent
  and paces requests.
- Event copy is the submitter's, trimmed to a sentence, with the source venue
  and link preserved. `source` is set to `delcotimes.com` on every scraped row.
- The API caps any single query at ~2,025 results, so the scraper walks the
  calendar in 7-day windows. If a window ever hits that cap it warns loudly —
  lower `WINDOW_DAYS` if you see it.
