"""discomp3play — a Discord bot that queues and streams local audio files.

Run with: python bot.py  (after setting DISCORD_TOKEN; see README.md)
"""

from __future__ import annotations

import asyncio

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from config import load_config
from library import Library
from pagination import send_paginated
from player import GuildPlayer

load_dotenv()
config = load_config()
library = Library(config.music_dir)

intents = discord.Intents.default()  # voice_states is included; no privileged intents needed
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
bot.players: dict[int, GuildPlayer] = {}  # type: ignore[attr-defined]


# -- helpers ----------------------------------------------------------------


def get_player(guild: discord.Guild) -> GuildPlayer:
    player = bot.players.get(guild.id)
    if player is None or player._destroyed:
        player = GuildPlayer(bot, guild, idle_timeout=config.idle_timeout)
        bot.players[guild.id] = player
    return player


async def ensure_voice(interaction: discord.Interaction) -> tuple[discord.VoiceClient | None, str | None]:
    """Connect (or move) the bot to the caller's voice channel."""
    user = interaction.user
    if not isinstance(user, discord.Member) or user.voice is None or user.voice.channel is None:
        return None, "You need to be in a voice channel first."
    channel = user.voice.channel
    me = interaction.guild.me  # type: ignore[union-attr]
    perms = channel.permissions_for(me)
    if not perms.connect:
        return None, f"I don't have permission to **Connect** to {channel.mention}."
    if not perms.speak:
        return None, f"I don't have permission to **Speak** in {channel.mention}."
    vc = interaction.guild.voice_client  # type: ignore[union-attr]
    if vc is None:
        try:
            vc = await channel.connect(timeout=20.0)
        except TimeoutError:
            return None, (
                "Voice connection timed out. Check that I have **Connect** permission on the channel, "
                "and that I'm not stuck in another voice channel from a previous session."
            )
        except discord.ClientException:
            vc = interaction.guild.voice_client  # type: ignore[union-attr]
    elif vc.channel != channel:
        try:
            await vc.move_to(channel)
        except TimeoutError:
            return None, "Voice connection timed out while moving channels."
    return vc, None  # type: ignore[return-value]


# Autocomplete callbacks must return within Discord's 3s interaction deadline,
# so they only read from the in-memory cache — never trigger a filesystem scan.
# The cache is pre-warmed in on_ready and refreshed by a background task.
async def track_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    matches = library.search(current, refresh=False)[:25]
    return [app_commands.Choice(name=t.name[:100], value=t.name[:100]) for t in matches]


async def album_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    q = current.lower().strip()
    names = [n for n in library.albums(refresh=False) if not q or q in n.lower()][:25]
    return [app_commands.Choice(name=n[:100], value=n[:100]) for n in names]


async def artist_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    q = current.lower().strip()
    names = [n for n in library.artists(refresh=False) if not q or q in n.lower()][:25]
    return [app_commands.Choice(name=n[:100], value=n[:100]) for n in names]


# -- events -----------------------------------------------------------------


