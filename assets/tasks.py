import logging

from celery import shared_task
from django.utils import timezone

from .models import CheckOut, OverdueNotice
from .selectors import overdue_q

logger = logging.getLogger(__name__)

BATCH_SIZE = 1000


@shared_task(name="assets.tasks.flag_overdue_checkouts")
def flag_overdue_checkouts() -> int:
    """Create today's OverdueNotice for every open, overdue check-out.

    Idempotent by construction, not by convention:
      * the candidate set excludes check-outs that already have a notice today, and
      * the INSERT uses ON CONFLICT DO NOTHING against the
        (checkout, notice_date) unique constraint,
    so running it five times a day, or two workers running it at once, still
    leaves exactly one notice per check-out per day.

    Streams ids in batches so a large overdue set is never loaded into memory
    at once. Returns the number of notices newly created.
    """
    now = timezone.now()
    today = timezone.localdate(now)

    candidate_ids = (
        CheckOut.objects.filter(overdue_q(now))
        .exclude(notices__notice_date=today)
        .values_list("id", flat=True)
        .order_by("id")
    )

    created = 0
    batch: list[int] = []
    for checkout_id in candidate_ids.iterator(chunk_size=BATCH_SIZE):
        batch.append(checkout_id)
        if len(batch) >= BATCH_SIZE:
            created += _insert_notices(batch, today)
            batch = []
    if batch:
        created += _insert_notices(batch, today)

    logger.info("flag_overdue_checkouts: created %d notice(s) for %s", created, today)
    return created


def _insert_notices(checkout_ids: list[int], today) -> int:
    before = OverdueNotice.objects.filter(checkout_id__in=checkout_ids, notice_date=today).count()
    OverdueNotice.objects.bulk_create(
        [OverdueNotice(checkout_id=cid, notice_date=today) for cid in checkout_ids],
        ignore_conflicts=True,
    )
    after = OverdueNotice.objects.filter(checkout_id__in=checkout_ids, notice_date=today).count()
    return after - before
