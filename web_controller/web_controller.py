import asyncio
import json
from datetime import datetime, timedelta, timezone
import logging
import re

import uvicorn
import aiohttp
import secrets
from fastapi import (
    FastAPI,
    Depends,
    HTTPException,
    Header,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import os
from pydantic import BaseModel
from redbot.core import commands, Config
import lavalink


log = logging.getLogger("red.web_controller")


# â”€â”€ Request Models â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class PlayRequest(BaseModel):
    query: str
    index: int | None = None
    play_next: bool = False

class VolumeRequest(BaseModel):
    volume: int

class SeekRequest(BaseModel):
    position: int  # in seconds

class RemoveRequest(BaseModel):
    index: int

class ReorderRequest(BaseModel):
    from_index: int
    to_index: int

class CreatePlaylistRequest(BaseModel):
    name: str

class AddToPlaylistRequest(BaseModel):
    playlist_name: str
    track: dict

class RemoveFromPlaylistRequest(BaseModel):
    playlist_name: str
    track_uri: str

class ReorderPlaylistRequest(BaseModel):
    playlist_name: str
    from_index: int
    to_index: int

class LoadPlaylistRequest(BaseModel):
    playlist_name: str
    shuffle: bool = False

class RenamePlaylistRequest(BaseModel):
    old_name: str
    new_name: str

class SaveQueuePlaylistRequest(BaseModel):
    name: str
    include_current: bool = True

class ImportYoutubePlaylistRequest(BaseModel):
    url: str
    name: str | None = None

# â”€â”€ Helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

_YT_RE = re.compile(r"(?:youtube\.com/watch\?v=|youtu\.be/)([a-zA-Z0-9_-]{11})")

def _yt_thumbnail(uri: str, identifier: str = "") -> str:
    """Extract a YouTube thumbnail URL if possible."""
    m = _YT_RE.search(uri or "")
    if m:
        return f"https://img.youtube.com/vi/{m.group(1)}/hqdefault.jpg"
    if identifier:
        return f"https://img.youtube.com/vi/{identifier}/hqdefault.jpg"
    return ""

def _track_dict(track) -> dict:
    """Serialize a lavalink Track into a plain dict."""
    return {
        "title": track.title,
        "author": track.author,
        "uri": track.uri,
        "duration_ms": track.length,
        "thumbnail": _yt_thumbnail(track.uri, getattr(track, "identifier", "")),
        "requester": str(track.requester) if track.requester else None,
        "requester_avatar": str(track.requester.display_avatar.url) if hasattr(track.requester, "display_avatar") else None,
    }

def _fmt_ms(ms: int) -> str:
    s = ms // 1000
    return f"{s // 60}:{s % 60:02d}"


import discord
from redbot.core import app_commands

class PlayerControlView(discord.ui.View):
    def __init__(self, cog, guild_id: int, paused: bool = False, shuffle: bool = False):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.sync_buttons(paused=paused, shuffle=shuffle)

    def sync_buttons(self, paused: bool = False, shuffle: bool = False):
        for child in self.children:
            if getattr(child, "custom_id", None) == "wc_playpause":
                child.label = "▶️ Reanudar" if paused else "⏸️ Pausar"
                child.style = discord.ButtonStyle.danger if paused else discord.ButtonStyle.primary
            elif getattr(child, "custom_id", None) == "wc_shuffle":
                child.label = "🔀 Aleatorio: ON" if shuffle else "🔀 Aleatorio"
                child.style = discord.ButtonStyle.success if shuffle else discord.ButtonStyle.secondary

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        bot_voice = interaction.guild.me.voice
        user_voice = interaction.user.voice
        if bot_voice and bot_voice.channel:
            if not user_voice or user_voice.channel != bot_voice.channel:
                await interaction.response.send_message("No estás en llamada conmigo, únete a mi para poder usar los botones.", ephemeral=True)
                return False
        return True

    @discord.ui.button(label="⏸️ Pausar", style=discord.ButtonStyle.primary, custom_id="wc_playpause", row=0)
    async def play_pause(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self.cog._player(self.guild_id)
        if not player or not player.current:
            await self.cog._clear_now_playing_buttons(self.guild_id)
            try:
                await interaction.message.edit(view=None)
            except Exception:
                pass
            return await interaction.response.send_message("No hay nada sonando.", ephemeral=True)
        is_pausing = not player.paused
        await player.pause(is_pausing)
        
        action = "pausó" if is_pausing else "reanudó"
        user_disp = interaction.user.display_name
        asyncio.create_task(self.cog._send_discord_msg(
            f"💬 **{user_disp}** {action} la música desde Discord.",
            self.guild_id,
            delete_after=15.0,
            source="discord"
        ))
        asyncio.create_task(self.cog._send_toast(f"💬 {user_disp} {action} la música desde Discord", self.guild_id))
        asyncio.create_task(self.cog._broadcast_state(player))
        
        audio_cog = self.cog.bot.get_cog("Audio")
        shuffle = await audio_cog.config.guild(player.guild).shuffle() if audio_cog else False
        self.sync_buttons(paused=player.paused, shuffle=shuffle)
        embed = await self.cog._build_now_playing_embed(self.guild_id, player.current)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="⏮️ Reiniciar", style=discord.ButtonStyle.secondary, custom_id="wc_rewind", row=0)
    async def rewind(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self.cog._player(self.guild_id)
        if not player or not player.current:
            await self.cog._clear_now_playing_buttons(self.guild_id)
            try:
                await interaction.message.edit(view=None)
            except Exception:
                pass
            return await interaction.response.send_message("No hay nada sonando.", ephemeral=True)
        await player.seek(0)
        user_disp = interaction.user.display_name
        asyncio.create_task(self.cog._send_discord_msg(
            f"💬 **{user_disp}** reinició la canción desde Discord.",
            self.guild_id,
            delete_after=15.0,
            source="discord"
        ))
        asyncio.create_task(self.cog._send_toast(f"💬 {user_disp} reinició la canción desde Discord", self.guild_id))
        await interaction.response.defer()

    @discord.ui.button(label="⏭️ Saltar", style=discord.ButtonStyle.secondary, custom_id="wc_skip", row=0)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self.cog._player(self.guild_id)
        if not player or not player.current:
            await self.cog._clear_now_playing_buttons(self.guild_id)
            try:
                await interaction.message.edit(view=None)
            except Exception:
                pass
            return await interaction.response.send_message("No hay nada sonando.", ephemeral=True)
        skipped_title = player.current.title
        await player.skip()
        user_disp = interaction.user.display_name
        asyncio.create_task(self.cog._send_discord_msg(
            f"💬 **{user_disp}** saltó **{skipped_title}** desde Discord.",
            self.guild_id,
            delete_after=15.0,
            source="discord"
        ))
        asyncio.create_task(self.cog._send_toast(f"💬 {user_disp} saltó la canción desde Discord", self.guild_id))
        await interaction.response.defer()

    @discord.ui.button(label="⏹️ Detener", style=discord.ButtonStyle.danger, custom_id="wc_stop", row=0)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self.cog._player(self.guild_id)
        if not player:
            await self.cog._clear_now_playing_buttons(self.guild_id)
            try:
                await interaction.message.edit(view=None)
            except Exception:
                pass
            return await interaction.response.send_message("No hay reproductor.", ephemeral=True)
        player.queue.clear()
        await player.stop()
        self.cog.last_np_msgs.pop(self.guild_id, None)
        user_disp = interaction.user.display_name
        asyncio.create_task(self.cog._send_discord_msg(
            f"💬 **{user_disp}** detuvo la música y limpió la cola desde Discord.",
            self.guild_id,
            delete_after=15.0,
            source="discord"
        ))
        asyncio.create_task(self.cog._send_toast(f"💬 {user_disp} detuvo la música desde Discord", self.guild_id))
        asyncio.create_task(self.cog._broadcast_state(player))
        asyncio.create_task(self.cog._broadcast_queue(player))
        try:
            await interaction.response.edit_message(view=None)
        except Exception:
            await interaction.response.defer()

    @discord.ui.button(label="🔀 Aleatorio", style=discord.ButtonStyle.secondary, custom_id="wc_shuffle", row=1)
    async def toggle_shuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self.cog._player(self.guild_id)
        if not player or not player.current:
            return await interaction.response.send_message("No hay nada sonando.", ephemeral=True)
        audio_cog = self.cog.bot.get_cog("Audio")
        if not audio_cog:
            return await interaction.response.send_message("Módulo de audio no disponible.", ephemeral=True)
        current = await audio_cog.config.guild(player.guild).shuffle()
        new_val = not current
        await audio_cog.config.guild(player.guild).shuffle.set(new_val)
        status_word = "activó" if new_val else "desactivó"
        user_disp = interaction.user.display_name
        asyncio.create_task(self.cog._send_discord_msg(
            f"💬 **{user_disp}** {status_word} el modo aleatorio desde Discord.",
            self.guild_id,
            delete_after=15.0,
            source="discord"
        ))
        asyncio.create_task(self.cog._send_toast(f"💬 {user_disp} {status_word} el modo aleatorio", self.guild_id))
        asyncio.create_task(self.cog._broadcast_state(player))
        self.sync_buttons(paused=player.paused, shuffle=new_val)
        embed = await self.cog._build_now_playing_embed(self.guild_id, player.current)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="📋 Ver Cola", style=discord.ButtonStyle.secondary, custom_id="wc_viewqueue", row=1)
    async def view_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self.cog._player(self.guild_id)
        if not player or (not player.current and not player.queue):
            return await interaction.response.send_message("La cola está vacía.", ephemeral=True)
        lines = []
        if player.current:
            lines.append(f"▶️ **Sonando:** {player.current.title} (`{_fmt_ms(player.current.length)}`)")
        if player.queue:
            lines.append("\n**Próximas en cola:**")
            for idx, t in enumerate(list(player.queue)[:10], start=1):
                lines.append(f"`{idx}.` {t.title} (`{_fmt_ms(t.length)}`)")
            if len(player.queue) > 10:
                lines.append(f"*...y {len(player.queue) - 10} canciones más.*")
            total_ms = sum((t.length or 0) for t in player.queue)
            lines.append(f"\n📊 **Total en espera:** {len(player.queue)} canciones • `{_fmt_ms(total_ms)}`")
        else:
            lines.append("\n*No hay más canciones en espera.*")
        embed = discord.Embed(
            title="📋 Cola de Reproducción — Radio CriZpy",
            description="\n".join(lines),
            color=discord.Color.blurple()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

# â”€â”€ Cog â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class WebController(commands.Cog):
    """Control Radio CriZpy from a desktop app via a REST + WebSocket API."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=87102024, force_registration=True)
        self.config.register_global(
            api_key="change-this-api-key",
            port=8710,
            client_id="",
            client_secret="",
            redirect_uri="http://localhost:5173/callback",
            sessions={},
        )
        self.config.register_guild(
            text_channel_id=None,
            history=[],
            activity_log=[],
            dj_role_id=None,
            priority_role_id=None,
        )
        self.config.register_user(
            playlists={}
        )

        # FastAPI app
        self.app = FastAPI(title="Radio CriZpy API")
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
            allow_origin_regex=r"https://.*\.ngrok-free\.dev",
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        # Internal state
        self.server: uvicorn.Server | None = None
        self.server_task: asyncio.Task | None = None
        self.progress_task: asyncio.Task | None = None
        self._running = False
        self.ws_clients = {}  # { websocket: session_token }
        self.sessions = {}  # { token: user_dict }
        self.disconnect_timers = {}  # { guild_id: asyncio.Task }
        self._last_track_title: str | None = None
        self.last_np_msgs = {}
        self._search_cache = {}  # { query_lower: (timestamp_float, response_dict) }
        self._debounce_tasks = {}  # { (guild_id, key): asyncio.Task }

        self._setup_routes()

    # â”€â”€ Auth helper â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def _verify_key(self, x_api_key: str = Header(...)):
        expected = await self.config.api_key()
        if x_api_key != expected:
            raise HTTPException(status_code=403, detail="Invalid API Key")

    # â”€â”€ Player helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def _get_discord_user(self, token: str):
        if not token or token not in self.sessions:
            return None
        user_id = int(self.sessions[token]["id"])
        return self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)

    def _get_target_guild_and_channel(self, token: str | None):
        if not token or token not in self.sessions:
            return None, None
        user_id = int(self.sessions[token]["id"])
        
        for guild in self.bot.guilds:
            member = guild.get_member(user_id)
            if member and member.voice and member.voice.channel:
                if self.sessions[token].get("last_guild_id") != guild.id:
                    self.sessions[token]["last_guild_id"] = guild.id
                    asyncio.create_task(self.config.sessions.set(self.sessions))
                    asyncio.create_task(self._notify_channel_switch(token, guild.id))
                return guild.id, member.voice.channel
                
        last_guild_id = self.sessions[token].get("last_guild_id")
        return last_guild_id, None

    def _player(self, guild_id: int):
        if not guild_id: return None
        try:
            return lavalink.get_player(guild_id)
        except Exception:
            return None

    async def _ll_load_tracks(self, query: str):
        from urllib.parse import quote
        from lavalink.node import get_node
        from lavalink.rest_api import LoadResult, reformat_query
        node = get_node()
        protocol = "https" if node.secured else "http"
        raw_url = str(query).strip()
        parsed_url = reformat_query(raw_url)
        url = f"{protocol}://{node.host}:{node.port}/loadtracks?identifier={quote(parsed_url)}"
        headers = {"Authorization": node.password}
        async with node.session.get(url, headers=headers) as resp:
            data = await resp.json(content_type=None)
        if isinstance(data, dict):
            data["query"] = raw_url
            data["encodedquery"] = url
            return LoadResult(data)
        return None

    async def _verify_user_in_bot_channel(self, token: str, guild_id: int, voice_ch, player):
        if not guild_id:
            raise HTTPException(403, "No estás conectado al servidor.")
            
        user = await self._get_discord_user(token)
        if not user:
            raise HTTPException(403, "Sesión inválida.")

        # Super Admin Bypass (RedBot Owner)
        is_super_admin = await self.bot.is_owner(user)
        
        guild = self.bot.get_guild(guild_id)
        member = guild.get_member(user.id) if guild else None
        
        # Determine privileges
        has_priority = False
        is_owner = False
        if member:
            is_owner = (member.id == guild.owner_id)
            priority_role_id = await self.config.guild(guild).priority_role_id()
            if priority_role_id:
                has_priority = any(r.id == priority_role_id for r in member.roles)

        is_privileged = is_super_admin or is_owner or has_priority
        
        # Enforce Mandatory Role
        if not is_privileged:
            req_role_id = await self.config.guild(guild).dj_role_id()
            if req_role_id and member:
                has_req_role = any(r.id == req_role_id for r in member.roles)
                if not has_req_role:
                    role = guild.get_role(req_role_id)
                    role_name = role.name if role else "Requerido"
                    raise HTTPException(403, f"Necesitas el rol '{role_name}' para usar el reproductor.")
                            
        # Voice Channel restrictions
        # Super Admins can control the bot without being in the same channel (if it's already playing)
        if not is_super_admin:
            if player and player.channel:
                if not voice_ch:
                    raise HTTPException(403, "Debes estar en el canal de voz del bot.")
                if player.channel.id != voice_ch.id:
                    raise HTTPException(403, f"Debes estar en el canal '{player.channel.name}' con el bot.")
            else:
                if not voice_ch:
                    raise HTTPException(403, "Debes unirte a un canal de voz primero.")

    async def _ensure_voice(self, guild_id: int, user_voice_channel):
        player = self._player(guild_id)
        if player and (player.channel is not None):
            return player

        if not user_voice_channel:
            raise HTTPException(400, "Debes unirte a un canal de voz primero.")

        player = await lavalink.connect(user_voice_channel, self_deaf=True)
        await asyncio.sleep(0.5)
        
        vol = 30
        audio_cog = self.bot.get_cog("Audio")
        guild = self.bot.get_guild(guild_id)
        
        if audio_cog and guild:
            try:
                saved_vol = await audio_cog.config.guild(guild).volume()
                if saved_vol is not None:
                    vol = saved_vol
            except Exception:
                pass
                
        await player.set_volume(vol)
        
        if audio_cog and guild:
            try:
                await audio_cog.config.guild(guild).volume.set(vol)
            except Exception:
                pass
                    
        return player

    async def _auto_disconnect_task(self, guild_id: int):
        await asyncio.sleep(1800) # 30 minutos = 1800 segundos
        self.disconnect_timers.pop(guild_id, None)
        player = self._player(guild_id)
        if player:
            player.queue.clear()
            await player.stop()
            await player.disconnect()
            await self._clear_now_playing_buttons(guild_id)
            await self._send_discord_msg("ðŸ”Œ Me salí del canal de voz porque pasé 30 minutos completamente solo.", guild_id)
            asyncio.create_task(self._broadcast_state(player))
            asyncio.create_task(self._broadcast_queue(player))

    async def _log_activity(self, guild_id: int, text: str, source: str = "web"):
        if not guild_id:
            return
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return
        clean_text = (text or "").replace("**", "").strip()
        if self.bot.user:
            clean_text = clean_text.replace(self.bot.user.mention, f"@{self.bot.user.display_name}")
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=48)
        entry = {
            "id": secrets.token_hex(4),
            "text": clean_text,
            "source": source,
            "timestamp": now.isoformat(),
        }
        try:
            async with self.config.guild(guild).activity_log() as logs:
                logs.insert(0, entry)
                valid = []
                for item in logs:
                    ts_str = item.get("timestamp")
                    if ts_str:
                        try:
                            t = datetime.fromisoformat(ts_str)
                            if t.tzinfo is None:
                                t = t.replace(tzinfo=timezone.utc)
                            if t >= cutoff:
                                valid.append(item)
                        except ValueError:
                            valid.append(item)
                logs.clear()
                logs.extend(valid[:400])
            await self._broadcast({"type": "activity_update", "data": entry}, guild_id=guild_id)
        except Exception as e:
            log.error(f"Failed to log activity: {e}")

    def _schedule_debounced_notice(self, guild_id: int, key: str, delay: float, text: str, source: str = "web"):
        task_key = (guild_id, key)
        old_task = self._debounce_tasks.pop(task_key, None)
        if old_task:
            old_task.cancel()

        async def _runner():
            try:
                await asyncio.sleep(delay)
                await self._send_discord_msg(text, guild_id, delete_after=15.0, source=source)
            except asyncio.CancelledError:
                pass
            finally:
                self._debounce_tasks.pop(task_key, None)

        self._debounce_tasks[task_key] = asyncio.create_task(_runner())

    async def _send_discord_msg(self, text: str, guild_id: int, delete_after: float | None = None, source: str | None = None, log_activity: bool = True):
        if not guild_id:
            return
        if source is None:
            if "desde Discord" in text or text.startswith("💬"):
                source = "discord"
            elif "desde CriZpy Media Player" in text or text.startswith("🌐"):
                source = "web"
            else:
                source = "bot"
        if log_activity:
            asyncio.create_task(self._log_activity(guild_id, text, source=source))
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return
        channel_id = await self.config.guild(guild).text_channel_id()
        if not channel_id:
            return
        channel = guild.get_channel(channel_id)
        if channel:
            try:
                if delete_after:
                    await channel.send(text, delete_after=delete_after)
                else:
                    await channel.send(text)
            except Exception as e:
                log.error(f"Failed to send discord message: {e}")

    async def _clear_now_playing_buttons(self, guild_id: int):
        if not guild_id:
            return
        old_msg = self.last_np_msgs.pop(guild_id, None)
        if old_msg:
            try:
                await old_msg.edit(view=None)
            except Exception:
                pass

    async def _build_now_playing_embed(self, guild_id: int, track=None):
        player = self._player(guild_id)
        track = track or (player.current if player else None)
        if not track:
            return None
        guild = self.bot.get_guild(guild_id)
        is_paused = bool(player and player.paused)
        audio_cog = self.bot.get_cog("Audio")
        repeat = await audio_cog.config.guild(player.guild).repeat() if (audio_cog and player) else False
        shuffle = await audio_cog.config.guild(player.guild).shuffle() if (audio_cog and player) else False

        embed = discord.Embed(
            title="⏸️ En Pausa" if is_paused else "🎶 Sonando",
            description=f"**[{track.title}]({track.uri})**",
            color=discord.Color.orange() if is_paused else discord.Color.green()
        )
        embed.set_author(name=track.author)
        embed.add_field(name="⏱️ Duración", value=f"`{_fmt_ms(track.length)}`", inline=True)
        if player:
            flags = f"Vol: `{player.volume}%`"
            if shuffle:
                flags += " • 🔀 Aleatorio"
            if repeat:
                flags += " • 🔁 Repetir"
            embed.add_field(name="🎛️ Estado", value=flags, inline=True)
            if player.queue:
                next_t = player.queue[0]
                total_q = len(player.queue)
                total_ms = sum((t.length or 0) for t in player.queue)
                embed.add_field(
                    name="⏭️ Siguiente en cola",
                    value=f"**{next_t.title}** (`{_fmt_ms(next_t.length)}`)\n*{total_q} en espera • Total: `{_fmt_ms(total_ms)}`*",
                    inline=False
                )
            else:
                embed.add_field(name="⏭️ Siguiente en cola", value="*Fin de la cola*", inline=False)

        thumbnail = _yt_thumbnail(track.uri, getattr(track, "identifier", ""))
        if thumbnail:
            embed.set_thumbnail(url=thumbnail)

        req_name = None
        req_avatar = None
        if track.requester:
            if hasattr(track.requester, "display_name"):
                req_name = track.requester.display_name
                if hasattr(track.requester, "display_avatar") and track.requester.display_avatar:
                    req_avatar = str(track.requester.display_avatar.url)
            else:
                try:
                    uid = int(track.requester)
                    member = guild.get_member(uid) if guild else None
                    u = member or self.bot.get_user(uid)
                    if u:
                        req_name = u.display_name
                        if hasattr(u, "display_avatar") and u.display_avatar:
                            req_avatar = str(u.display_avatar.url)
                except (ValueError, TypeError):
                    req_name = str(track.requester)
        if req_name:
            if req_avatar:
                embed.set_footer(text=f"Pedido por: {req_name}", icon_url=req_avatar)
            else:
                embed.set_footer(text=f"Pedido por: {req_name}")
        return embed

    async def _refresh_now_playing_embed(self, guild_id: int):
        if not guild_id or guild_id not in self.last_np_msgs:
            return
        msg = self.last_np_msgs.get(guild_id)
        if not msg:
            return
        player = self._player(guild_id)
        if not player or not player.current:
            await self._clear_now_playing_buttons(guild_id)
            return
        try:
            audio_cog = self.bot.get_cog("Audio")
            shuffle = await audio_cog.config.guild(player.guild).shuffle() if audio_cog else False
            embed = await self._build_now_playing_embed(guild_id, player.current)
            view = PlayerControlView(self, guild_id, paused=player.paused, shuffle=shuffle)
            await msg.edit(embed=embed, view=view)
        except Exception as e:
            log.debug(f"Could not refresh now playing embed: {e}")

    async def _send_now_playing(self, track, guild_id: int):
        if not guild_id:
            return
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return
        channel_id = await self.config.guild(guild).text_channel_id()
        if not channel_id:
            return
        channel = guild.get_channel(channel_id)
        if channel:
            await self._clear_now_playing_buttons(guild_id)
            player = self._player(guild_id)
            audio_cog = self.bot.get_cog("Audio")
            shuffle = await audio_cog.config.guild(player.guild).shuffle() if (audio_cog and player) else False
            embed = await self._build_now_playing_embed(guild_id, track)
            view = PlayerControlView(self, guild_id, paused=bool(player and player.paused), shuffle=shuffle)
            try:
                msg = await channel.send(embed=embed, view=view)
                self.last_np_msgs[guild_id] = msg
            except Exception as e:
                log.error(f"Failed to send now playing message: {e}")

    # â”€â”€ WebSocket broadcasting â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def _notify_channel_switch(self, token: str, guild_id: int):
        for ws, ws_token in self.ws_clients.items():
            if ws_token == token:
                try:
                    await ws.send_json({
                        "type": "channel_switched",
                        "data": {"guild_id": str(guild_id)}
                    })
                except Exception:
                    pass

    async def _broadcast(self, message: dict, guild_id: int = None):
        if not self.ws_clients:
            return
        to_remove = []
        for ws, token in self.ws_clients.items():
            if guild_id is not None:
                user_guild, _ = self._get_target_guild_and_channel(token)
                if user_guild != guild_id:
                    continue
            try:
                await ws.send_json(message)
            except Exception:
                to_remove.append(ws)
        for ws in to_remove:
            self.ws_clients.pop(ws, None)

    async def _send_toast(self, message: str, guild_id: int = None):
        await self._broadcast({
            "type": "toast",
            "data": {"message": message}
        }, guild_id=guild_id)

    def _get_user_name(self, token: str | None, guild_id: int | None = None) -> str:
        if token and token in self.sessions:
            user_info = self.sessions[token]
            if guild_id:
                guild = self.bot.get_guild(guild_id)
                if guild:
                    member = guild.get_member(int(user_info["id"]))
                    if member:
                        return member.display_name
            return user_info["name"]
        return "Alguien"

    async def _broadcast_state(self, player):
        audio_cog = self.bot.get_cog("Audio")
        repeat = await audio_cog.config.guild(player.guild).repeat()
        shuffle = await audio_cog.config.guild(player.guild).shuffle()
        await self._broadcast({
            "type": "state_change",
            "data": {
                "paused": player.paused,
                "volume": player.volume,
                "repeat": repeat,
                "shuffle": shuffle,
                "connected": (player.channel is not None),
            },
        }, guild_id=player.guild.id)

    async def _broadcast_track(self, track, guild_id: int):
        if not track:
            await self._broadcast({"type": "track_change", "data": None}, guild_id=guild_id)
            return
        await self._broadcast({
            "type": "track_change",
            "data": _track_dict(track),
        }, guild_id=guild_id)

    async def _broadcast_queue(self, player):
        await self._broadcast({
            "type": "queue_update",
            "data": [_track_dict(t) for t in player.queue],
        }, guild_id=player.guild.id)

    # â”€â”€ Progress loop â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def _progress_loop(self):
        self._last_queue_len = {}
        while self._running:
            await asyncio.sleep(1)
            try:
                for g_id in list(self.last_np_msgs.keys()):
                    p = self._player(g_id)
                    if not p or not p.current:
                        await self._clear_now_playing_buttons(g_id)

                for player in lavalink.active_players():
                    guild_id = player.guild.id
                    current_q_len = len(player.queue)
                    last_len = self._last_queue_len.get(guild_id, 0)
                    
                    if current_q_len > last_len:
                        new_track = player.queue[-1]
                        await self._broadcast({
                            "type": "track_added",
                            "data": {"title": new_track.title}
                        }, guild_id=guild_id)
                        await self._broadcast_queue(player)
                    elif current_q_len < last_len:
                        await self._broadcast_queue(player)
                        
                    self._last_queue_len[guild_id] = current_q_len

                    if not player.current or player.paused or not self.ws_clients:
                        continue
                    
                    await self._broadcast({
                        "type": "progress",
                        "data": {
                            "position_ms": player.position,
                            "duration_ms": player.current.length,
                        },
                    }, guild_id=guild_id)
            except Exception:
                pass

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot:
            if self.bot.user and member.id == self.bot.user.id and before.channel and not after.channel:
                await self._clear_now_playing_buttons(member.guild.id)
            return
        guild = member.guild
        bot_voice = guild.me.voice
        if not bot_voice or not bot_voice.channel:
            return
        
        bot_channel = bot_voice.channel
        if (before.channel == bot_channel) or (after.channel == bot_channel):
            listeners = []
            for m in bot_channel.members:
                if m.bot:
                    continue
                avatar = m.display_avatar.url if m.display_avatar else None
                listeners.append({
                    "id": str(m.id),
                    "name": m.display_name,
                    "avatar": str(avatar),
                })
            await self._broadcast({
                "type": "listeners_update",
                "data": {"listeners": listeners, "channel_name": bot_channel.name}
            }, guild_id=guild.id)
            
            if len(listeners) == 0:
                if guild.id not in self.disconnect_timers:
                    self.disconnect_timers[guild.id] = asyncio.create_task(self._auto_disconnect_task(guild.id))
                    
                player = self._player(guild.id)
                if player and player.current and not player.paused:
                    await player.pause(True)
                    await self._send_discord_msg("⏸️ Pausé la música porque me dejaron hablando solo (no hay nadie en el canal de voz).", guild.id)
                    asyncio.create_task(self._broadcast_state(player))
            else:
                if guild.id in self.disconnect_timers:
                    self.disconnect_timers[guild.id].cancel()
                    self.disconnect_timers.pop(guild.id, None)

    # â”€â”€ Lavalink event listener â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def _ll_event(self, player, event_type, extra):
        try:
            guild_id = player.guild.id
            if event_type == lavalink.LavalinkEvents.TRACK_START:
                self._last_track_title = player.current.title if player.current else None
                await self._broadcast_track(player.current, guild_id)
                await self._broadcast_queue(player)
                
                if player.current:
                    await self._send_now_playing(player.current, guild_id)
                    asyncio.create_task(self._log_activity(
                        guild_id,
                        f"🎶 Empezó a sonar: {player.current.title} ({_fmt_ms(player.current.length)})",
                        source="bot"
                    ))
                    guild = self.bot.get_guild(guild_id)
                    if guild:
                        track = player.current
                        req_name = None
                        if track.requester:
                            if hasattr(track.requester, "display_name"):
                                req_name = track.requester.display_name
                            else:
                                try:
                                    u = self.bot.get_user(int(track.requester))
                                    req_name = u.display_name if u else str(track.requester)
                                except (ValueError, TypeError):
                                    req_name = str(track.requester)
                                    
                        entry = {
                            "title": track.title,
                            "author": track.author,
                            "uri": track.uri,
                            "duration_ms": track.length,
                            "thumbnail": _yt_thumbnail(track.uri, getattr(track, "identifier", "")),
                            "played_by": str(track.requester) if track.requester else None,
                            "played_by_name": req_name,
                            "played_at": datetime.now(timezone.utc).isoformat()
                        }
                        async with self.config.guild(guild).history() as hist:
                            hist.insert(0, entry)
                            
                            now = datetime.now(timezone.utc)
                            cutoff = now - timedelta(days=7)
                            new_hist = []
                            for h in hist:
                                if "played_at" in h:
                                    try:
                                        t = datetime.fromisoformat(h["played_at"])
                                        if t.tzinfo is None:
                                            t = t.replace(tzinfo=timezone.utc)
                                        if t >= cutoff:
                                            new_hist.append(h)
                                    except ValueError:
                                        new_hist.append(h)
                                else:
                                    new_hist.append(h)
                                    
                            hist.clear()
                            hist.extend(new_hist[:100])
                            
                        await self._broadcast({"type": "history_update", "data": entry}, guild_id=guild_id)
                    
            elif event_type in (
                lavalink.LavalinkEvents.QUEUE_END,
                lavalink.LavalinkEvents.TRACK_EXCEPTION,
                lavalink.LavalinkEvents.TRACK_STUCK,
                lavalink.LavalinkEvents.FORCED_DISCONNECT,
            ):
                await self._clear_now_playing_buttons(guild_id)
                if not player.current or event_type == lavalink.LavalinkEvents.QUEUE_END:
                    await self._broadcast_track(None, guild_id)
                await self._broadcast_state(player)
        except Exception as e:
            log.error(f"Error in lavalink event handler: {e}")

    # â”€â”€ Route setup â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _setup_routes(self):
        app = self.app

        class AuthCallbackRequest(BaseModel):
            code: str

        @app.get("/api/auth/url")
        async def auth_url():
            client_id = await self.config.client_id()
            redirect_uri = await self.config.redirect_uri()
            if not client_id:
                raise HTTPException(500, "Client ID not configured")
            url = f"https://discord.com/api/oauth2/authorize?client_id={client_id}&redirect_uri={redirect_uri}&response_type=code&scope=identify"
            return {"url": url}

        @app.post("/api/auth/callback")
        async def auth_callback(req: AuthCallbackRequest):
            client_id = await self.config.client_id()
            client_secret = await self.config.client_secret()
            redirect_uri = await self.config.redirect_uri()
            
            if not client_id or not client_secret:
                raise HTTPException(500, "OAuth2 not configured")
                
            data = {
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "authorization_code",
                "code": req.code,
                "redirect_uri": redirect_uri
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post("https://discord.com/api/oauth2/token", data=data) as resp:
                    if resp.status != 200:
                        raise HTTPException(400, "Discord OAuth error")
                    token_data = await resp.json()
                    access_token = token_data["access_token"]
                    
                async with session.get("https://discord.com/api/users/@me", headers={"Authorization": f"Bearer {access_token}"}) as resp:
                    if resp.status != 200:
                        raise HTTPException(400, "Failed to get user info")
                    user_data = await resp.json()
                    
            user_id = user_data["id"]
            username = user_data.get("global_name") or user_data.get("username")
            avatar = f"https://cdn.discordapp.com/avatars/{user_id}/{user_data['avatar']}.png" if user_data.get("avatar") else None
            
            session_token = secrets.token_urlsafe(32)
            user_info = {"id": user_id, "name": username, "avatar": avatar, "last_guild_id": None}
            self.sessions[session_token] = user_info
            asyncio.create_task(self.config.sessions.set(self.sessions))
            
            return {"session_token": session_token, "user": user_info}

        # â”€â”€ State â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        @app.get("/api/status")
        async def status(x_api_key: str = Header(...), x_session_token: str = Header(None)):
            await self._verify_key(x_api_key)
            guild_id, _ = self._get_target_guild_and_channel(x_session_token)
            guild = self.bot.get_guild(guild_id) if guild_id else None
            player = self._player(guild_id) if guild_id else None
            return {
                "status": "ok",
                "bot_name": str(self.bot.user),
                "guild_name": guild.name if guild else None,
                "connected": (player.channel is not None) if player else False,
            }

        # â”€â”€ Now Playing â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        @app.get("/api/now")
        async def now_playing(x_api_key: str = Header(...), x_session_token: str = Header(None)):
            await self._verify_key(x_api_key)
            guild_id, _ = self._get_target_guild_and_channel(x_session_token)
            if not guild_id:
                return {"playing": False}
            player = self._player(guild_id)
            if not player or not player.current:
                return {"playing": False}
            t = player.current
            audio_cog = self.bot.get_cog("Audio")
            repeat = await audio_cog.config.guild(player.guild).repeat()
            shuffle = await audio_cog.config.guild(player.guild).shuffle()
            return {
                "playing": True,
                **_track_dict(t),
                "position_ms": player.position,
                "paused": player.paused,
                "volume": player.volume,
                "repeat": repeat,
                "shuffle": shuffle,
            }

        # â”€â”€ Queue â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        @app.get("/api/queue")
        async def queue(x_api_key: str = Header(...), x_session_token: str = Header(None)):
            await self._verify_key(x_api_key)
            guild_id, _ = self._get_target_guild_and_channel(x_session_token)
            player = self._player(guild_id) if guild_id else None
            if not player:
                return {"queue": [], "total": 0}
            return {
                "queue": [_track_dict(t) for t in player.queue],
                "total": len(player.queue),
            }

        # â”€â”€ History â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        @app.get("/api/history")
        async def history(x_api_key: str = Header(...), x_session_token: str = Header(None)):
            await self._verify_key(x_api_key)
            guild_id, _ = self._get_target_guild_and_channel(x_session_token)
            if not guild_id:
                return {"history": []}
            guild = self.bot.get_guild(guild_id)
            if not guild:
                return {"history": []}
            hist = await self.config.guild(guild).history()
            
            now = datetime.now(timezone.utc)
            cutoff = now - timedelta(days=7)
            
            valid_hist = []
            for h in hist:
                if "played_by" in h and h["played_by"] and "played_by_name" not in h:
                    try:
                        u = self.bot.get_user(int(h["played_by"]))
                        if u:
                            h["played_by_name"] = u.display_name
                        else:
                            h["played_by_name"] = h["played_by"]
                    except (ValueError, TypeError):
                        h["played_by_name"] = str(h["played_by"])
                        
                if "played_at" in h:
                    try:
                        t = datetime.fromisoformat(h["played_at"])
                        if t.tzinfo is None:
                            t = t.replace(tzinfo=timezone.utc)
                        if t >= cutoff:
                            valid_hist.append(h)
                    except ValueError:
                        valid_hist.append(h)
                else:
                    valid_hist.append(h)
                    
            if len(valid_hist) != len(hist):
                await self.config.guild(guild).history.set(valid_hist)
                
            return {"history": valid_hist}

        @app.post("/api/history/clear")
        async def clear_history(x_api_key: str = Header(...), x_session_token: str = Header(None)):
            await self._verify_key(x_api_key)
            guild_id, _ = self._get_target_guild_and_channel(x_session_token)
            if not guild_id:
                raise HTTPException(400, "No guild")
            guild = self.bot.get_guild(guild_id)
            await self.config.guild(guild).history.set([])
            return {"cleared": True}

        @app.get("/api/activity")
        async def get_activity_log(x_api_key: str = Header(...), x_session_token: str = Header(None)):
            await self._verify_key(x_api_key)
            guild_id, _ = self._get_target_guild_and_channel(x_session_token)
            if not guild_id:
                return {"activity": []}
            guild = self.bot.get_guild(guild_id)
            if not guild:
                return {"activity": []}
            logs = await self.config.guild(guild).activity_log()
            now = datetime.now(timezone.utc)
            cutoff = now - timedelta(hours=48)
            valid = []
            for item in logs:
                ts_str = item.get("timestamp")
                if ts_str:
                    try:
                        t = datetime.fromisoformat(ts_str)
                        if t.tzinfo is None:
                            t = t.replace(tzinfo=timezone.utc)
                        if t >= cutoff:
                            valid.append(item)
                    except ValueError:
                        valid.append(item)
            if len(valid) != len(logs):
                await self.config.guild(guild).activity_log.set(valid)
            return {"activity": valid}

        # â”€â”€ Playlists â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        @app.get("/api/playlists")
        async def list_playlists(x_api_key: str = Header(...), x_session_token: str = Header(None)):
            await self._verify_key(x_api_key)
            user = await self._get_discord_user(x_session_token)
            if not user:
                raise HTTPException(401, "Login requerido")
            playlists = await self.config.user(user).playlists()
            summary = {name: len(tracks) for name, tracks in playlists.items()}
            durations = {
                name: sum((t.get("duration_ms") or 0) for t in tracks)
                for name, tracks in playlists.items()
            }
            return {"playlists": summary, "durations": durations}

        @app.post("/api/playlists/save_queue")
        async def save_queue_as_playlist(req: SaveQueuePlaylistRequest, x_api_key: str = Header(...), x_session_token: str = Header(None)):
            await self._verify_key(x_api_key)
            user = await self._get_discord_user(x_session_token)
            if not user:
                raise HTTPException(401, "Login requerido")
            name = (req.name or "").strip()
            if not name:
                raise HTTPException(400, "Nombre inválido")
            guild_id, _ = self._get_target_guild_and_channel(x_session_token)
            player = self._player(guild_id) if guild_id else None
            if not player or (not player.current and not player.queue):
                raise HTTPException(400, "La cola está vacía")

            collected = []
            seen_uris = set()
            if req.include_current and player.current:
                d = _track_dict(player.current)
                if d.get("uri"):
                    seen_uris.add(d["uri"])
                collected.append(d)
            for t in player.queue:
                d = _track_dict(t)
                uri = d.get("uri")
                if uri and uri not in seen_uris:
                    seen_uris.add(uri)
                    collected.append(d)

            async with self.config.user(user).playlists() as pls:
                if name in pls:
                    existing_uris = {t.get("uri") for t in pls[name]}
                    for item in collected:
                        if item.get("uri") not in existing_uris:
                            pls[name].append(item)
                            existing_uris.add(item.get("uri"))
                else:
                    pls[name] = collected
            return {"saved": name, "count": len(collected)}

        @app.post("/api/playlists/import_yt")
        async def import_yt_playlist(req: ImportYoutubePlaylistRequest, x_api_key: str = Header(...), x_session_token: str = Header(None)):
            await self._verify_key(x_api_key)
            user = await self._get_discord_user(x_session_token)
            if not user:
                raise HTTPException(401, "Login requerido")
            url = (req.url or "").strip()
            if not url.startswith("http"):
                raise HTTPException(400, "Pega un enlace válido de YouTube")
            result = await self._ll_load_tracks(url)
            tracks = result.tracks if result and hasattr(result, "tracks") else []
            if not tracks:
                raise HTTPException(404, "No se encontraron canciones en ese enlace")
            pl_info = getattr(result, "playlist_info", None)
            default_name = getattr(pl_info, "name", None) or tracks[0].title or "Playlist YouTube"
            name = (req.name or "").strip() or default_name

            serialized = []
            seen = set()
            for t in tracks[:200]:
                d = _track_dict(t)
                if d.get("uri") and d["uri"] not in seen:
                    seen.add(d["uri"])
                    serialized.append(d)

            async with self.config.user(user).playlists() as pls:
                if name in pls:
                    existing_uris = {t.get("uri") for t in pls[name]}
                    for item in serialized:
                        if item.get("uri") not in existing_uris:
                            pls[name].append(item)
                            existing_uris.add(item.get("uri"))
                else:
                    pls[name] = serialized
            return {"imported": name, "count": len(serialized)}

        @app.get("/api/playlists/{name}")
        async def get_playlist(name: str, x_api_key: str = Header(...), x_session_token: str = Header(None)):
            await self._verify_key(x_api_key)
            user = await self._get_discord_user(x_session_token)
            if not user:
                raise HTTPException(401, "Login requerido")
            playlists = await self.config.user(user).playlists()
            if name not in playlists:
                raise HTTPException(404, "Playlist no encontrada")
            return {"name": name, "tracks": playlists[name]}

        @app.post("/api/playlists/create")
        async def create_playlist(req: CreatePlaylistRequest, x_api_key: str = Header(...), x_session_token: str = Header(None)):
            await self._verify_key(x_api_key)
            user = await self._get_discord_user(x_session_token)
            if not user:
                raise HTTPException(401, "Login requerido")
            async with self.config.user(user).playlists() as pls:
                if req.name in pls:
                    raise HTTPException(400, "Ya existe esa playlist")
                pls[req.name] = []
            return {"created": req.name}

        @app.post("/api/playlists/add")
        async def add_to_playlist(req: AddToPlaylistRequest, x_api_key: str = Header(...), x_session_token: str = Header(None)):
            await self._verify_key(x_api_key)
            user = await self._get_discord_user(x_session_token)
            if not user:
                raise HTTPException(401, "Login requerido")
            async with self.config.user(user).playlists() as pls:
                if req.playlist_name not in pls:
                    raise HTTPException(404, "Playlist no encontrada")
                if not any(t["uri"] == req.track["uri"] for t in pls[req.playlist_name]):
                    pls[req.playlist_name].append(req.track)
            return {"added": True}

        
        @app.post("/api/playlists/reorder")
        async def reorder_playlist(req: ReorderPlaylistRequest, x_api_key: str = Header(...), x_session_token: str = Header(None)):
            await self._verify_key(x_api_key)
            user = await self._get_discord_user(x_session_token)
            if not user:
                raise HTTPException(401, "Login requerido")
            async with self.config.user(user).playlists() as pls:
                if req.playlist_name not in pls:
                    raise HTTPException(404, "Playlist no encontrada")
                tracks = pls[req.playlist_name]
                if req.from_index < 0 or req.from_index >= len(tracks):
                    raise HTTPException(400, "Índice inválido")
                track = tracks.pop(req.from_index)
                tracks.insert(req.to_index, track)
            return {"reordered": True}

        @app.post("/api/playlists/remove")
        async def remove_from_playlist(req: RemoveFromPlaylistRequest, x_api_key: str = Header(...), x_session_token: str = Header(None)):
            await self._verify_key(x_api_key)
            user = await self._get_discord_user(x_session_token)
            if not user:
                raise HTTPException(401, "Login requerido")
            async with self.config.user(user).playlists() as pls:
                if req.playlist_name not in pls:
                    raise HTTPException(404, "Playlist no encontrada")
                pls[req.playlist_name] = [t for t in pls[req.playlist_name] if t["uri"] != req.track_uri]
            return {"removed": True}

        @app.post("/api/playlists/rename")
        async def rename_playlist(req: RenamePlaylistRequest, x_api_key: str = Header(...), x_session_token: str = Header(None)):
            await self._verify_key(x_api_key)
            user = await self._get_discord_user(x_session_token)
            if not user:
                raise HTTPException(401, "Login requerido")
            old_name = (req.old_name or "").strip()
            new_name = (req.new_name or "").strip()
            if not old_name or not new_name:
                raise HTTPException(400, "Nombre inválido")
            if old_name == new_name:
                return {"renamed": new_name}
            async with self.config.user(user).playlists() as pls:
                if old_name not in pls:
                    raise HTTPException(404, "Playlist no encontrada")
                if new_name in pls:
                    raise HTTPException(400, "Ya existe una playlist con ese nombre")
                pls[new_name] = pls.pop(old_name)
            return {"renamed": new_name}

        @app.post("/api/playlists/load")
        async def load_playlist(req: LoadPlaylistRequest, x_api_key: str = Header(...), x_session_token: str = Header(None)):
            import random
            await self._verify_key(x_api_key)
            user = await self._get_discord_user(x_session_token)
            if not user:
                raise HTTPException(401, "Login requerido")
            
            guild_id, voice_ch = self._get_target_guild_and_channel(x_session_token)
            if not guild_id:
                raise HTTPException(400, "No estás en un canal de voz")
            
            playlists = await self.config.user(user).playlists()
            if req.playlist_name not in playlists:
                raise HTTPException(404, "Playlist no encontrada")
                
            player = await self._ensure_voice(guild_id, voice_ch)
            tracks_to_load = list(playlists[req.playlist_name])
            if not tracks_to_load:
                return {"loaded": 0, "playlist": req.playlist_name}

            if req.shuffle:
                random.shuffle(tracks_to_load)

            # 1. Load first valid track immediately so playback starts in <1s
            first_loaded = False
            start_idx = 0
            for idx, t in enumerate(tracks_to_load):
                start_idx = idx + 1
                try:
                    result = await self._ll_load_tracks(t["uri"])
                    if result and hasattr(result, "tracks") and result.tracks:
                        player.add(user.id, result.tracks[0])
                        first_loaded = True
                        break
                except Exception:
                    continue

            if first_loaded and not player.current:
                await player.play()
            asyncio.create_task(self._broadcast_queue(player))

            remaining = tracks_to_load[start_idx:]

            async def _load_remaining_batch():
                sem = asyncio.Semaphore(6)
                async def _fetch_one(item):
                    async with sem:
                        try:
                            res = await self._ll_load_tracks(item["uri"])
                            if res and hasattr(res, "tracks") and res.tracks:
                                return res.tracks[0]
                        except Exception:
                            pass
                        return None

                loaded_tracks = await asyncio.gather(*(_fetch_one(item) for item in remaining))
                for tr in loaded_tracks:
                    if tr is not None:
                        player.add(user.id, tr)
                if not player.current and player.queue:
                    await player.play()
                await self._broadcast_queue(player)
                await self._refresh_now_playing_embed(guild_id)

            if remaining:
                asyncio.create_task(_load_remaining_batch())
            else:
                asyncio.create_task(self._refresh_now_playing_embed(guild_id))

            total_expected = len(tracks_to_load)
            username = self._get_user_name(x_session_token, guild_id)
            shuffle_tag = " (en aleatorio 🔀)" if req.shuffle else ""
            await self._send_discord_msg(
                f"🌐 ▶️ **{username}** cargó la playlist **{req.playlist_name}**{shuffle_tag} ({total_expected} canciones) desde CriZpy Media Player.",
                guild_id,
                source="web",
            )
            
            return {"loaded": total_expected, "playlist": req.playlist_name, "shuffle": req.shuffle}

        @app.delete("/api/playlists/{name}")
        async def delete_playlist(name: str, x_api_key: str = Header(...), x_session_token: str = Header(None)):
            await self._verify_key(x_api_key)
            user = await self._get_discord_user(x_session_token)
            if not user:
                raise HTTPException(401, "Login requerido")
            async with self.config.user(user).playlists() as pls:
                if name not in pls:
                    raise HTTPException(404, "Playlist no encontrada")
                del pls[name]
            return {"deleted": name}

        # â”€â”€ Listeners â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        @app.get("/api/listeners")
        async def listeners(x_api_key: str = Header(...), x_session_token: str = Header(None)):
            await self._verify_key(x_api_key)
            guild_id, _ = self._get_target_guild_and_channel(x_session_token)
            if not guild_id:
                return {"listeners": []}
            guild = self.bot.get_guild(guild_id)
            if not guild or not guild.me.voice or not guild.me.voice.channel:
                return {"listeners": []}
            
            members = []
            for member in guild.me.voice.channel.members:
                if member.bot:
                    continue
                avatar = member.display_avatar.url if member.display_avatar else None
                members.append({
                    "id": str(member.id),
                    "name": member.display_name,
                    "avatar": str(avatar),
                })
            return {"listeners": members, "channel_name": guild.me.voice.channel.name}


        # â”€â”€ Search â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        @app.get("/api/search")
        async def search(q: str, x_api_key: str = Header(...), x_session_token: str = Header(None)):
            import time
            await self._verify_key(x_api_key)
            q_clean = (q or "").strip()
            if not q_clean:
                return {"results": [], "is_playlist": False, "playlist_name": None}

            cache_key = q_clean.lower()
            now_ts = time.time()
            cached = self._search_cache.get(cache_key)
            if cached and (now_ts - cached[0]) < 600:
                return cached[1]

            lookup = q_clean if q_clean.startswith("http") else f"ytsearch:{q_clean}"
            result = await self._ll_load_tracks(lookup)
            tracks = result.tracks if result and hasattr(result, "tracks") else []
            is_pl = bool(getattr(result, "is_playlist", False))
            pl_info = getattr(result, "playlist_info", None)
            pl_name = getattr(pl_info, "name", None) if is_pl else None
            limit = 100 if is_pl else 10
            payload = {
                "results": [_track_dict(t) for t in tracks[:limit]],
                "is_playlist": is_pl,
                "playlist_name": pl_name,
            }
            if payload["results"]:
                if len(self._search_cache) >= 120:
                    oldest_key = min(self._search_cache, key=lambda k: self._search_cache[k][0])
                    self._search_cache.pop(oldest_key, None)
                self._search_cache[cache_key] = (now_ts, payload)
            return payload

        @app.post("/api/queue/clear")
        async def clear_queue_only(x_api_key: str = Header(...), x_session_token: str = Header(None)):
            await self._verify_key(x_api_key)
            guild_id, voice_ch = self._get_target_guild_and_channel(x_session_token)
            player = self._player(guild_id) if guild_id else None
            await self._verify_user_in_bot_channel(x_session_token, guild_id, voice_ch, player)
            if not player:
                raise HTTPException(400, "No player")
            count = len(player.queue)
            player.queue.clear()
            username = self._get_user_name(x_session_token, guild_id)
            if count > 0:
                await self._send_discord_msg(
                    f"🌐 🧹 **{username}** limpió la cola de espera ({count} canciones) desde CriZpy Media Player.",
                    guild_id,
                    delete_after=15.0,
                    source="web",
                )
            asyncio.create_task(self._broadcast_queue(player))
            asyncio.create_task(self._refresh_now_playing_embed(guild_id))
            return {"cleared": count}

        # â”€â”€ Play â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        @app.post("/api/play")
        async def play(req: PlayRequest, x_api_key: str = Header(...), x_session_token: str = Header(None)):
            await self._verify_key(x_api_key)
            guild_id, voice_ch = self._get_target_guild_and_channel(x_session_token)
            player = self._player(guild_id)
            await self._verify_user_in_bot_channel(x_session_token, guild_id, voice_ch, player)
            
            player = await self._ensure_voice(guild_id, voice_ch)

            # Determine if it's a URL or search query
            query = (req.query or "").strip()
            if query.startswith("http"):
                result = await player.load_tracks(query)
            else:
                result = await player.search_yt(query)

            tracks = result.tracks if hasattr(result, "tracks") else result
            if not tracks:
                raise HTTPException(404, "No tracks found")

            requester_id = int(self.sessions[x_session_token]["id"]) if x_session_token and x_session_token in self.sessions else self.bot.user.id
            username = self._get_user_name(x_session_token, guild_id)
            is_pl = bool(query.startswith("http") and getattr(result, "is_playlist", False) and len(tracks) > 1)

            if is_pl:
                pl_info = getattr(result, "playlist_info", None)
                pl_name = getattr(pl_info, "name", None) or "Playlist de YouTube"
                batch = list(tracks[:150])
                if req.play_next and player.current:
                    for idx, t in enumerate(batch):
                        t.requester = requester_id
                        player.queue.insert(idx, t)
                else:
                    for t in batch:
                        player.add(requester_id, t)
                if not player.current:
                    await player.play()
                await self._send_discord_msg(
                    f"🌐 ▶️ **{username}** agregó la playlist de YouTube **{pl_name}** ({len(batch)} canciones) a la cola desde CriZpy Media Player.",
                    guild_id,
                    source="web",
                )
                asyncio.create_task(self._broadcast_queue(player))
                asyncio.create_task(self._refresh_now_playing_embed(guild_id))
                return {
                    "queued": _track_dict(batch[0]),
                    "queued_count": len(batch),
                    "is_playlist": True,
                    "playlist_name": pl_name,
                    "position_in_queue": len(player.queue),
                }

            track = tracks[0]
            dur_str = _fmt_ms(track.length)
            was_playing = bool(player.current)
            if req.play_next and was_playing:
                track.requester = requester_id
                player.queue.insert(0, track)
                pos = 1
                msg_text = f"🌐 ⏭️ **{username}** puso para sonar siguiente: **{track.title}** (`{dur_str}`)"
            elif req.index is not None and was_playing and 0 <= req.index < len(player.queue):
                track.requester = requester_id
                player.queue.insert(req.index, track)
                pos = req.index + 1
                msg_text = f"🌐 ▶️ **{username}** agregó a la cola (#{pos}): **{track.title}** (`{dur_str}`)"
            else:
                player.add(requester_id, track)
                pos = len(player.queue)
                pos_tag = f" • Posición #{pos}" if was_playing and pos > 0 else ""
                msg_text = f"🌐 ▶️ **{username}** agregó a la cola: **{track.title}** (`{dur_str}`){pos_tag}"

            if not player.current:
                await player.play()

            await self._send_discord_msg(msg_text, guild_id, source="web")
            asyncio.create_task(self._broadcast_queue(player))
            if was_playing:
                asyncio.create_task(self._refresh_now_playing_embed(guild_id))

            return {
                "queued": _track_dict(track),
                "position_in_queue": pos,
                "played_next": bool(req.play_next and was_playing),
            }

        # â”€â”€ Summon â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        @app.post("/api/summon")
        async def summon(x_api_key: str = Header(...), x_session_token: str = Header(None)):
            await self._verify_key(x_api_key)
            guild_id, voice_ch = self._get_target_guild_and_channel(x_session_token)
            player = self._player(guild_id)
            await self._verify_user_in_bot_channel(x_session_token, guild_id, voice_ch, player)
            if player and player.channel is not None:
                return {"summoned": False, "already_connected": True}
                
            await self._ensure_voice(guild_id, voice_ch)
            username = self._get_user_name(x_session_token, guild_id)
            await self._send_discord_msg(f"🌐 **{username}** conectó a {self.bot.user.mention} al canal de voz desde CriZpy Media Player.", guild_id, delete_after=15.0, source="web")
            return {"summoned": True}

        # â”€â”€ Pause / Resume â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        @app.post("/api/pause")
        async def pause(x_api_key: str = Header(...), x_session_token: str = Header(None)):
            await self._verify_key(x_api_key)
            guild_id, voice_ch = self._get_target_guild_and_channel(x_session_token)
            player = self._player(guild_id) if guild_id else None
            await self._verify_user_in_bot_channel(x_session_token, guild_id, voice_ch, player)
            
            if not player or not player.current:
                raise HTTPException(400, "Nothing is playing")
            await player.pause(not player.paused)
            
            username = self._get_user_name(x_session_token, guild_id)
            status = "pausó" if player.paused else "reanudó"
            await self._send_discord_msg(f"🌐 **{username}** {status} la música desde CriZpy Media Player.", guild_id, delete_after=15.0, source="web")
            asyncio.create_task(self._broadcast_state(player))
            asyncio.create_task(self._refresh_now_playing_embed(guild_id))
            return {"paused": player.paused}

        # â”€â”€ Skip â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        @app.post("/api/skip")
        async def skip(x_api_key: str = Header(...), x_session_token: str = Header(None)):
            await self._verify_key(x_api_key)
            guild_id, voice_ch = self._get_target_guild_and_channel(x_session_token)
            player = self._player(guild_id) if guild_id else None
            await self._verify_user_in_bot_channel(x_session_token, guild_id, voice_ch, player)
            if not player or not player.current:
                raise HTTPException(400, "Nothing is playing")

            skipped_title = player.current.title
            await player.skip()
            username = self._get_user_name(x_session_token, guild_id)
            asyncio.create_task(self._send_discord_msg(
                f"🌐 ⏭️ **{username}** saltó **{skipped_title}** desde CriZpy Media Player.",
                guild_id,
                delete_after=15.0,
                source="web"
            ))
            return {"skipped": True}

        # â”€â”€ Previous â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        @app.post("/api/prev")
        async def prev(x_api_key: str = Header(...), x_session_token: str = Header(None)):
            await self._verify_key(x_api_key)
            guild_id, voice_ch = self._get_target_guild_and_channel(x_session_token)
            player = self._player(guild_id) if guild_id else None
            await self._verify_user_in_bot_channel(x_session_token, guild_id, voice_ch, player)
            if not player or not player.current:
                raise HTTPException(400, "Nothing is playing")
            # Lavalink doesn't natively support prev; seek to 0 as fallback
            await player.seek(0)
            username = self._get_user_name(x_session_token, guild_id)
            await self._send_discord_msg(f"🌐 ⏮️ **{username}** reinició la canción actual desde CriZpy Media Player.", guild_id, delete_after=15.0, source="web")
            return {"rewound": True}

        # â”€â”€ Stop â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        @app.post("/api/stop")
        async def stop(x_api_key: str = Header(...), x_session_token: str = Header(None)):
            await self._verify_key(x_api_key)
            guild_id, voice_ch = self._get_target_guild_and_channel(x_session_token)
            player = self._player(guild_id) if guild_id else None
            await self._verify_user_in_bot_channel(x_session_token, guild_id, voice_ch, player)
            if not player:
                raise HTTPException(400, "No player")
            player.queue.clear()
            await player.stop()
            await self._clear_now_playing_buttons(guild_id)
            username = self._get_user_name(x_session_token, guild_id)
            await self._send_discord_msg(f"🌐 ⏹️ **{username}** detuvo la reproducción y limpió la cola desde CriZpy Media Player.", guild_id, delete_after=15.0, source="web")
            asyncio.create_task(self._broadcast_state(player))
            asyncio.create_task(self._broadcast_queue(player))
            return {"stopped": True}

        # â”€â”€ Volume â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        @app.post("/api/volume")
        async def volume(req: VolumeRequest, x_api_key: str = Header(...), x_session_token: str = Header(None)):
            await self._verify_key(x_api_key)
            guild_id, voice_ch = self._get_target_guild_and_channel(x_session_token)
            player = self._player(guild_id) if guild_id else None
            await self._verify_user_in_bot_channel(x_session_token, guild_id, voice_ch, player)
            if not player:
                raise HTTPException(400, "No player")
            vol = max(0, min(150, req.volume))
            await player.set_volume(vol)
            
            # Sincronizar con el config nativo de Audio de RedBot para que no se reinicie
            guild = self.bot.get_guild(guild_id)
            audio_cog = self.bot.get_cog("Audio")
            if audio_cog and guild:
                try:
                    await audio_cog.config.guild(guild).volume.set(vol)
                except Exception as e:
                    log.error(f"Failed to sync volume with Audio cog: {e}")

            username = self._get_user_name(x_session_token, guild_id)
            self._schedule_debounced_notice(
                guild_id,
                "volume",
                2.0,
                f"🌐 🔊 **{username}** ajustó el volumen a **{vol}%** desde CriZpy Media Player.",
                source="web"
            )
            asyncio.create_task(self._broadcast_state(player))
            asyncio.create_task(self._refresh_now_playing_embed(guild_id))
            return {"volume": vol}

        # â”€â”€ Seek â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        @app.post("/api/seek")
        async def seek(req: SeekRequest, x_api_key: str = Header(...), x_session_token: str = Header(None)):
            await self._verify_key(x_api_key)
            guild_id, voice_ch = self._get_target_guild_and_channel(x_session_token)
            player = self._player(guild_id) if guild_id else None
            await self._verify_user_in_bot_channel(x_session_token, guild_id, voice_ch, player)
            if not player or not player.current:
                raise HTTPException(400, "Nothing is playing")
            ms = req.position * 1000
            await player.seek(ms)
            username = self._get_user_name(x_session_token, guild_id)
            self._schedule_debounced_notice(
                guild_id,
                "seek",
                2.0,
                f"🌐 ⏩ **{username}** movió la canción a **`{_fmt_ms(ms)}`** desde CriZpy Media Player.",
                source="web"
            )
            return {"position_ms": ms}

        # â”€â”€ Remove from queue â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        @app.post("/api/remove")
        async def remove(req: RemoveRequest, x_api_key: str = Header(...), x_session_token: str = Header(None)):
            await self._verify_key(x_api_key)
            guild_id, voice_ch = self._get_target_guild_and_channel(x_session_token)
            player = self._player(guild_id) if guild_id else None
            await self._verify_user_in_bot_channel(x_session_token, guild_id, voice_ch, player)
            if not player:
                raise HTTPException(400, "No player")
            idx = req.index
            if idx < 0 or idx >= len(player.queue):
                raise HTTPException(400, "Invalid queue index")
            removed = player.queue.pop(idx)
            username = self._get_user_name(x_session_token, guild_id)
            await self._send_discord_msg(f"🌐 ❌ **{username}** quitó de la cola: **{removed.title}** desde CriZpy Media Player.", guild_id, delete_after=15.0, source="web")
            asyncio.create_task(self._broadcast_queue(player))
            asyncio.create_task(self._refresh_now_playing_embed(guild_id))
            return {"removed": _track_dict(removed)}

        # â”€â”€ Reorder queue â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        @app.post("/api/reorder")
        async def reorder(req: ReorderRequest, x_api_key: str = Header(...), x_session_token: str = Header(None)):
            await self._verify_key(x_api_key)
            guild_id, _ = self._get_target_guild_and_channel(x_session_token)
            player = self._player(guild_id)
            if not player or not player.queue:
                raise HTTPException(400, "Queue empty")
            
            if req.from_index < 0 or req.from_index >= len(player.queue):
                raise HTTPException(400, "Invalid from_index")
            if req.to_index < 0 or req.to_index >= len(player.queue):
                raise HTTPException(400, "Invalid to_index")
                
            track = player.queue.pop(req.from_index)
            player.queue.insert(req.to_index, track)
            username = self._get_user_name(x_session_token, guild_id)
            asyncio.create_task(self._log_activity(
                guild_id,
                f"🌐 ↕️ {username} movió '{track.title}' a la posición #{req.to_index + 1} de la cola.",
                source="web"
            ))
            await self._broadcast_queue(player)
            asyncio.create_task(self._refresh_now_playing_embed(guild_id))
            return {"queue": [_track_dict(t) for t in player.queue]}

        # â”€â”€ Repeat â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        @app.post("/api/repeat")
        async def repeat(x_api_key: str = Header(...), x_session_token: str = Header(None)):
            await self._verify_key(x_api_key)
            guild_id, voice_ch = self._get_target_guild_and_channel(x_session_token)
            player = self._player(guild_id) if guild_id else None
            await self._verify_user_in_bot_channel(x_session_token, guild_id, voice_ch, player)
            if not player:
                raise HTTPException(400, "No player")
            audio_cog = self.bot.get_cog("Audio")
            current = await audio_cog.config.guild(player.guild).repeat()
            await audio_cog.config.guild(player.guild).repeat.set(not current)
            status = "activó" if not current else "desactivó"
            username = self._get_user_name(x_session_token, guild_id)
            await self._send_discord_msg(f"🌐 🔁 **{username}** {status} la repetición desde CriZpy Media Player.", guild_id, delete_after=15.0, source="web")
            asyncio.create_task(self._broadcast_state(player))
            asyncio.create_task(self._refresh_now_playing_embed(guild_id))
            return {"repeat": not current}

        # â”€â”€ Shuffle â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        @app.post("/api/shuffle")
        async def shuffle(x_api_key: str = Header(...), x_session_token: str = Header(None)):
            await self._verify_key(x_api_key)
            guild_id, voice_ch = self._get_target_guild_and_channel(x_session_token)
            player = self._player(guild_id) if guild_id else None
            await self._verify_user_in_bot_channel(x_session_token, guild_id, voice_ch, player)
            if not player:
                raise HTTPException(400, "No player")
            audio_cog = self.bot.get_cog("Audio")
            current = await audio_cog.config.guild(player.guild).shuffle()
            await audio_cog.config.guild(player.guild).shuffle.set(not current)
            status = "activó" if not current else "desactivó"
            username = self._get_user_name(x_session_token, guild_id)
            await self._send_discord_msg(f"🌐 🔀 **{username}** {status} el modo aleatorio desde CriZpy Media Player.", guild_id, delete_after=15.0, source="web")
            asyncio.create_task(self._broadcast_state(player))
            asyncio.create_task(self._refresh_now_playing_embed(guild_id))
            return {"shuffle": not current}



        # â”€â”€ Disconnect â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        @app.post("/api/disconnect")
        async def disconnect(x_api_key: str = Header(...), x_session_token: str = Header(None)):
            await self._verify_key(x_api_key)
            guild_id, voice_ch = self._get_target_guild_and_channel(x_session_token)
            player = self._player(guild_id) if guild_id else None
            await self._verify_user_in_bot_channel(x_session_token, guild_id, voice_ch, player)
            
            if not player or player.channel is None:
                return {"disconnected": False, "not_in_voice": True}
                
            player.queue.clear()
            await player.stop()
            await player.disconnect()
            await self._clear_now_playing_buttons(guild_id)
            
            username = self._get_user_name(x_session_token, guild_id)
            await self._send_discord_msg(f"**{username}** desconectó a {self.bot.user.mention} del canal de voz desde CriZpy Media Player.", guild_id)
            
            asyncio.create_task(self._broadcast_state(player))
            return {"disconnected": True}

        # â”€â”€ WebSocket â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        @app.websocket("/ws")
        async def ws_endpoint(websocket: WebSocket, api_key: str = "", session_token: str = None):
            expected = await self.config.api_key()
            if api_key != expected:
                await websocket.close(code=1008, reason="Invalid API Key")
                return
            await websocket.accept()
            self.ws_clients[websocket] = session_token
            log.info("WebSocket client connected (%d total)", len(self.ws_clients))
            try:
                while True:
                    await websocket.receive_text()
            except WebSocketDisconnect:
                pass
            finally:
                self.ws_clients.pop(websocket, None)
                log.info("WebSocket client disconnected (%d remaining)", len(self.ws_clients))



    # â”€â”€ Lifecycle â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def cog_load(self):
        self.sessions = await self.config.sessions()
        port = await self.config.port()
        config = uvicorn.Config(
            self.app, host="0.0.0.0", port=port, log_level="warning",
        )
        self.server = uvicorn.Server(config)
        self._running = True
        self.server_task = asyncio.create_task(self.server.serve())
        self.progress_task = asyncio.create_task(self._progress_loop())
        lavalink.register_event_listener(self._ll_event)
        log.info("WebController API started on port %d", port)

    async def cog_unload(self):
        self._running = False
        for g_id in list(self.last_np_msgs.keys()):
            await self._clear_now_playing_buttons(g_id)
        lavalink.unregister_event_listener(self._ll_event)
        if self.server:
            self.server.should_exit = True
        if self.server_task:
            try:
                await asyncio.wait_for(self.server_task, timeout=5)
            except asyncio.TimeoutError:
                self.server_task.cancel()
        
        for task in self.disconnect_timers.values():
            task.cancel()
        self.disconnect_timers.clear()
        
        if self.progress_task:
            self.progress_task.cancel()
        for ws in list(self.ws_clients):
            try:
                await ws.close()
            except Exception:
                pass
        self.ws_clients.clear()
        log.info("WebController API stopped")

    # â”€â”€ Red commands for configuration â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    @commands.group(name="wc", aliases=["webcontroller"])
    @commands.is_owner()
    async def wc_group(self, ctx):
        """⚙️ Configuración del Web Controller para Radio CriZpy."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @commands.Cog.listener()
    async def on_command(self, ctx):
        """Auto-configura el canal de texto si no hay uno fijado y registra comandos de audio en el historial de 48h"""
        if not ctx.guild or not ctx.command:
            return
        cmd_name = ctx.command.qualified_name.lower()
        if cmd_name in ["play", "p", "search", "join", "connect"]:
            current_id = await self.config.guild(ctx.guild).text_channel_id()
            if not current_id or not ctx.guild.get_channel(current_id):
                target_channel = ctx.channel.parent if isinstance(ctx.channel, discord.Thread) else ctx.channel
                await self.config.guild(ctx.guild).text_channel_id.set(target_channel.id)
        audio_cmds = {"play", "p", "skip", "stop", "pause", "resume", "prev", "shuffle", "repeat", "volume", "disconnect", "dc", "summon", "join", "bump", "remove"}
        if cmd_name in audio_cmds and ctx.author and not ctx.author.bot:
            raw_content = (ctx.message.content or "").strip()
            asyncio.create_task(self._log_activity(
                ctx.guild.id,
                f"💬 {ctx.author.display_name} ejecutó comando en Discord: {raw_content[:120]}",
                source="discord"
            ))

    @commands.command(name="setchannel")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_channels=True)
    async def setchannel_cmd(self, ctx, canal: discord.TextChannel = None):
        """Fija el canal donde el bot enviará todos sus avisos (Web y Discord)."""
        if canal is None:
            canal = ctx.channel
        await self.config.guild(ctx.guild).text_channel_id.set(canal.id)
        await ctx.send(f"✅ ¡Plomo! A partir de ahora mandaré todos los avisos del reproductor por {canal.mention}.")

    @wc_group.command(name="setkey")
    async def wc_setkey(self, ctx, key: str):
        """Cambia la API key. ¡Borra el mensaje después!"""
        await self.config.api_key.set(key)
        await ctx.send("✅ API Key actualizada. ¡Borra este mensaje por seguridad!")

    @commands.command(name="setrol")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_roles=True)
    async def setrol_cmd(self, ctx, rol: discord.Role = None):
        """Fija el rol obligatorio para usar la web. Usa !setrol sin nada para quitarlo."""
        if rol is None:
            await self.config.guild(ctx.guild).dj_role_id.set(None)
            await ctx.send("ðŸ”“ Rol obligatorio quitado en este servidor. Ahora cualquiera en el canal de voz puede usar la web.")
        else:
            await self.config.guild(ctx.guild).dj_role_id.set(rol.id)
            await ctx.send(f"ðŸ”’ Listo, compadre. En este servidor ahora es **obligatorio** tener el rol {rol.mention} para controlar la música desde la web.")

    @commands.command(name="setrolprioridad")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_roles=True)
    async def setrolprioridad_cmd(self, ctx, rol: discord.Role = None):
        """Fija el rol con PRIORIDAD para usar la web saltándose los requisitos."""
        if rol is None:
            await self.config.guild(ctx.guild).priority_role_id.set(None)
            await ctx.send("ðŸ”“ Rol de prioridad quitado en este servidor.")
        else:
            await self.config.guild(ctx.guild).priority_role_id.set(rol.id)
            await ctx.send(f"ðŸ‘‘ El rol {rol.mention} ahora es VIP en este servidor. Los que lo tengan pueden controlar la música saltándose el rol obligatorio.")

    @wc_group.command(name="setport")
    async def wc_setport(self, ctx, port: int):
        """Cambia el puerto de la API (requiere reiniciar el cog)."""
        await self.config.port.set(port)
        await ctx.send(f"✅ Puerto cambiado a `{port}`. Reinicia el cog: `!reload web_controller`")

    @wc_group.command(name="setclient")
    async def wc_setclient(self, ctx, client_id: str):
        """ðŸ”‘ Configura el Client ID de Discord OAuth2"""
        await self.config.client_id.set(client_id)
        await ctx.send("✅ Client ID configurado.")

    @wc_group.command(name="setredirect")
    async def wc_setredirect(self, ctx, url: str):
        """🌐 Configura la URL de Redirect para Discord OAuth2"""
        await self.config.redirect_uri.set(url)
        await ctx.send(f"✅ Redirect URI configurado a: `{url}`")

    @wc_group.command(name="setsecret")
    async def wc_setsecret(self, ctx, client_secret: str):
        """ðŸ”‘ Configura el Client Secret de Discord OAuth2 (Hazlo por MD)"""
        await self.config.client_secret.set(client_secret)
        if ctx.guild:
            await ctx.message.delete()
        await ctx.send("✅ Client Secret configurado (mensaje borrado por seguridad).")

    @wc_group.command(name="info")
    async def wc_info(self, ctx):
        """ℹ️ Muestra la configuración actual del Web Controller."""
        port = await self.config.port()
        key = await self.config.api_key()
        cid = await self.config.client_id()
        text_id = await self.config.guild(ctx.guild).text_channel_id() if ctx.guild else None
        txt_ch = ctx.guild.get_channel(text_id) if ctx.guild and text_id else None

        msg = (
            "**🎛️ Web Controller — Radio CriZpy**\n"
            f"🌐 API: `http://localhost:{port}`\n"
            f"ðŸ”‘ Key: ||`{key}`||\n"
            f"ðŸ’¬ Canal de texto actual: {txt_ch.mention if txt_ch else 'No configurado'}\n"
            f"ðŸ“¡ Clientes WS: `{len(self.ws_clients)}`\n"
            f"ðŸ†” Client ID OAuth2: `{cid if cid else 'No configurado'}`"
        )
        await ctx.send(msg)

    @app_commands.command(name="rol", description="Configura el rol necesario para controlar el reproductor web.")
    @app_commands.default_permissions(manage_roles=True)
    async def set_dj_role_slash(self, interaction: discord.Interaction, rol: discord.Role):
        await self.config.guild(interaction.guild).dj_role_id.set(rol.id)
        await interaction.response.send_message(f"✅ El rol {rol.mention} ha sido configurado como DJ para el reproductor web en este servidor.", ephemeral=True)

    @app_commands.command(name="prioridad", description="Configura el rol que tiene prioridad en el reproductor web.")
    @app_commands.default_permissions(manage_roles=True)
    async def set_priority_role_slash(self, interaction: discord.Interaction, rol: discord.Role):
        await self.config.guild(interaction.guild).priority_role_id.set(rol.id)
        await interaction.response.send_message(f"✅ El rol {rol.mention} ha sido configurado con Prioridad para el reproductor web en este servidor.", ephemeral=True)

    @app_commands.command(name="prefix", description="Configura el prefijo del bot para comandos de texto.")
    @app_commands.default_permissions(manage_guild=True)
    async def set_prefix_slash(self, interaction: discord.Interaction, prefijo: str):
        try:
            if interaction.guild:
                await self.bot.set_prefixes(guild=interaction.guild, prefixes=[prefijo])
            else:
                await self.bot.set_prefixes(prefixes=[prefijo])
            await interaction.response.send_message(f"✅ Prefijo del bot actualizado a: `{prefijo}`", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Error al intentar cambiar el prefijo vía slash command. Por favor usa el comando nativo de RedBot: `@Radio CriZpy set prefix {prefijo}`", ephemeral=True)