async def _library_refresh_loop(interval: float = 60.0) -> None:
    """Re-scan the library off the event loop on a slow timer.

    Keeps autocomplete responsive when files are added/removed on disk without
    blocking the event loop on every keystroke.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            await library.refresh()
        except Exception as exc:  # noqa: BLE001
            print(f"[library] background refresh failed: {exc}")


@bot.event
async def on_ready() -> None:
    # Pre-warm the cache off the event loop so the first autocomplete call
    # doesn't have to do a cold scan (which would blow Discord's 3s deadline).
    try:
        tracks = await library.refresh()
        print(f"[library] indexed {len(tracks)} track(s) from {library.root}")
    except Exception as exc:  # noqa: BLE001
        print(f"[library] initial scan failed: {exc}")

    if not hasattr(bot, "_library_task"):
        bot._library_task = bot.loop.create_task(_library_refresh_loop())  # type: ignore[attr-defined]

    try:
        if config.guild_ids:
            total = 0
            for gid in config.guild_ids:
                guild = discord.Object(id=gid)
                bot.tree.copy_global_to(guild=guild)
                synced = await bot.tree.sync(guild=guild)
                total += len(synced)
                print(f"[bot] synced {len(synced)} command(s) to guild {gid}")
            print(f"[bot] synced {total} command(s) across {len(config.guild_ids)} guild(s)")
        else:
            synced = await bot.tree.sync()
            print(f"[bot] synced {len(synced)} slash command(s) globally")
    except Exception as exc:  # noqa: BLE001
        print(f"[bot] command sync failed: {exc}")
    print(f"[bot] logged in as {bot.user} (id: {bot.user.id})")
    print(f"[bot] music library: {library.root}")


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    # Bot itself was disconnected -> tear down its player.
    if member.id == bot.user.id:
        if before.channel is not None and after.channel is None:
            player = bot.players.get(member.guild.id)
            if player:
                await player.destroy(reason="disconnected")
        return

    # A human left; if the bot is now alone, leave too.
    vc = member.guild.voice_client
    if vc and vc.channel and not any(not m.bot for m in vc.channel.members):
        player = bot.players.get(member.guild.id)
        if player:
            await player.destroy(reason="alone")


# -- commands ---------------------------------------------------------------


@bot.tree.command(name="play", description="Queue a track from the library (and start playing)")
@app_commands.describe(query="Track name or number — see /list")
@app_commands.autocomplete(query=track_autocomplete)
async def play(interaction: discord.Interaction, query: str) -> None:
    track = library.resolve(query)
    if track is None:
        await interaction.response.send_message(f"❌ No track matching `{query}`.", ephemeral=True)
        return
    # Voice connect can exceed Discord's 3s interaction deadline; defer first.
    await interaction.response.defer()
    vc, err = await ensure_voice(interaction)
    if err:
        await interaction.followup.send(f"❌ {err}", ephemeral=True)
        return
    player = get_player(interaction.guild)
    player.text_channel = interaction.channel
    position = player.add(track)
    await interaction.followup.send(f"➕ Queued **{track.name}** (position {position}).")


async def _queue_many(
    interaction: discord.Interaction,
    tracks: list,
    label: str,
    empty_msg: str,
) -> None:
    """Shared body for /playalbum and /playartist: queue a batch of tracks."""
    if not tracks:
        await interaction.followup.send(empty_msg, ephemeral=True)
        return
    vc, err = await ensure_voice(interaction)
    if err:
        await interaction.followup.send(f"❌ {err}", ephemeral=True)
        return
    player = get_player(interaction.guild)
    player.text_channel = interaction.channel
    starting_position = len(player.queue) + 1
    for t in tracks:
        player.add(t)
    await interaction.followup.send(
        f"➕ Queued **{len(tracks)} track(s)** from {label} "
        f"(positions {starting_position}–{starting_position + len(tracks) - 1})."
    )


@bot.tree.command(name="playalbum", description="Queue every track from an album")
@app_commands.describe(album="Album name (autocomplete)")
@app_commands.autocomplete(album=album_autocomplete)
async def playalbum(interaction: discord.Interaction, album: str) -> None:
    await interaction.response.defer()
    tracks = library.tracks_by_album(album)
    await _queue_many(
        interaction,
        tracks,
        label=f"**{album}**",
        empty_msg=f"❌ No album matching `{album}`.",
    )


@bot.tree.command(name="playartist", description="Queue every track by an artist")
@app_commands.describe(artist="Artist name (autocomplete)")
@app_commands.autocomplete(artist=artist_autocomplete)
async def playartist(interaction: discord.Interaction, artist: str) -> None:
    await interaction.response.defer()
    tracks = library.tracks_by_artist(artist)
    await _queue_many(
        interaction,
        tracks,
        label=f"**{artist}**",
        empty_msg=f"❌ No artist matching `{artist}`.",
    )


list_group = app_commands.Group(name="list", description="Browse the music library")

EMPTY_LIBRARY_MSG = f"📂 No audio files found in `{config.music_dir}`."


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' if n != 1 else ''}"


@list_group.command(name="tracks", description="List every track in the library")
async def list_tracks(interaction: discord.Interaction) -> None:
    # Cold library scans (ID3 tag reads) can exceed Discord's 3s deadline.
    await interaction.response.defer(ephemeral=True)
    tracks = library.tracks(force=True)
    items = [f"`{i + 1:>3}.` {t.display}" for i, t in enumerate(tracks)]
    await send_paginated(
        interaction,
        title="🎵 Tracks",
        items=items,
        empty_message=EMPTY_LIBRARY_MSG,
        unit="tracks",
    )


@list_group.command(name="albums", description="List albums in the library")
async def list_albums(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    albums = library.albums()
    items = []
    for name, tracks in albums.items():
        artists = sorted({t.artist for t in tracks if t.artist})
        by = f" — by {', '.join(artists)}" if artists else ""
        items.append(f"**{name}**{by}  `({_plural(len(tracks), 'track')})`")
    await send_paginated(
        interaction,
        title="💿 Albums",
        items=items,
        empty_message=EMPTY_LIBRARY_MSG,
        unit="albums",
    )


@list_group.command(name="artists", description="List artists in the library")
async def list_artists(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    artists = library.artists()
    items = []
    for name, tracks in artists.items():
        albums = sorted({t.album for t in tracks if t.album})
        suffix = f", {_plural(len(albums), 'album')}" if albums else ""
        items.append(f"**{name}**  `({_plural(len(tracks), 'track')}{suffix})`")
    await send_paginated(
        interaction,
        title="🎤 Artists",
        items=items,
        empty_message=EMPTY_LIBRARY_MSG,
        unit="artists",
    )


bot.tree.add_command(list_group)


@bot.tree.command(name="queue", description="Show the current queue")
async def queue_cmd(interaction: discord.Interaction) -> None:
    await interaction.response.defer()
    player = bot.players.get(interaction.guild.id)
    if not player or (player.current is None and not player.queue):
        await interaction.followup.send("The queue is empty.", ephemeral=True)
        return
    items: list[str] = []
    if player.current:
        items.append(f"**Now playing:** {player.current.display}")
        if player.queue:
            items.append("**Up next:**")
    for i, t in enumerate(player.queue, 1):
        items.append(f"`{i:>3}.` {t.display}")
    await send_paginated(
        interaction,
        title="📜 Queue",
        items=items,
        empty_message="The queue is empty.",
        unit="lines",
        ephemeral=False,
    )


@bot.tree.command(name="nowplaying", description="Show the currently playing track")
async def nowplaying(interaction: discord.Interaction) -> None:
    player = bot.players.get(interaction.guild.id)
    if not player or player.current is None:
        await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
        return
    await interaction.response.send_message(f"▶️ Now playing: **{player.current.name}**")


@bot.tree.command(name="skip", description="Skip the current track")
async def skip(interaction: discord.Interaction) -> None:
    player = bot.players.get(interaction.guild.id)
    if player and player.skip():
        await interaction.response.send_message("⏭️ Skipped.")
    else:
        await interaction.response.send_message("Nothing to skip.", ephemeral=True)


@bot.tree.command(name="pause", description="Pause playback")
async def pause(interaction: discord.Interaction) -> None:
    player = bot.players.get(interaction.guild.id)
    if player and player.pause():
        await interaction.response.send_message("⏸️ Paused.")
    else:
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)


@bot.tree.command(name="resume", description="Resume playback")
async def resume(interaction: discord.Interaction) -> None:
    player = bot.players.get(interaction.guild.id)
    if player and player.resume():
        await interaction.response.send_message("▶️ Resumed.")
    else:
        await interaction.response.send_message("Nothing is paused.", ephemeral=True)


@bot.tree.command(name="stop", description="Stop playback and clear the queue")
async def stop(interaction: discord.Interaction) -> None:
    player = bot.players.get(interaction.guild.id)
    if player:
        player.stop()
        await interaction.response.send_message("⏹️ Stopped and cleared the queue.")
    else:
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)


@bot.tree.command(name="shuffle", description="Shuffle the up-next queue")
async def shuffle(interaction: discord.Interaction) -> None:
    import random

    player = bot.players.get(interaction.guild.id)
    if player and len(player.queue) > 1:
        random.shuffle(player.queue)
        await interaction.response.send_message("🔀 Shuffled the queue.")
    else:
        await interaction.response.send_message("Not enough tracks to shuffle.", ephemeral=True)


def _parse_position_or_range(spec: str, max_n: int) -> tuple[int, int] | None:
    """Parse '5' (single) or '3-7' (inclusive range); returns 1-based (start, end)."""
    s = spec.strip()
    # Normalize en-dash, em-dash, and `..` to a plain hyphen.
    for sep in ("..", "–", "—"):
        s = s.replace(sep, "-")
    if "-" in s:
        a_str, b_str = s.split("-", 1)
        try:
            a, b = int(a_str), int(b_str)
        except ValueError:
            return None
        if a > b:
            a, b = b, a
    else:
        try:
            a = int(s)
        except ValueError:
            return None
        b = a
    if a < 1 or b > max_n:
        return None
    return a, b


@bot.tree.command(name="remove", description="Remove a track (or a range) from the up-next queue")
@app_commands.describe(position="A position (e.g. 5) or a range (e.g. 3-7)")
async def remove(interaction: discord.Interaction, position: str) -> None:
    player = bot.players.get(interaction.guild.id)
    if not player or not player.queue:
        await interaction.response.send_message("The queue is empty.", ephemeral=True)
        return
    parsed = _parse_position_or_range(position, len(player.queue))
    if parsed is None:
        await interaction.response.send_message(
            f"❌ Invalid position. Use a number (e.g. `5`) or a range (e.g. `3-7`). "
            f"Queue has {len(player.queue)} track(s).",
            ephemeral=True,
        )
        return
    start, end = parsed
    removed = player.remove_range(start, end)
    if len(removed) == 1:
        await interaction.response.send_message(
            f"🗑️ Removed **{removed[0].display}** from the queue."
        )
    else:
        preview = "\n".join(f"• {t.display}" for t in removed[:10])
        extra = "" if len(removed) <= 10 else f"\n…and {len(removed) - 10} more."
        await interaction.response.send_message(
            f"🗑️ Removed **{len(removed)} tracks** (positions {start}–{end}):\n{preview}{extra}"
        )


@bot.tree.command(name="volume", description="Set playback volume (0–200%)")
@app_commands.describe(percent="Volume percentage, 0 to 200")
async def volume(interaction: discord.Interaction, percent: app_commands.Range[int, 0, 200]) -> None:
    player = bot.players.get(interaction.guild.id)
    if not player:
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        return
    player.set_volume(percent / 100)
    await interaction.response.send_message(f"🔊 Volume set to {percent}%.")


@bot.tree.command(name="help", description="Show available commands")
async def help_cmd(interaction: discord.Interaction) -> None:
    embed = discord.Embed(
        title="🎵 discomp3play",
        description="A Discord MP3 player that queues and streams local audio files.",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="▶️ Playback",
        value=(
            "`/play <query>` — Queue a track (autocomplete enabled)\n"
            "`/playalbum <album>` — Queue every track on an album\n"
            "`/playartist <artist>` — Queue every track by an artist\n"
            "`/pause` · `/resume` — Pause / resume playback\n"
            "`/skip` — Skip the current track\n"
            "`/stop` — Stop and clear the queue\n"
            "`/nowplaying` — Show what's playing\n"
            "`/volume <0–200>` — Set playback volume"
        ),
        inline=False,
    )
    embed.add_field(
        name="📜 Queue",
        value=(
            "`/queue` — Show the queue (paginated)\n"
            "`/shuffle` — Shuffle the up-next queue\n"
            "`/remove <pos|range>` — Remove one track or a range (e.g. `5` or `3-7`)"
        ),
        inline=False,
    )
    embed.add_field(
        name="📚 Library",
        value=(
            "`/list tracks` — Browse all tracks\n"
            "`/list albums` — Browse by album\n"
            "`/list artists` — Browse by artist"
        ),
        inline=False,
    )
    embed.add_field(
        name="🚪 Voice",
        value="`/leave` — Disconnect the bot from voice",
        inline=False,
    )
    embed.set_footer(text="Tip: /play autocompletes by title, artist, album, or filename.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="leave", description="Disconnect the bot from voice")
async def leave(interaction: discord.Interaction) -> None:
    player = bot.players.get(interaction.guild.id)
    if player or interaction.guild.voice_client:
        if player:
            await player.destroy(reason="leave")
        elif interaction.guild.voice_client:
            await interaction.guild.voice_client.disconnect(force=True)
        await interaction.response.send_message("👋 Left the voice channel.")
    else:
        await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)


# -- entry point ------------------------------------------------------------


def main() -> None:
    if not config.token:
        raise SystemExit(
            "DISCORD_TOKEN is not set. Copy .env.example to .env and add your bot token."
        )
    bot.run(config.token)


if __name__ == "__main__":
    main()
