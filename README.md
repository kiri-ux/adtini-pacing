# adtini · Pacing

Turns the daily client-serve drops in S3 into the two views the buying team
keeps by hand today: a pacing sheet per order, and one line per order across
every client.

Built to sit beside the quote builder - same Flask/gunicorn shape, same
`adtini.css`, same chrome - so it can move inside adtini later without a
rewrite.

---

## What it does

Two kinds of file land in `s3://adtini-orders/orders/`, told apart by their
filename:

| Filename | What it carries | Grain |
|---|---|---|
| `client-serve*` | delivery - impressions, clicks, spend, conversions | one row per day per strategy |
| `orders*` | the sold side - totals, flight dates, budgets | one row per line item |

They join on the ids they share, `order_id` and `line_item_id`, so nothing is
matched on names. The orders file is read first on each sweep, so a line item
exists for that day's delivery to land against.

**Delivery in.** Each drop is a rolling 31-day window, so days arrive
repeatedly; rows are replaced, not appended, and the numbers are always the
latest ones.

**Sold terms in.** Imported from the orders drops, so nothing is typed in by
hand. Anything a buyer does edit is marked buyer-maintained and the next
import leaves it alone - budgets get adjusted mid-flight and the adjustment
has to survive. "Hand back to orders file" undoes that.

**Which orders get a page.** Only Insertion Orders. A Cancelled order is kept
rather than dropped, because it may have run for months before it was
cancelled - it appears if it delivered, and is marked `cancelled`. An order
that never ran at all (Draft, Declined) is out.

**Pacing out.** Three layouts, matching the three tabs:

| Type | Used for | Paces on |
|---|---|---|
| Impression | most orders | delivered impressions |
| Click | PPC, LinkedIn | **ad spend** |
| Event | Performance Max | **client cost against the client's budget** |

Click orders pace on the ad spend - `total_ppc_ad_spend`,
`total_linkedin_ad_spend` - and not on the client's monthly budget, which
carries the management fee that never reaches the platform. The spend column
is chosen by keyword rather than an exact product name, because the delivery
feed says "PPC" where the orders export says something longer, and an exact
map silently fell through to the budget.

Event orders pace what the client is billed against what the client is
charged. The feed reports platform cost, so it is grossed up by the ratio the
order was sold at - `client_total_budget / google_total_spend`, typically 4x
- and the page names the multiplier it used. Targeting the platform spend
instead, as it did first, made every Performance Max order read as under by
the size of the fee however it was running.

Orders are classified automatically by product and data source, and a buyer
can override the type per order.

### The math

Read off the hand-kept sheets and checked against them in
`tests/test_pacing.py`:

```
flight_days  = (end - start) + 1              inclusive
day_target   = total_sold / flight_days
on_pace      = day_target x days_run          days_run capped at flight_days
to_date      = delivery summed inside the flight
pacing       = (on_pace - to_date) / on_pace
```

The sign convention is the sheet's: **negative is over-delivering, positive is
under.** Within ±10% shows as on pace.

Monthly pacing works the same way on the part of the calendar month the flight
actually covers - a flight starting on the 17th owes its monthly impressions
in 14 days, not 30.

### Which CPM

Three different CPMs exist for the same line item and they are not
interchangeable:

| | What it is | Where it comes from | Used for |
|---|---|---|---|
| **Setup CPM** | what the DSP campaign is built at | the rate card | **pacing** |
| **Retail CPM** | what the client is billed | orders file: budget ÷ impressions | margin |
| **Partner hard cost** | what the supply partner charges | the rate card | margin |

Pacing runs on the setup rate. Pacing on the retail rate would show a budget
the buying team never bought at - a Display line is set up at $2.50, which is
the card's Max and matches the hand-kept sheet, while its retail rate is
several times that.

The card lives in `data/rate_card.csv`, versioned so a rate change is a
reviewable commit, and moves into the database the day the team wants to edit
it in the app. It also carries each product's performance goal (0.40% CTR for
Display, 90% VR for CTV) and the partner hard cost, so the order page can show
delivered CTR against the goal and margin against the 50% target. Restricted
categories take their own higher entry, keyed off the `restricted` flag the
delivery feed carries.

Products bought on budget rather than a rate - PPC, LinkedIn, Performance Max -
are deliberately absent from the card and have no CPM at all.

