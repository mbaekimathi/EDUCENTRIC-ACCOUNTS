from pathlib import Path

from django.conf import settings
from django.core.cache import cache

from .models import SchoolProfile

DEFAULT_BRAND = {
    "school_name": "Educentric Accounts",
    "school_display": "Accounts",
    "primary_color": "#1f5cf0",
    "school_accent": "#1f5cf0",
    "motto": "",
    "logo_url": "",
    "brand_initials": "EC",
    "has_logo": False,
}


def _normalize_color(color, fallback="#1f5cf0"):
    value = (color or "").strip()
    if len(value) == 6 and all(ch in "0123456789abcdefABCDEF" for ch in value):
        value = f"#{value}"
    if len(value) == 7 and value.startswith("#") and all(
        ch in "0123456789abcdefABCDEF" for ch in value[1:]
    ):
        return value.lower()
    return fallback


def _brand_initials(name):
    source = (name or "").strip()
    parts = [part for part in source.replace("-", " ").split() if part]
    if len(parts) >= 2:
        return "".join(part[0] for part in parts[:2]).upper()
    return source[:2].upper() if source else "EC"


def _logo_url(logo_name):
    name = (logo_name or "").strip().lstrip("/")
    if not name:
        return ""
    path = Path(settings.MEDIA_ROOT) / name
    if not path.is_file():
        return ""
    return f"{settings.MEDIA_URL.rstrip('/')}/{name}"


def school_branding(request):
    brand = cache.get("accounts_school_branding_v2")
    if brand is None:
        profile = SchoolProfile.objects.only(
            "official_name",
            "display_name",
            "primary_color",
            "motto",
            "school_logo",
        ).first()
        if profile:
            display = profile.display_name or profile.official_name or DEFAULT_BRAND["school_display"]
            accent = _normalize_color(profile.primary_color, DEFAULT_BRAND["primary_color"])
            logo_url = _logo_url(profile.school_logo)
            brand = {
                "school_name": profile.official_name or DEFAULT_BRAND["school_name"],
                "school_display": display,
                "primary_color": accent,
                "school_accent": accent,
                "motto": profile.motto or "",
                "logo_url": logo_url,
                "brand_initials": _brand_initials(display),
                "has_logo": bool(logo_url),
            }
        else:
            brand = DEFAULT_BRAND.copy()
        cache.set("accounts_school_branding_v2", brand, 300)
    return {"brand": brand}
