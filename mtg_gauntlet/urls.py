"""URL routing for the MTG Gauntlet web service."""

from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from . import views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("login", auth_views.LoginView.as_view(), name="login"),
    path("logout", auth_views.LogoutView.as_view(), name="logout"),
    path("", views.home, name="home"),
    path("home/running", views.home_running, name="home_running"),
    path("engine/forge", views.forge_status, name="forge_status"),
    path("engine/matches", views.matches_status, name="matches_status"),
    path("", include("decks.urls")),
    path("", include("playtest.urls")),
]
