# ANSWERS — Parts B, C and D

---

## Part B — Diagnose three broken snippets

### Snippet 1 — overdue report view

**1. What is wrong**

| # | Defect | Consequence in production |
|---|---|---|
| 1 | **N+1 queries.** `c.asset.name`, `c.asset.asset_tag` and `c.employee.full_name` each lazily load a related row. That is 2 extra queries per open check-out (asset and employee are fetched once each per instance, then cached). | 5,000 open check-outs = 10,001 queries. Latency grows linearly with data. |
| 2 | **The overdue filter runs in Python, not SQL.** The queryset is *every open* check-out, and `due_at < now` is checked in the loop. | Every open row is transferred, instantiated and held in memory (a Django queryset caches its results), including all the ones that are not overdue. |
| 3 | **Sorting in Python by a truncated integer.** `days_overdue` is floored to whole days, so everything 2.1 and 2.9 days late ties, and ties come out in arbitrary order. The database could return the rows already ordered by `due_at`. | Unstable, wrong ordering within a day; an extra O(n log n) sort in the web process. |
| 4 | **`timezone.now()` is called once per row, twice per overdue row.** The row set is judged against a moving clock. | On a big set the first and last rows are evaluated at different instants; an item can pass the `<` check and then compute against a later "now". Harmless-looking, but the report is not a consistent snapshot. |
| 5 | **No pagination / unbounded response.** | Response size and memory grow without limit. Eventually the request hits the gateway timeout. |
| 6 | **No authentication or permission check.** It is a plain Django view (not DRF), so the project's DRF `IsAuthenticated` default does not apply, and there is no `@login_required`. It also accepts any HTTP method. | Anyone who can reach the URL gets employee names. |
| 7 | **Spec mismatch:** the report must include the employee *code*; this returns only the name. | Consumers cannot join the report back to employees reliably (names are not unique). |

**2. Why it looks correct locally**

- **The N+1 and the Python filtering are invisible with 10 rows.** 21 queries against a local database take a few milliseconds, and nothing on the page counts queries (no debug toolbar, no query-count assertion).
- **Sorting looks right** because local test data rarely has two items overdue by the same whole number of days.
- **The moving `now` needs a large set.** The drift is microseconds on 10 rows.
- **The missing auth is hidden** because the developer is usually logged into `/admin/`, so the session cookie is present anyway.

**3. Fix**

```python
from django.utils import timezone
from rest_framework import generics

class OverdueReportView(generics.ListAPIView):          # DRF: auth + pagination apply
    serializer_class = OverdueRowSerializer              # includes employee_code + days_overdue

    def get_queryset(self):
        self.now = timezone.now()                        # ONE instant for filter and arithmetic
        return (CheckOut.objects
                .filter(returned_at__isnull=True, due_at__lt=self.now)   # filter in SQL
                .select_related("asset", "employee")                     # 1 JOINed query, not 2N+1
                .only("id", "due_at", "asset__name", "asset__asset_tag",
                      "employee__employee_code", "employee__full_name")
                .order_by("due_at", "id"))                               # most overdue first, stable

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "now": self.now}
```

This is what `assets/views.py::OverdueReportView` and `assets/selectors.py::overdue_checkouts` in this repository do. The partial index `checkout_open_due_idx (due_at) WHERE returned_at IS NULL` serves both the filter and the ordering.

**4. What would have caught it**

- **A query-count test:** `django_assert_max_num_queries` with 15+ rows, as in `tests/test_reports.py::test_query_count_does_not_grow_with_rows`. A test that asserts a *constant* count fails the moment someone removes `select_related`.
- **In development:** django-debug-toolbar or `nplusone`, which raises on lazy loads in DEBUG.
- **An anonymous-client test** asserting 401/403 catches the missing authentication.
- **A test with two items overdue by the same whole number of days** catches the unstable order.

---

### Snippet 2 — check-out endpoint

**1. What is wrong**

