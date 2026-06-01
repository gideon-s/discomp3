# discomp3play

[![CI](https://github.com/gideon-s/discomp3/actions/workflows/ci.yml/badge.svg)](https://github.com/gideon-s/discomp3/actions/workflows/ci.yml)

A Discord bot that queues up local audio files and streams them into a voice
channel in sequence. Built with [discord.py](https://discordpy.readthedocs.io/)
and slash commands.

## Features

- 🎵 Scans a local folder (`music/` by default) for audio files
- 🔎 `/play` with autocomplete against your library
- 📜 Per-server queue with now-playing announcements
- ⏯️ `/skip`, `/pause`, `/resume`, `/stop`, `/shuffle`, `/remove`, `/volume`
- 🚪 Auto-disconnects when idle or left alone in a channel

## Requirements

- Python 3.10+
- [`ffmpeg`](https://ffmpeg.org/) on your `PATH` (used to decode/stream audio)

## Setup

### 1. Create the bot application

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) → **New Application**.
2. Open the **Bot** tab and click **Reset Token** to reveal your bot token. Keep it secret.
3. Invite the bot to your server with an OAuth2 URL using the scopes
   `bot` and `applications.commands`, and the permissions:
   **View Channel**, **Send Messages**, **Connect**, **Speak**.

   No privileged intents are required.

### 2. Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure

```bash
cp .env.example .env
# edit .env and paste your DISCORD_TOKEN
```

Set `GUILD_ID` to your server's ID during development so slash commands appear
instantly (global sync can take up to an hour).

### 4. Add music

Drop `.mp3` (or `.flac`, `.wav`, `.m4a`, `.ogg`, `.opus`, …) files into the
`music/` folder. Subfolders are scanned recursively.

### 5. Run

```bash
python bot.py
```

## Commands

| Command | Description |
| --- | --- |
| `/play <query>` | Queue a track by name or number; joins your voice channel |
| `/list` | Show available tracks in the library |
| `/queue` | Show the current queue |
| `/nowplaying` | Show the currently playing track |
| `/skip` | Skip the current track |
| `/pause` · `/resume` | Pause / resume playback |
| `/stop` | Stop and clear the queue |
| `/shuffle` | Shuffle the up-next queue |
| `/remove <position>` | Remove a queued track by position |
| `/volume <0–200>` | Set playback volume |
| `/leave` | Disconnect from voice |

## Project layout

| File | Purpose |
| --- | --- |
| `bot.py` | Bot setup, events, and slash commands |
| `player.py` | Per-guild queue and async playback loop |
| `library.py` | Scans the music folder and resolves track queries |
| `config.py` | Loads configuration from environment variables |
