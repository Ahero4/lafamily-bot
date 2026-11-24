"""Снимки показателей Discord-профилей для семейного портала."""

from lafamily.modules.events.time import week_range


def profile_snapshots(store, guild_id):
    period = week_range()
    return [
        {
            "discordId": row["user_id"],
            "total": row["total"],
            "week": row["week"],
            "events": row["events"],
            "wins": row["wins"],
            "mvps": row["mvps"],
            "weekEvents": row["week_events"],
            "weekWins": row["week_wins"],
            "weekMvps": row["week_mvps"],
            "weekStart": period[0],
        }
        for row in store.participant_profiles(str(guild_id), period)
    ]
