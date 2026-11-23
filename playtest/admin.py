"""Django admin registrations."""

from django.contrib import admin

from .models import Match, MatchEvent, Playtest, Seat


class SeatInline(admin.TabularInline):
    model = Seat
    extra = 0


@admin.register(Match)
class MatchAdmin(admin.ModelAdmin):
    list_display = ("id", "owner", "format", "status", "turn", "end_reason", "forge_game_id", "created_at")
    list_filter = ("status", "format", "end_reason")
    readonly_fields = ("created_at", "finished_at")
    inlines = [SeatInline]


@admin.register(MatchEvent)
class MatchEventAdmin(admin.ModelAdmin):
    list_display = ("match", "seq", "turn", "seat_position", "kind", "text")
    list_filter = ("kind",)
    search_fields = ("text",)


@admin.register(Playtest)
class PlaytestAdmin(admin.ModelAdmin):
    list_display = ("id", "owner", "format", "deck_name", "seed", "created_at")
    list_filter = ("format",)
    readonly_fields = ("created_at",)