| # | Defect | Consequence |
|---|---|---|
| 1 | **Check-then-act race on the asset.** `asset.status` is read, then (without any lock) a CheckOut is inserted. Two concurrent requests both read `AVAILABLE` and both insert. | **Double booking**: one asset checked out to two people. Rule 7 is violated. |
| 2 | **Check-then-act race on the three-item limit.** `open_count` is read without locking the employee, so two concurrent check-outs of different assets by the same employee both count 2 and both insert. | An employee can hold 4+ items. Rule 3 is violated. |
| 3 | **No transaction.** `CheckOut.objects.create` commits (autocommit) before `asset.save()` runs. If the save fails, or the process is killed between the two statements, a CheckOut exists while the asset is still `AVAILABLE`. | Violates rule 5 exactly ("a check-out row must never exist alongside an AVAILABLE asset"). Worse, the asset can then be checked out again. |
| 4 | **Lost update: `asset.save()` without `update_fields`.** It writes every column from an in-memory copy that may be seconds old. | Overwrites concurrent changes to the asset: a rename, or someone moving it to `MAINTENANCE` in the meantime. |
| 5 | **Unknown tag or code → 500.** `Asset.objects.get` / `Employee.objects.get` raise `DoesNotExist`, which DRF does not map to 404. | Violates rule 8; clients see a server error for a typo. |
| 6 | **Missing field → 500.** `request.data["asset_tag"]` raises `KeyError`. | Violates the "400, not 500" contract. |
| 7 | **Inactive employees are not checked.** | Violates rule 2. |
| 8 | **`due_at` is not validated at all.** It is not checked for the future or the 30-day limit, and not parsed: a malformed string reaches the model and fails during the INSERT. | Violates rule 4; bad input becomes a 500. |
| 9 | **Hard-coded status strings** instead of `Asset.Status.AVAILABLE`. | A typo silently never matches. Minor, but it is how rule checks rot. |
| 10 | **Authorization depends entirely on global DRF settings.** No `permission_classes` on the view. If the project default is `AllowAny` (DRF's own default), anyone can check assets out. | Worth stating explicitly on a write endpoint. |

**2. Why it looks correct locally**

- **The races need two requests inside the same few milliseconds.** One developer clicking a button never produces that. Even a naive threaded test usually passes: the first request often commits before the second one reads (I hit exactly this in this repository; see the commit "Concurrency tests passed with the locks deleted").
- **The missing transaction needs a failure between two statements.** Locally the database never fails there.
- **The lost update needs a concurrent writer to the same asset.**
- **The 500s only appear with bad input,** and hand-testing uses valid tags.
- **With `DEBUG=True` a 500 shows a helpful traceback page,** so it feels like a validation error rather than an outage.

**3. Fix**

```python
from django.db import IntegrityError, transaction

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def check_out_asset(request):
    data = CheckOutCreateSerializer(data=request.data)        # 400 on missing/malformed fields
    data.is_valid(raise_exception=True)
    v = data.validated_data
    validate_due_at(v["due_at"])                               # future, <= now + 30 days -> 400

    try:
        with transaction.atomic():                             # rule 5: both writes or neither
            asset = get_object_or_404(Asset.objects.select_for_update(), asset_tag=v["asset_tag"])
            employee = get_object_or_404(Employee.objects.select_for_update(),
                                         employee_code=v["employee_code"])       # rule 8 -> 404
            if not employee.is_active:
                raise ValidationError({"employee_code": "inactive"})             # rule 2 -> 400
            if asset.status != Asset.Status.AVAILABLE:                           # read under lock
                raise Conflict("not available")                                  # rule 1/7 -> 409
            if CheckOut.objects.filter(employee=employee, returned_at__isnull=True).count() >= 3:
                raise Conflict("limit reached")                                  # rule 3 -> 409
            checkout = CheckOut.objects.create(asset=asset, employee=employee, due_at=v["due_at"])
            asset.status = Asset.Status.CHECKED_OUT
            asset.save(update_fields=["status", "updated_at"])                   # no lost update
    except IntegrityError:                                     # partial unique index backstop
        raise Conflict("already checked out")
    return Response(CheckOutSerializer(checkout).data, status=201)
```

Plus the database constraint, so the invariant holds even if a future code path forgets the lock:

```python
models.UniqueConstraint(fields=["asset"], condition=Q(returned_at__isnull=True),
                        name="uniq_open_checkout_per_asset")
```

Locks are always taken in the order Asset → Employee, and the return path never locks an Employee, so the two paths cannot deadlock. This is `assets/services.py::check_out` in the repository.

**4. What would have caught it**

- **A real concurrency test:** `django_db(transaction=True)`, two threads, a barrier, and a widened race window (`tests/test_concurrency.py`). It is only trustworthy after **mutation-checking** it: delete the lock and confirm the test goes red. Mine did not at first.
- **The partial unique index itself.** It turns a silent double-booking into a loud IntegrityError in any environment with concurrent traffic.
- **A fault-injection test** that makes `Asset.save` raise after the INSERT, then asserts that no CheckOut row exists (`test_rule5_failure_after_insert_rolls_back_both_writes`).
- **Negative-input tests** (unknown tag, missing field) asserting 404/400.
- **A load test** (locust or k6) firing parallel check-outs at one asset.

---

### Snippet 3 — nightly notice task

**1. What is wrong**

| # | Defect | Consequence |
|---|---|---|
| 1 | **Not idempotent.** Each run creates a notice per overdue check-out and does not check whether one already exists for today. | With the Part A unique constraint `(checkout, notice_date)`: the first duplicate raises `IntegrityError`, the task dies mid-loop, and every check-out after that point gets no notice. A retry (or `acks_late` redelivery) dies at the same row again, forever. Without the constraint: duplicate notices, and duplicate emails, on every run. |
| 2 | **Emails are sent even when the notice was not new, and before anything is committed.** `deliver_email.delay()` is queued in the same loop iteration as the notice. | If the task fails at row 5,000 and is retried, rows 1–4,999 are emailed **again**. If it runs hourly (or Beat double-fires), employees are spammed. Because the notice and the email are not tied to a transaction, a worker can also send an email for a notice that was never committed. |
| 3 | **Model instances passed to `.delay()`.** `c.employee` and `c` are passed as task arguments. | With Celery's default JSON serializer, this raises `kombu.exceptions.EncodeError` in production. With pickle, it ships a stale snapshot (and pickle over a broker is a security risk). Pass primary keys; the task re-reads fresh state. |
| 4 | **N+1:** `c.employee` lazily loads per row. | Tens of thousands of extra queries. |
| 5 | **Unbounded memory and one INSERT per row.** Iterating a queryset caches every instance; `create()` is one round trip per row; tens of thousands of broker messages go out one by one. | At tens of thousands of rows: large memory spikes in the worker, a slow run, broker pressure. Long runs also widen the window for failure #1. |
| 6 | **Two different "now"s, and a UTC date.** The filter uses one `timezone.now()`. `notice_date` uses another, and `.date()` on it is the **UTC** date. | A run spanning midnight dates notices inconsistently. For an Indian workforce, the UTC date is "yesterday" until 05:30 IST, so notices land on the wrong business day. |
| 7 | **The return value is wrong and costs another query.** `overdue.count()` re-runs the query *after* the loop. | It reports the current overdue set, not the number of notices sent. Items returned mid-run, or rows skipped after a failure, make the number wrong. Monitoring built on it lies. |
| 8 | **No retry policy for transient errors.** A DB blip fails the whole run with no automatic retry. Adding a naive retry makes #1 and #2 worse. | Silent gaps in notices. |

**2. Why it looks correct locally**

- **Idempotency bugs need a second run on the same day.** Development runs it once.
- **Retries need a failure mid-loop.** It never fails locally.
- **Serialization is hidden by eager mode.** `CELERY_TASK_ALWAYS_EAGER=True` (common in tests and development) calls `deliver_email` in-process without serializing its arguments, so passing model instances "works".
- **The N+1 and memory problems need volume.** With 20 overdue rows they cost nothing.
- **The UTC date only differs between 00:00 and 05:30 IST,** outside working hours.

**3. Fix**

Make the notice creation idempotent at the database level, send emails only for notices that are genuinely new and only after they commit, pass ids, and batch.

```python
from itertools import islice
from django.db import transaction

def chunks(it, n):
    it = iter(it)
    while batch := list(islice(it, n)):
        yield batch

@shared_task(bind=True, acks_late=True, autoretry_for=(OperationalError,),
             retry_backoff=True, max_retries=5)
def send_overdue_notices(self):
    now = timezone.now()                                   # one instant
    today = timezone.localdate(now)                        # business-timezone date
    candidate_ids = (CheckOut.objects
                     .filter(returned_at__isnull=True, due_at__lt=now)
                     .exclude(notices__notice_date=today)  # skip already-noticed (retry-safe)
                     .order_by("id")
                     .values_list("id", flat=True))
    created = 0
    for batch in chunks(candidate_ids.iterator(chunk_size=1000), 1000):
        with transaction.atomic():
            OverdueNotice.objects.bulk_create(
                [OverdueNotice(checkout_id=cid, notice_date=today) for cid in batch],
                ignore_conflicts=True,                     # ON CONFLICT DO NOTHING
            )
            created += len(batch)
            # Queue emails only once the notices are durable.
            transaction.on_commit(lambda b=batch: [
                deliver_email.delay(checkout_id=cid, notice_date=today.isoformat()) for cid in b
            ])
    return created


@shared_task(acks_late=True)
def deliver_email(checkout_id: int, notice_date: str):
    # Claim the send atomically. Only one worker can flip emailed_at from NULL,
    # so concurrent or duplicated messages still produce one email.
    claimed = (OverdueNotice.objects
               .filter(checkout_id=checkout_id, notice_date=notice_date, emailed_at__isnull=True)
               .update(emailed_at=timezone.now()))
    if not claimed:
        return "already sent"
    checkout = CheckOut.objects.select_related("employee", "asset").get(pk=checkout_id)
    send_mail(...)  # if this raises, reset emailed_at (or record a failure) and retry
```

Two notes on that design:
- `emailed_at` would be one extra nullable field on OverdueNotice. The "claim then send" order gives at-most-once email. If a missed email is worse than a duplicate, send first and mark afterwards (at-least-once). That's a product decision, and I'd write it down.
- Two concurrent runs can both pick the same candidate before either commits. `ignore_conflicts` keeps the notices unique, and the `emailed_at` claim keeps the emails unique. The `created` count can then over-report slightly. The Part A task (`assets/tasks.py`) counts precisely by comparing before and after per batch.

**4. What would have caught it**

- **An idempotency test:** run the task twice and assert one notice and one email per check-out (`tests/test_tasks.py`).
- **A partial-failure test:** make the insert raise on the Nth row, re-run, and assert that no row was emailed twice and every row got a notice.
- **Tests that exercise real serialization:** run against a real worker (`celery.contrib.testing.worker.start_worker`) instead of eager mode, or assert `json.dumps(args)` works for every `.delay()` in tests.
- **A query-count assertion** on the task with a few dozen rows.
- **Monitoring:** alert when notices created per day exceeds the number of overdue check-outs. That is the fingerprint of duplicate notices.

---

## Part C — Optimise the slow PostgreSQL query

### 1. Rewrite

```sql
SELECT c.id, c.asset_id, c.employee_id, c.checked_out_at, c.due_at      -- (a)
FROM checkouts c
WHERE c.returned_at IS NULL
  AND c.checked_out_at >= TIMESTAMPTZ '2026-01-01 00:00:00+05:30'        -- (b)
  AND c.checked_out_at <  TIMESTAMPTZ '2026-07-01 00:00:00+05:30'
  AND EXISTS (SELECT 1 FROM employees e                                   -- (c)
              WHERE e.id = c.employee_id AND e.is_active)
ORDER BY c.due_at, c.id                                                   -- (d)
LIMIT 100;                                                                -- (e)
-- next page: AND (c.due_at, c.id) > (:last_due_at, :last_id)
```

**(a) Name the columns instead of `SELECT *`.** `*` drags `condition_note` along, and that free-text column can be TOASTed. Fetching it costs extra I/O and network for data the screen may not show. Named columns also make an index-only scan possible (see the INCLUDE index below).
- *Cost:* if the screen really shows the note, add it back. Correctness doesn't depend on this.

**(b) Replace `DATE(c.checked_out_at) BETWEEN ...` with a half-open range on the raw column.** This is the main fix.
- `DATE(col)` wraps the column in a function, so **no btree index on `checked_out_at` can be used** (the predicate isn't sargable). Postgres must compute `DATE()` for every candidate row.
- There is also a correctness problem: `DATE()` of a `timestamptz` depends on the session's `TimeZone` setting. The same query returns different rows for a session in UTC and one in IST.
- `>= '2026-01-01' AND < '2026-07-01'` is exactly equivalent to `BETWEEN '2026-01-01' AND '2026-06-30'` on dates, including the whole of 30 June. It can use an index, and it pins the time zone explicitly.
- I've assumed the business day is IST (`+05:30`). That assumption needs confirming.
- *Cost:* the application has to pass timestamps rather than dates. That's trivial.

**(c) Use `EXISTS` instead of `IN (SELECT ...)`.** On PG15 both become the same *hash semi-join*, so this is mainly a readability and NULL-safety habit, not a speed-up. I'd say that plainly rather than claim a win.
- A plain `JOIN` would also be safe here (`employees.id` is unique, so it can't multiply rows).
- *Cost:* none.

**(d) Add `id` to the ORDER BY.** Ordering by `due_at` alone is non-deterministic on ties, which breaks pagination: rows can repeat or vanish between pages. `(due_at, id)` is a total order and makes keyset pagination possible.

**(e) Add `LIMIT` plus keyset pagination.** The screen can't usefully display an unbounded set. With an index in `due_at` order, `LIMIT` lets Postgres **stop after 100 rows instead of sorting everything**.
- Keyset (`(due_at, id) > (...)`) stays fast on page 500, whereas `OFFSET` gets slower with every page.
- *Cost:* no "jump to page N" and no exact total without a separate `COUNT`. If the screen needs a total, show an estimate or cache it.

### 2. Indexes

```sql
-- Primary: partial, in the ORDER BY order, covering the filter column.
CREATE INDEX CONCURRENTLY checkouts_open_due_idx
    ON checkouts (due_at, id)
    INCLUDE (checked_out_at, employee_id, asset_id)
    WHERE returned_at IS NULL;
```

Why each part earns its place:
- **Partial (`WHERE returned_at IS NULL`).** Most of the 4.2M rows are returned, and the query only wants open ones.
  - The partial index holds only open rows, so it's a small fraction of the size. It stays hot in cache and is cheap to maintain.
  - Returned rows cost it nothing, apart from the one update that removes a row from it on return.
  - A full composite index on `(returned_at, …)` would index millions of rows the query never touches.
  - *Trade-off:* it only helps queries that repeat the `returned_at IS NULL` predicate. That's fine: every "open items" screen does.
- **Leading `(due_at, id)`.** It matches the ORDER BY, so Postgres reads rows already sorted: no Sort node, and with `LIMIT` it stops early.
- **`INCLUDE (checked_out_at, employee_id, asset_id)`.** The `checked_out_at` range and the employee semi-join are checked from the index entry, without visiting the heap for rows that will be filtered out. With the named-column SELECT this can become an **index-only scan**, provided the visibility map is kept current by vacuum.

**The alternative I would weigh:** `ON checkouts (checked_out_at) WHERE returned_at IS NULL`. This one is better if the date range is **very selective** among open rows (a few hundred matches), because it then fetches only those rows and sorts a small set. The `due_at`-leading index is better when the range matches most open rows and the screen pages with `LIMIT`. Which one wins depends on the distribution (see question 5), so I'd check both with `EXPLAIN` on production-like data before choosing, rather than keep both "just in case": each extra index is write cost on an 8k-rows-per-day table.

**What I would *not* add:**
- **An index on `employees(is_active)`.** 12,000 rows is a few hundred pages. Postgres hashes it in memory in about a millisecond, and a boolean with low selectivity is a poor index key anyway.
- **An index on `DATE(checked_out_at)` (an expression index).** It would "fix" the original query, but it keeps the time-zone ambiguity. The rewrite is the better fix.

Use `CONCURRENTLY` so the build doesn't block writes on a live table. It can't run inside a transaction; in Django use `AddIndexConcurrently` with `atomic = False`.

### 3. EXPLAIN (ANALYZE, BUFFERS): before and after

**Before (expected):**
```
Sort  (actual time=7900..7990 rows=N)                   Sort Method: external merge  Disk: ...kB   (if N large)
  -> Hash Semi Join
       Hash Cond: (c.employee_id = employees.id)
       -> Seq Scan on checkouts c  (actual rows=... loops=1)
            Filter: ((returned_at IS NULL) AND (date(checked_out_at) >= '2026-01-01') AND ...)
            Rows Removed by Filter: ~4,1xx,xxx
            Buffers: shared hit=... read=<tens of thousands>
       -> Hash -> Seq Scan on employees  Filter: is_active
Execution Time: ~8000 ms
```
The signature of the problem is **`Seq Scan on checkouts`** with **`Rows Removed by Filter` in the millions**, and a large `Buffers: ... read=` count (the whole table read from disk or the OS cache). If the result set is large there's also a Sort that spills to disk.

**After (expected):**
```
Limit  (actual time=0.1..2 rows=100)
  -> Nested Loop Semi Join  (or Hash Semi Join)
       -> Index Scan (or Index Only Scan) using checkouts_open_due_idx on checkouts c
            Filter: (checked_out_at >= ... AND checked_out_at < ...)
            Rows Removed by Filter: <small>
            Buffers: shared hit=<hundreds>
       -> Index Scan using employees_pkey on employees e  Filter: is_active
Execution Time: a few ms
```
**The line that proves it worked:** `Index Scan using checkouts_open_due_idx on checkouts`, replacing `Seq Scan on checkouts`, with no Sort node above it and `Buffers` falling from tens of thousands of pages to hundreds.

I'd also compare **estimated vs actual rows** on that node. A large mismatch means the planner is guessing, and the plan may flip back as data changes.

### 4. Growth (8,000 rows/day ≈ 2.9M/year): what breaks first

1. **Autovacuum and statistics fall behind first (probably), not the index.** Every check-out is updated once (on return), and each update leaves a dead row. With the default `autovacuum_vacuum_scale_factor = 0.2`, vacuum waits for ~840k dead rows at today's size, ~1.4M next year.
   - Meanwhile, bloat grows and the visibility map goes stale, so index-only scans turn into heap fetches.
   - `autoanalyze` (default 10%) triggers so rarely that **the newest dates fall outside the column histogram**, and the planner underestimates recent ranges. That's the classic way a plan suddenly flips.
   - **Fix:** per-table settings, e.g. `ALTER TABLE checkouts SET (autovacuum_vacuum_scale_factor = 0.01, autovacuum_analyze_scale_factor = 0.005)`, then monitor `n_dead_tup`, `last_autovacuum` and `last_autoanalyze`.
2. **Reports over history get slower.** Any query over returned rows (history, audits, date ranges without `returned_at IS NULL`) scans an ever-growing heap.
   - Before that hurts: range-**partition** `checkouts` by `checked_out_at` (monthly or quarterly). Partition pruning then keeps date-range queries on a few partitions, and old partitions can be detached or archived cheaply.
   - Partitioning needs the primary key to include the partition key. That's a real migration, so plan it early rather than in an incident.
3. **The open set itself grows** if items are never returned. The partial index grows with it. That's a data-hygiene alert: count of open rows older than N days.
4. **Offset pagination and `COUNT(*)` on the screen** degrade linearly. Keyset pagination (above) avoids that.

### 5. What I'd measure first

**The actual selectivity of each predicate on the real data:**
```sql
SELECT count(*) FILTER (WHERE returned_at IS NULL) AS open_rows,
       count(*) FILTER (WHERE returned_at IS NULL
                        AND checked_out_at >= '2026-01-01' AND checked_out_at < '2026-07-01') AS open_in_range
FROM checkouts;
```

Everything above assumes open rows are a small fraction of 4.2M. If half the table is open (items never marked returned), the partial index is huge and a different design is needed. The choice between a `due_at`-leading and a `checked_out_at`-leading index depends entirely on `open_in_range / open_rows`. I can't know either number from the schema.

I'd also want `EXPLAIN (ANALYZE, BUFFERS)` of the current plan: whether the 8 seconds is disk reads (`read=`) or CPU on cached pages (`hit=`) changes how much an index will save.

---

## Part D — Production reasoning

### D1. Zero-downtime migration: non-nullable `location_id` FK on 4.2M rows

**Three deploys (expand → backfill → contract).** Each one is safe with the previous code still running on some of the four instances.

**Deploy 1: expand, and start writing.**
- **Migration:** `ADD COLUMN location_id bigint NULL`. Nullable with no default is a catalog-only change, so it's instant. It still needs a brief `ACCESS EXCLUSIVE` lock, so set `lock_timeout = '3s'` and retry, so it can't queue behind a long transaction and stall every query behind it.
- Add the FK as `NOT VALID` (a raw-SQL migration via `SeparateDatabaseAndState`), so it doesn't scan the table.
- Build the index with `CREATE INDEX CONCURRENTLY` (`AddIndexConcurrently`, `atomic = False`).
- **Code:** the model has `null=True`, and every code path that creates a check-out now sets `location_id`.
- **In-flight old code:** the old instances name their columns explicitly in INSERT and SELECT, so they never mention `location_id`. Their inserts get NULL, which is allowed. Nothing breaks during the rolling restart.

**Between deploys: backfill.**
- A management command (not a migration) updates rows `WHERE location_id IS NULL` in id-ranged batches of about 5–10k, each in its own short transaction, pausing between batches and watching replica lag.
- It's idempotent and resumable.
- Then `ALTER TABLE checkouts VALIDATE CONSTRAINT checkouts_location_fk`. This scans the table but only takes `SHARE UPDATE EXCLUSIVE`, so reads and writes carry on.

**Deploy 2: contract.** Only after *all* instances run Deploy-1 code (otherwise an old instance would insert NULL):
- `ADD CONSTRAINT location_not_null CHECK (location_id IS NOT NULL) NOT VALID`
- `VALIDATE CONSTRAINT location_not_null` (non-blocking scan)
- `ALTER COLUMN location_id SET NOT NULL`. Postgres 12+ sees the validated CHECK and **skips the table scan**, so the `ACCESS EXCLUSIVE` lock lasts milliseconds.
- Drop the redundant CHECK.
- The model becomes `null=False`.

That's two code deploys plus a backfill job. Some teams would count three if the backfill ships as its own release.

**What locks the table if you get it wrong:**
- `ALTER COLUMN ... SET NOT NULL` without the validated CHECK scans all 4.2M rows **while holding `ACCESS EXCLUSIVE`**, blocking every read and write.
- `ADD CONSTRAINT ... FOREIGN KEY` without `NOT VALID` does the same kind of full validation scan under a lock that blocks writes.
- Django's default `AddField(null=False)` path hits the `SET NOT NULL` case.
- Any of these can also queue behind a long-running transaction and freeze traffic even before it starts. That's what `lock_timeout` prevents.

### D2. Latency triage: overdue report went from fine to 25 seconds, no deploy in 9 days

**The order I'd check in, and what each check rules in or out:**

1. **Scope** (APM traces or logs). Is it only this endpoint, and every request or just some? Is the time spent in the DB span, in Python, or waiting for a DB connection?
   - DB-span time → go to step 3.
   - Pool wait → connection exhaustion; look for something else holding connections.
   - Python time → serialization of a huge page, or N+1.
2. **What changed without a deploy?** Data volume, a new cron or reporting job, DB configuration, a failover to a replica with a cold cache, instance or storage changes (cloud **I/O burst credits** running out produce exactly this "fine for months, slow this morning" shape).
3. **The query now:** `EXPLAIN (ANALYZE, BUFFERS)` of the report's query, compared with the plan it used to have.
   - A different plan (Seq Scan instead of the partial index, or a nested loop with a huge row misestimate) → a planner problem, go to step 4.
   - The same plan but far more rows → a data problem.
4. **Statistics and bloat:** `pg_stat_user_tables` for `last_autoanalyze`, `last_autovacuum`, `n_dead_tup` and `n_live_tup` on `checkouts`.
5. **Locks and contention:** `pg_stat_activity` (`wait_event_type = 'Lock'`, `idle in transaction`, long-running queries), `pg_locks`, plus DB CPU and IOPS.
6. **Data shape:** how many overdue rows exist now versus last week. A bulk import, or a team that stopped marking returns, grows the overdue set. Pagination's `COUNT(*)` scales with it even though each page is 20 rows.

**The two most likely causes, given that no code changed:**

**(1) A plan flip from stale statistics or data growth crossing a threshold.**
- *Confirm:* the estimated vs actual row counts in `EXPLAIN ANALYZE` differ by orders of magnitude, and `last_autoanalyze` is old.
- Run `ANALYZE checkouts` and re-run `EXPLAIN`. If the fast plan comes back, that's confirmed. Then fix it permanently with per-table analyze and vacuum thresholds.

**(2) External contention on the database.**
- This means a long-running or idle-in-transaction session holding locks, a new heavy job sharing the instance, autovacuum grinding a bloated table, or I/O throttling.
- *Confirm:* `pg_stat_activity` shows the report waiting on `Lock` or `IO` wait events, and the cloud metrics show exhausted burst balance or saturated IOPS at the same time the latency started.
- The report's `EXPLAIN` alone looks normal when run in isolation.

I'd measure these before choosing a fix, rather than add an index on a hunch.

### D3. CI/CD and safety on GitHub Actions

**On every pull request** (all required to merge; see `.github/workflows/ci.yml`):
- lint (ruff)
- `python manage.py makemigrations --check --dry-run`, which fails if a model change has no migration
- the test suite against **real Postgres 16 and Redis service containers** (the same versions as production; the concurrency tests can't run on SQLite)
- a **migration safety lint** over the `sqlmigrate` output (e.g. django-migration-linter or squawk), which flags `SET NOT NULL`, non-concurrent index builds, column renames and drops
- `docker build`
- `pip-audit` for vulnerable dependencies, and secret scanning

**On merge to `main`:**
- Build the image once, tagged with the commit SHA, and push it to the registry. The **same artifact** is promoted everywhere; it's never rebuilt per environment.
- Deploy automatically to **staging**: run the migrations there as a one-off job, then roll out, then run smoke tests (`/health/`, one check-out → return round trip, the overdue report).

**Production gate:** a GitHub *Environment* with required reviewers. Only a SHA that passed staging can be promoted, and ideally outside peak hours.

**Migrations relative to code:**
- Migrations run as a **single one-off job before** the new code rolls out. They never run on container start, because four replicas would race to apply them.
- That means every migration must be compatible with the **currently running (N-1) code**: the expand/contract discipline from D1. Additive changes ship first. Anything destructive (a drop, a rename, `NOT NULL`) ships in a later release, after no running code depends on the old shape.

**Rollback story when the schema has already moved:**
- **Roll back the code, not the schema.** Redeploy the previous image SHA. That's safe *because* the migration was additive and N-1 compatible, so the old code runs fine on the new schema.
- Reverse migrations are never run automatically in production. Reversing a data or destructive migration can lose data, and some can't be reversed at all.
- If the migration itself is wrong, **fix forward** with a new migration.
- Risky behaviour ships behind a **feature flag**, so "rollback" is often just a flag flip.
- Before any contract (destructive) migration, confirm a recent backup and point-in-time recovery.
- The deploy uses health-check-gated rolling updates, so a failing health check on new instances halts the rollout automatically.
