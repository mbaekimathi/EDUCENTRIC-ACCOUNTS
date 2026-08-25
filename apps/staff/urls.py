from django.urls import path

from . import views

app_name = "staff"

urlpatterns = [
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
]
