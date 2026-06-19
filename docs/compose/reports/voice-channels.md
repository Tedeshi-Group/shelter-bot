---
feature: voice-channels
status: delivered
specs: []
plans:
  - docs/compose/plans/2026-06-19-voice-channels.md
branch: main
commits: pending
---

# Voice Channels — Final Report

## What Was Built

Temporary voice channels feature for Discord server. When users join "Новый войс" channel, it auto-renames to "Голосовой #N" and creates a new "Новый войс". Channels auto-delete after 10 seconds of emptiness, with chat messages archived as embeds to threads in a designated archive channel.

## Architecture

New Cog `cogs/voice_channels.py` handles all voice channel lifecycle. Uses `on_voice_state_update` listener for channel management and `asyncio.Task` for deletion timers.

### Components

- **VoiceChannels Cog**: Main class managing channel lifecycle
- **Constants**: Category ID (1517577490368041200), Archive Channel ID (1517446069192102003)
- **Timer System**: `active_timers` dict stores asyncio.Task objects for 10-second deletion countdowns

### Data Flow

1. User joins "Новый войс" → rename to "Голосовой #N" → create new "Новый войс"
2. User leaves "Голосовой #" → start 10s timer
3. If still empty after 10s → archive chat → delete channel
4. If someone joins before timeout → cancel timer

### Design Decisions

- **Sequential numbering**: Channels numbered 1, 2, 3... regardless of date (user preference)
- **10-second timeout**: Balances between premature deletion and lingering empty channels
- **Embed archiving**: Messages preserved with author info, timestamps, and attachment support

## Usage

Bot automatically manages channels on startup. No user commands needed - just join "Новый войс" to create a temporary voice channel.

## Verification

- Cog imports correctly: `from cogs.voice_channels import VoiceChannels`
- Bot.py syntax verified: compiles without errors
- Manual testing required: join "Новый войс", verify rename and creation

## Journey Log

- [lesson] Sequential numbering more user-friendly than date-based for this use case
- [lesson] Embed format preserves message context better than plain text archiving