Each line item records where its rate came from, so the page can say "from
rate card" rather than leaving a buyer to guess which of the three they are
looking at.

### The pacing table

The homepage is one line per order: buyer, partner, client, order id, a pill
per product, then monthly and total serve against goal, the daily rate
against what is needed from here, and days left. A `Pacing total` line sums
the page - impression orders only, since adding dollars to impressions gives
a number that means nothing.

Each pacing cell carries two figures, because one cannot answer the question
on its own. The **bar fills** to how much of the goal has run and its **tick**
marks where it should be by now; the **number** beside it is the ratio of the
two, so 100% is on pace, under is behind and over is ahead. A bar at 60% is
early or late depending on the date.

Hovering a bar gives served, expected and goal; hovering a product pill gives
that product's served and expected without opening the order. Both are
markup rather than `title` attributes, so they hold more than one line and
appear on keyboard focus.

Opening an order gives the same columns per line item, plus clicks, CTR and
conversions.

### Seeing the strategies apart

The order page charts delivery per strategy per day, so retargeting behaving
differently from behavioral is visible rather than buried in a line item
total. Lines are grouped by targeting, not by the feed's strategy id: one line
item routinely carries twenty ids that are the same targeting re-flighted, all
named identically, and twenty indistinguishable lines answer nothing. Past
eight series the tail folds into "Other" rather than colours being reused.

There is deliberately no daily-target line on that chart. The target is the
whole order's, and drawing it against a single strategy invites reading that
strategy as behind when the order is fine.

---

## Setting it up on Render

You asked what to create: **a Web Service, a Cron Job and a Postgres
instance.** `render.yaml` defines all three, so the fastest path is a
Blueprint rather than making them by hand.

### 1. Create the services

Render dashboard → **New → Blueprint** → pick this repo. It reads
`render.yaml` and creates:

| Service | Type | Plan | What it does |
|---|---|---|---|
| `adtini-pacing` | Web Service (Python) | Starter | the tool itself |
| `adtini-pacing-ingest` | Cron Job (Python) | Starter | pulls S3 daily at 13:00 UTC |
| `adtini-pacing-db` | Postgres | Basic 256MB | shared by both |

Starter is the right size to begin with. The web service needs a paid plan
regardless - Free spins down after inactivity, and a cold start on a table
this size is a bad first impression for the team. Storage is the thing to
watch: a year of daily delivery at the current volume is roughly 1.5-2 GB, so
expect to move the database up a tier within the first year.

### 2. Set the secrets

Four values are marked `sync: false` in the blueprint, meaning Render prompts
you rather than storing them in git. Set them on **both** the web service and
the cron job:

| Key | Value |
|---|---|
| `AWS_ACCESS_KEY_ID` | IAM key for the bucket |
| `AWS_SECRET_ACCESS_KEY` | its secret |
| `APP_PASSWORD` | shared password for the buying team |
| `SESSION_SECRET` | web service only; Render generates it |

`DATABASE_URL` is wired to the Postgres instance automatically. `S3_BUCKET`,
`S3_PREFIX` and `AWS_REGION` are already in the blueprint - change them there
if the bucket moves.

### 3. The IAM user

