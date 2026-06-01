"""Per-guild playback: a queue and an async loop that streams tracks in order."""

from __future__ import annotations

import asyncio

import discord

from library import Track

# ffmpeg input/output options. -vn drops any (album-art) video stream.
FFMPEG_OPTIONS = {"options": "-vn"}


class GuildPlayer:
    """Owns the queue and playback loop for a single guild.

    Playback is driven by a long-lived task (`_player_loop`). Track completion
    is signalled from ffmpeg's `after` callback (which runs on a non-async
    thread) back into the event loop via `call_soon_threadsafe`.
    """

    def __init__(self, bot: discord.Client, guild: discord.Guild, idle_timeout: int = 300):
        self.bot = bot
        self.guild = guild
        self.idle_timeout = idle_timeout
        self.queue: list[Track] = []
        self.current: Track | None = None
        self.text_channel: discord.abc.Messageable | None = None
        self.volume: float = 1.0
        self._next = asyncio.Event()
        self._notify = asyncio.Event()
        self._destroyed = False
        self.task = bot.loop.create_task(self._run())

    @property
    def voice_client(self) -> discord.VoiceClient | None:
        return self.guild.voice_client  # type: ignore[return-value]

    # -- queue mutation -----------------------------------------------------

    def add(self, track: Track) -> int:
        """Append a track; return its 1-based position in the up-next queue."""
        self.queue.append(track)
        self._notify.set()
        return len(self.queue)

    def remove_range(self, start: int, end: int) -> list[Track]:
        """Remove tracks at 1-based positions `start..end` (inclusive); return them."""
        s = max(0, start - 1)
        e = min(len(self.queue), end)
        if s >= e:
            return []
        removed = self.queue[s:e]
        del self.queue[s:e]
        return removed

    def clear(self) -> None:
        self.queue.clear()

    # -- playback loop ------------------------------------------------------

    async def _run(self) -> None:
        try:
            await self._player_loop()
        except asyncio.CancelledError:
            pass

    async def _player_loop(self) -> None:
        while not self._destroyed:
            self._next.clear()

            if not self.queue:
                self._notify.clear()
                try:
                    await asyncio.wait_for(self._notify.wait(), timeout=self.idle_timeout)
                except asyncio.TimeoutError:
                    await self.destroy(reason="idle")
                    return
                continue

            track = self.queue.pop(0)
            self.current = track

            vc = self.voice_client
            if vc is None or not vc.is_connected():
                # Lost the voice connection; drop the track and keep going.
                self.current = None
                await asyncio.sleep(0.5)
                continue

            try:
                source = discord.PCMVolumeTransformer(
                    discord.FFmpegPCMAudio(track.path, **FFMPEG_OPTIONS),
                    volume=self.volume,
                )
                vc.play(source, after=self._after)
            except Exception as exc:  # noqa: BLE001 - surface any ffmpeg/source error
                await self._send(f"⚠️ Failed to play **{track.name}**: `{exc}`")
                self.current = None
                continue

            await self._send(f"▶️ Now playing: **{track.name}**")
            await self._next.wait()
            self.current = None

    def _after(self, error: Exception | None) -> None:
        if error:
            print(f"[player] playback error in {self.guild.id}: {error}")
        # after() runs off the event loop thread; hop back on safely.
        self.bot.loop.call_soon_threadsafe(self._next.set)

    async def _send(self, message: str) -> None:
        if self.text_channel is not None:
            try:
                await self.text_channel.send(message)
            except discord.HTTPException:
                pass

    # -- transport controls -------------------------------------------------

    def skip(self) -> bool:
        vc = self.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()  # fires `after` -> advances the loop
            return True
        return False

    def pause(self) -> bool:
        vc = self.voice_client
        if vc and vc.is_playing():
            vc.pause()
            return True
        return False

    def resume(self) -> bool:
        vc = self.voice_client
        if vc and vc.is_paused():
            vc.resume()
            return True
        return False

    def stop(self) -> None:
        """Clear the queue and stop the current track (stays connected)."""
        self.queue.clear()
        vc = self.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()

    def set_volume(self, volume: float) -> None:
        self.volume = volume
        vc = self.voice_client
        if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = volume

    async def destroy(self, reason: str | None = None) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        self.queue.clear()
        vc = self.voice_client
        if vc:
            await vc.disconnect(force=True)
        self.bot.players.pop(self.guild.id, None)  # type: ignore[attr-defined]
        if not self.task.done():
            self.task.cancel()
