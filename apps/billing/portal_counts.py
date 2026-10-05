"""Cached aggregate counts for hot portal pages (60s TTL)."""

from django.core.cache import cache

_CACHE_KEY = "accounts_portal_counts_v1"
_CACHE_TTL = 60


def get_portal_counts():
    data = cache.get(_CACHE_KEY)
    if data is not None:
        return data

    from apps.directory.models import Student

    from .models import FeeCategory, FeeCharge, Payment

    data = {
        "learner_count": Student.objects.filter(is_suspended=False).count(),
        "active_category_count": FeeCategory.objects.filter(is_active=True).count(),
        "invoice_charge_count": FeeCharge.objects.count(),
        "invoice_payment_count": Payment.objects.count(),
    }
    cache.set(_CACHE_KEY, data, _CACHE_TTL)
    return data
