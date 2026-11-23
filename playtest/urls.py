"""Playtest routes: sessions, and each session's matches under it."""

from django.urls import path

from . import views

urlpatterns = [
    path("playtests", views.playtest_list, name="playtest_list"),
    path("playtests/new", views.new_playtest, name="new_playtest"),
    path("playtests/new/opponents", views.new_playtest_opponents, name="new_playtest_opponents"),
    path("playtests/create", views.create_playtest, name="create_playtest"),
    path("playtests/<uuid:playtest_id>", views.playtest_detail, name="playtest_detail"),
    path("playtests/<uuid:playtest_id>/live", views.playtest_live, name="playtest_live"),
    path("playtests/<uuid:playtest_id>/stop", views.stop_playtest, name="stop_playtest"),
    path("playtests/<uuid:playtest_id>/delete", views.delete_playtest, name="delete_playtest"),
    path("playtests/<uuid:playtest_id>/matches/<uuid:match_id>", views.match_detail, name="match_detail"),
    path("playtests/<uuid:playtest_id>/matches/<uuid:match_id>/live", views.match_live, name="match_live"),
    path("playtests/<uuid:playtest_id>/matches/<uuid:match_id>/stop", views.stop_match, name="stop_match"),
    path("playtests/<uuid:playtest_id>/matches/<uuid:match_id>/delete", views.delete_match, name="delete_match"),
]