The credentials only need to read the one prefix. Anything wider is more
access than the tool has any use for:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::adtini-orders",
      "Condition": {"StringLike": {"s3:prefix": ["orders/*"]}}
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject"],
      "Resource": "arn:aws:s3:::adtini-orders/orders/*"
    }
  ]
}
```

### 4. pacing.reporting.zone

On the **web service** → Settings → Custom Domains → add
`pacing.reporting.zone`. Render then shows you the record to create at
whoever hosts `reporting.zone`:

```
Type   Name      Value
CNAME  pacing    adtini-pacing.onrender.com
```

Render issues the TLS certificate itself once the record resolves, usually
within a few minutes. No certificate to buy or renew.

### 5. First run

1. Open the site and sign in with `APP_PASSWORD`.
2. Go to **Data** → **Run sweep**. The first sweep loads every drop in the
   bucket - orders first, then delivery - which takes a couple of minutes for
   a month of files.
3. Go to **Pacing**. Orders that came through with their sold terms are
   already pacing.
4. Filter to **Sold terms: needs terms** to see anything the orders files did
   not cover, and fill those in by hand.

After that the cron job keeps both sides current on its own.

---

## Schema changes

The schema is Alembic's, applied by the web service's pre-deploy command
(`alembic upgrade head`) before traffic moves to the new version. Nothing in
the app creates or alters tables.

That is not a style preference. The first deploy built its tables with
`create_all`, which creates missing tables but never alters existing ones, so
the columns added in the next deploy were never applied and every page failed
on `column orders.order_type does not exist`. `/healthz` now reports that
case specifically - it returns 503 naming the missing tables or the failing
column, rather than going green on a process that cannot serve a page.

After changing a model:

```bash
alembic revision --autogenerate -m "what changed"
alembic upgrade head
```

## Running it locally

```bash
pip install -r requirements.txt
cp .env.example .env          # fill in the AWS keys, or skip and upload a file
alembic upgrade head          # `python app.py` also does this for you
python app.py                 # http://127.0.0.1:5000
```

With no `DATABASE_URL` it uses a local SQLite file, and with no `APP_PASSWORD`
it skips the login gate. To work without S3 access, drop a CSV or zip on the
**Data** page - it takes the same file the bucket does.

```bash
python -m pytest tests/ -q    # 67 tests
python scripts/ingest.py      # what the cron job runs
```

---

## Layout

```
app.py              Flask routes, template filters, the login gate
models.py           schema: order book, delivery feed, ingest log
config.py  db.py    environment and SQLAlchemy setup

pacing/
  calendar.py       flight windows, elapsed days, month clipping
  engine.py         the three pacing types, and the Total row

migrations/         Alembic; the schema's only owner
ingest/
  s3.py             listing and fetching the drops
  normalize.py      the delivery export's quirks
  orders.py         the orders export's quirks, and its alias table
  loader.py         routes by filename, upserts, ingest log

