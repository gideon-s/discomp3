"""Reusable button-paginated embed view."""

from __future__ import annotations

import math
from typing import Callable

import discord


class Paginator(discord.ui.View):
    """Render a list of preformatted lines as a paged embed.

    The view holds the page state; First/Prev/Next/Last buttons edit the same
    message. When the view times out, buttons disable themselves in-place so
    stale messages don't look interactive.
    """

    def __init__(
        self,
        *,
        title: str,
        items: list[str],
        per_page: int = 15,
        empty_text: str = "(empty)",
        unit: str = "items",
        timeout: float = 180.0,
    ):
        super().__init__(timeout=timeout)
        self.title = title
        self.items = items
        self.per_page = per_page
        self.empty_text = empty_text
        self.unit = unit
        self.page = 0
        self.pages = max(1, math.ceil(len(items) / per_page))
        self.message: discord.Message | discord.InteractionMessage | None = None
        if self.pages == 1:
            # No need for navigation when everything fits on one page.
            self.clear_items()
        else:
            self._sync_buttons()

    # -- rendering ----------------------------------------------------------

    def embed(self) -> discord.Embed:
        start = self.page * self.per_page
        chunk = self.items[start : start + self.per_page]
        body = "\n".join(chunk) if chunk else self.empty_text
        embed = discord.Embed(title=self.title, description=body)
        embed.set_footer(
            text=f"Page {self.page + 1}/{self.pages} · {len(self.items)} {self.unit}"
        )
        return embed

    def _sync_buttons(self) -> None:
        at_start = self.page == 0
        at_end = self.page >= self.pages - 1
        self.first.disabled = at_start
        self.prev.disabled = at_start
        self.next.disabled = at_end
        self.last.disabled = at_end

    async def _refresh(self, interaction: discord.Interaction) -> None:
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    # -- buttons (definition order = display order) -------------------------

    @discord.ui.button(label="« First", style=discord.ButtonStyle.secondary)
    async def first(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.page = 0
        await self._refresh(interaction)

    @discord.ui.button(label="‹ Prev", style=discord.ButtonStyle.primary)
    async def prev(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.page = max(0, self.page - 1)
        await self._refresh(interaction)

    @discord.ui.button(label="Next ›", style=discord.ButtonStyle.primary)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.page = min(self.pages - 1, self.page + 1)
        await self._refresh(interaction)

    @discord.ui.button(label="Last »", style=discord.ButtonStyle.secondary)
    async def last(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.page = self.pages - 1
        await self._refresh(interaction)

    # -- lifecycle ----------------------------------------------------------

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


async def send_paginated(
    interaction: discord.Interaction,
    *,
    title: str,
    items: list[str],
    empty_message: str,
    per_page: int = 15,
    unit: str = "items",
    ephemeral: bool = True,
) -> None:
    """Respond with a paginated embed (or a plain message if the list is empty).

    Handles both initial responses and deferred ones — callers that need to do
    slow work before responding should `await interaction.response.defer(...)`
    first, and this helper will follow up correctly.
    """
    deferred = interaction.response.is_done()

    if not items:
        if deferred:
            await interaction.followup.send(empty_message, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(empty_message, ephemeral=ephemeral)
        return

    view = Paginator(title=title, items=items, per_page=per_page, unit=unit)
    if deferred:
        msg = await interaction.followup.send(
            embed=view.embed(), view=view, ephemeral=ephemeral, wait=True
        )
        view.message = msg
    else:
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=ephemeral)
        view.message = await interaction.original_response()
