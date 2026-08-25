from django.conf import settings
from django.conf.urls.static import static
from django.contrib.auth import logout
from django.shortcuts import redirect
from django.urls import include, path


def home(request):
    """Send signed-in staff to the accounts dashboard; everyone else to login."""
    if request.user.is_authenticated and getattr(request.user, "can_access_portal", False):
        return redirect("billing:dashboard")
    if request.user.is_authenticated:
        logout(request)
    return redirect("staff:login")


urlpatterns = [
    path("", home, name="home"),
    path("auth/", include("apps.staff.urls")),
    path("accounts-dashboard/", include("apps.billing.urls")),
]

if settings.DEBUG or getattr(settings, "SERVE_MEDIA", False):
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
