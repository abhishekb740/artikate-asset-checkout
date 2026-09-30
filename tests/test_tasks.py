from datetime import timedelta

import pytest
from django.utils import timezone

from assets.models import OverdueNotice
from assets.tasks import flag_overdue_checkouts


@pytest.mark.django_db
def test_task_is_idempotent_within_a_day(make_asset, make_employee, make_checkout):
    now = timezone.now()
    emp = make_employee()
    five_days_ago = now - timedelta(days=5)
    overdue_1 = make_checkout(make_asset(), emp, checked_out_at=five_days_ago, due_at=now - timedelta(days=1))
    overdue_2 = make_checkout(make_asset(), emp, checked_out_at=five_days_ago, due_at=now - timedelta(hours=1))
    make_checkout(make_asset(), emp, checked_out_at=now, due_at=now + timedelta(days=2))            # not overdue
    make_checkout(make_asset(), emp, checked_out_at=now - timedelta(days=9),
                  due_at=now - timedelta(days=4), returned_at=now - timedelta(days=3))              # returned

    assert flag_overdue_checkouts() == 2          # called directly, no worker needed
    assert flag_overdue_checkouts() == 0          # second run creates nothing
    for _ in range(3):
        flag_overdue_checkouts()

    today = timezone.localdate()
    assert OverdueNotice.objects.count() == 2
    assert set(OverdueNotice.objects.values_list("checkout_id", flat=True)) == {overdue_1.id, overdue_2.id}
    assert set(OverdueNotice.objects.values_list("notice_date", flat=True)) == {today}


@pytest.mark.django_db
def test_task_adds_a_new_notice_on_a_new_day(make_asset, make_employee, make_checkout):
    now = timezone.now()
    co = make_checkout(make_asset(), make_employee(), checked_out_at=now - timedelta(days=5),
                       due_at=now - timedelta(days=2))
    OverdueNotice.objects.create(checkout=co, notice_date=timezone.localdate() - timedelta(days=1))
    assert flag_overdue_checkouts() == 1
    assert co.notices.count() == 2


@pytest.mark.django_db
def test_task_runs_through_celery_eagerly(settings, make_asset, make_employee, make_checkout):
    """The registered Celery task (not just the function) works end to end."""
    settings.CELERY_TASK_ALWAYS_EAGER = True
    from config.celery import app
    app.conf.task_always_eager = True
    try:
        now = timezone.now()
        make_checkout(make_asset(), make_employee(), checked_out_at=now - timedelta(days=3),
                      due_at=now - timedelta(days=1))
        result = flag_overdue_checkouts.delay()
        assert result.get(timeout=5) == 1
    finally:
        app.conf.task_always_eager = False
