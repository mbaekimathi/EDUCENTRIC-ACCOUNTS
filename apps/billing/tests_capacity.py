from datetime import date, timedelta

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase

from apps.billing.models import Payment
from apps.billing.reports import (
    REPORT_MAX_PERIOD_DAYS,
    _limited_list,
    validate_report_period,
)
from apps.billing.views import (
    _FEE_APPLY_LOCK_PREFIX,
    _FEE_APPLY_LOCK_TTL,
    _FEE_APPLY_MAX_CELLS,
    _REPORT_LOCK_PREFIX,
    _REPORT_LOCK_TTL,
)


class ReportPeriodTests(SimpleTestCase):
    def test_rejects_overlong_custom_range(self):
        start = date(2024, 1, 1)
        end = start + timedelta(days=REPORT_MAX_PERIOD_DAYS + 1)
        message = validate_report_period(start, end)
        self.assertIsNotNone(message)
        self.assertIn(str(REPORT_MAX_PERIOD_DAYS), message)

    def test_accepts_year_range(self):
        start = date(2024, 1, 1)
        end = date(2024, 12, 31)
        self.assertIsNone(validate_report_period(start, end))


class ReportLockTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_per_user_report_lock_is_independent(self):
        lock_a = f"{_REPORT_LOCK_PREFIX}1"
        lock_b = f"{_REPORT_LOCK_PREFIX}2"
        self.assertTrue(cache.add(lock_a, "1", _REPORT_LOCK_TTL))
        self.assertTrue(cache.add(lock_b, "1", _REPORT_LOCK_TTL))
        self.assertFalse(cache.add(lock_a, "1", _REPORT_LOCK_TTL))


class FeeApplyGuardTests(SimpleTestCase):
    def test_cell_cap_constant_is_reasonable(self):
        self.assertGreater(_FEE_APPLY_MAX_CELLS, 0)
        self.assertLessEqual(_FEE_APPLY_MAX_CELLS, 10000)

    def test_lock_prefix_is_scoped(self):
        self.assertTrue(_FEE_APPLY_LOCK_PREFIX.endswith(":"))


class LimitedListTests(TestCase):
    def test_limited_list_empty_queryset(self):
        items, truncated = _limited_list(Payment.objects.none(), 5)
        self.assertEqual(items, [])
        self.assertFalse(truncated)