sheets.py           parses the hand-kept pacing sheets
ratecard.py         setup CPM, goal CTR/VR and partner cost per product
data/rate_card.csv  the card itself, versioned
orderbook.py        imports orders; classification and labelling rules
views.py            read models for the two pages, and the chart series
exports.py          XLSX in the same column order as the sheets
static/chart.js     the per-strategy daily chart
scripts/ingest.py   the cron entrypoint
```

---

## The strategy split

The orders drop stops at the product line item; the delivery drop is per
strategy; only the buying team's hand-kept sheets say how a line item's sold
impressions are split across its strategies. Those are parsed by `sheets.py`
into `data/strategy_seed.csv` and applied by
`scripts/import_strategy_seed.py` (`--dry-run` to see what would match).

A seed, not a feed. The sheets are hand-kept, so reading them on a schedule
would have them fighting the nightly orders import; what lands is the tool's
afterwards.

Sections match an order by its order number where the sheet carries one, and
by client name and flight dates where it does not. Anything matching nothing
is reported rather than dropped.

Sold rows pair with delivery on the **targeting** (behavioral, retargeting,
AI, geo-fencing...), not the whole label, because the two name products
differently - "FB/IG - Category" against "FB - Category Facebook". One
consequence is visible on the page: where a sheet splits one targeting across
two products, both rows show that targeting's whole delivery rather than a
share of it.

## Memory

The Starter instance is 512 MB and the app is ~110 MB resident per worker
before it serves anything, so it runs **one** gunicorn worker with threads.
Two workers plus an ingest overran the limit, the worker was OOM-killed
mid-request, and it surfaced as a 502.

Delivery files are read in chunks and never held whole: a 69 MB drop read at
once peaked at 415 MB, against 188 MB chunked. Drops are streamed from S3 to
a temp file rather than through memory, and uploads likewise.

Because a file is read in chunks, the summing happens in the database rather
than per chunk - chunks land in `delivery_staging` unaggregated and are
folded into `daily_delivery` in one grouped upsert. Aggregating per chunk
would let a grain that straddles a chunk boundary be counted from only its
last chunk.

**Load one file at a time while you are testing.** The **Data** page lists
what is in the bucket with its size and whether it has been loaded, and each
row has its own Load button. The sweep skips anything over a size limit
(50MB by default), so the multi-gigabyte bulk exports stay out of the way
until you ask for one: `orders-db-all-*` alone is 3.3GB against 2.4MB for all
the per-unit files put together, and they largely say the same thing.

Under it: `python scripts/ingest.py --only <key>` and `--max-mb N`.

**The sweep does not run in the web service.** The button on the **Data**
page starts `scripts/ingest.py` as a separate process and returns straight
away; the page shows files arriving as they land. Run inside the request it
took the whole service down - Render restarted the instance for exceeding
its memory limit and every open page got a 502. Measured during a sweep now:
114 MB for the web worker plus 188 MB for the ingest, against a 512 MB limit.

Starting a sweep redirects rather than rendering the page, so refreshing -
which the page asks you to do - does not re-post the form and start a second
one. A sweep already running refuses a second anyway, checked by pid so a
stale lock from a container restart does not wedge the button.

A sweep is resumable, so a restart mid-run loses nothing: files already
loaded are skipped by their ETag. The nightly cron job runs the same script
on its own instance, and is the normal path - the button is for a backfill
you do not want to wait a day for.

Uploading a file still runs in the web worker, one file at a time, which the
chunked reader keeps inside about 190 MB. Use the sweep for a backlog.

## Things worth knowing

**History starts where the feed does.** Each drop carries 31 days, so on day
one the tool only knows about the last month. An order whose flight started
earlier has a life-of-flight figure that is short by whatever ran before -
pages mark those `PARTIAL` rather than quietly under-reporting. Month-to-date
is unaffected, and the gap closes as daily drops accumulate. If there is an
archive of older drops in the bucket, the first sweep picks them all up.

**Both exports are messy, and the ingest layer absorbs it.**

For delivery (`ingest/normalize.py`): `order_id` is blank on Adlib and beta
rows, the header ships two columns both called `goal_cpm_` (neither is used -
`goal_internal_cpm` is), 17-digit Meta campaign ids have to be read as text or
they lose their last digits, and one strategy running several creatives
produces several rows for a day, which are summed. A blank `line_item_id` gets
a deterministic key of its own, because leaving it empty collapsed 3% of rows
across hundreds of unrelated orders into one bucket.

For orders (`ingest/orders.py`): **`total_campaign_impressions` does not
hold a total** - it carries `0.999999999999` on every row of the real
exports, some ratio artifact, and the other three copies of the column are
empty. Read straight through it made every sold total 1, so every impression
order showed a $0.00 budget and a meaningless pacing percent. The total is
`monthly_campaign_impressions x months_running`, which is what the hand-kept
sheet computes too; a value at least as large as the monthly one is believed
and kept. `months_running` is the line item's own - not
`client_months_running`, which is how long they have been a client (140 for
an order whose line ran 70).

Also: ids arrive as HTML
(`<a href="...viewOrder/2873">2873</a>`), header names repeat (`start_date`
twice, `total_campaign_impressions` four times, `months_running`
thirty-four times) with the value in whichever copy happens to carry it, dates
carry a time, the buyer carries their email, and the flattened join repeats
every line item many times over. Column matching is by alias on a simplified
header name, so the two exports seen so far - which differ from each other -
both map cleanly. Anything unrecognised is reported on the **Data** page
rather than silently dropped, so a changed export gets noticed.

**The feed's ids do not respect their own column.** `line_item_id` sometimes
carries a name rather than an id, and a key built from a strategy name runs
to 130 characters, against a 64-character column. Anything over the limit is
truncated with a hash of the original appended, so it stays unique and still
reads as itself; real numeric ids are far below the limit and pass through
untouched, so the join to the orders file is unaffected. Postgres rejects an
overlong value and SQLite silently keeps it, so this only appeared when the
ingest was run against the real engine.

**Two strategies can share a name.** The feed ships distinct strategy ids
under one name - two Meta ad sets both called "Facebook/Instagram Premium" -
so labels get a `(2)` suffix to keep them apart. Rows a buyer cannot tell
apart are rows they cannot pace.

**Adtini reporting, later.** The pacing engine reads `DailyDelivery` and
nothing else. Pointing it at Adtini's own reporting instead of the S3 drops
means writing a second loader into the same table; the engine, views and
exports do not change.


**The chart's palette is computed, not chosen.** The eight categorical colours
in `static/chart.js` are validated against the lightness band, chroma floor,
colour-vision separation, normal-vision floor and 3:1 contrast on a white
surface. The order is the colour-vision safety mechanism, so it must not be
reordered, and a ninth series folds into "Other" rather than reusing a colour.
Strategy names arrive from the feed and the orders export is known to carry
HTML in its fields, so every label goes into the DOM with `textContent` and
the chart reads its data from a JSON block rather than from generated script.
