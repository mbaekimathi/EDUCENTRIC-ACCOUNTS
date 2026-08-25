from django.core.cache import cache

from .models import SchoolProfile

DEFAULT_BRAND = {
    "school_name": "Educentric Accounts",
    "school_display": "Accounts",
    "primary_color": "#0f6b4c",
    "motto": "",
}


def school_branding(request):
    brand = cache.get("accounts_school_branding")
    if brand is None:
        profile = SchoolProfile.objects.only(
            "official_name", "display_name", "primary_color", "motto"
        ).first()
        if profile:
            brand = {
                "school_name": profile.official_name or DEFAULT_BRAND["school_name"],
                "school_display": profile.display_name or profile.official_name,
                # Finance UI keeps a stable teal; school red remains available as accent.
                "primary_color": "#0f6b4c",
                "school_accent": profile.primary_color or "#ef1f1f",
                "motto": profile.motto or "",
            }
        else:
            brand = {**DEFAULT_BRAND, "school_accent": "#ef1f1f"}
        cache.set("accounts_school_branding", brand, 300)
    return {"brand": brand}
