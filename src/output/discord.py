"""
Discord output — plain requests, Bot token auth, no SDK.

Auth:    Authorization: Bot {token}
API:     https://discord.com/api/v10
Limits:  2000 chars per message — chunked at newline boundaries
Rate:    HTTP 429 handled by sleeping retry_after + 0.5s and retrying

If DISCORD_BOT_TOKEN is not set, all methods log a warning and return False
silently — never raise. This allows the pipeline to run without Discord
configured during development.
"""
import time

import requests

from src.logger import get_logger

logger = get_logger(__name__)

_API_BASE = "https://discord.com/api/v10"
_CHUNK_SIZE = 2000
_CHUNK_SLEEP = 0.5
_RATE_LIMIT_BUFFER = 0.5  # extra sleep on top of retry_after


class DiscordClient:
    def __init__(self, bot_token: str, guild_id: str):
        self._token = bot_token
        self._guild_id = guild_id
        self._channel_cache: dict[str, str] = {}
        self._headers = {
            "Authorization": f"Bot {bot_token}",
            "Content-Type": "application/json",
        }

    # ---------------------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------------------

    def _resolve_channel(self, channel_name: str) -> str:
        if channel_name in self._channel_cache:
            return self._channel_cache[channel_name]

        url = f"{_API_BASE}/guilds/{self._guild_id}/channels"
        resp = requests.get(url, headers=self._headers, timeout=10)
        resp.raise_for_status()

        for ch in resp.json():
            self._channel_cache[ch["name"]] = ch["id"]

        if channel_name not in self._channel_cache:
            raise ValueError(f"Channel '{channel_name}' not found in guild {self._guild_id}")

        return self._channel_cache[channel_name]

    def _post_raw(self, channel_id: str, content: str) -> None:
        url = f"{_API_BASE}/channels/{channel_id}/messages"
        while True:
            resp = requests.post(
                url,
                headers=self._headers,
                json={"content": content},
                timeout=10,
            )
            if resp.status_code == 429:
                retry_after = float(resp.json().get("retry_after", 1))
                sleep_for = retry_after + _RATE_LIMIT_BUFFER
                logger.warning(f"[discord] Rate limited — sleeping {sleep_for:.1f}s")
                time.sleep(sleep_for)
                continue
            resp.raise_for_status()
            break

    def _chunk_text(self, text: str) -> list[str]:
        if len(text) <= _CHUNK_SIZE:
            return [text]

        chunks = []
        while text:
            if len(text) <= _CHUNK_SIZE:
                chunks.append(text)
                break
            split = text.rfind("\n", 0, _CHUNK_SIZE)
            if split == -1:
                split = _CHUNK_SIZE
            chunks.append(text[:split])
            text = text[split:].lstrip("\n")

        return chunks

    # ---------------------------------------------------------------------------
    # Public interface
    # ---------------------------------------------------------------------------

    def post(self, channel_name: str, message: str) -> bool:
        """
        Post a message to the named channel, chunking if over 2000 chars.
        Returns True on success, False on any error.
        """
        if not self._token:
            logger.warning("[discord] No bot token set — skipping post")
            return False
        try:
            channel_id = self._resolve_channel(channel_name)
            chunks = self._chunk_text(message)
            for i, chunk in enumerate(chunks):
                logger.info(f"[discord] Posting chunk {i + 1}/{len(chunks)} to #{channel_name}")
                self._post_raw(channel_id, chunk)
                if i < len(chunks) - 1:
                    time.sleep(_CHUNK_SLEEP)
            return True
        except Exception as e:
            logger.error(f"[discord] Failed to post to #{channel_name}: {e}")
            return False

    def post_report(self, report: str) -> bool:
        """Post the weekly report to the configured report channel."""
        return self.post(self._report_channel, report)

    def post_log(self, message: str) -> bool:
        """Post a run summary line to the configured log channel."""
        return self.post(self._log_channel, message)

    def post_alert(self, message: str) -> bool:
        """Post an alert to the configured alert channel, prefixed with ⚠️ ALERT |"""
        return self.post(self._alert_channel, f"⚠️ ALERT | {message}")

    # Channel name properties — set by make_discord_client()
    _report_channel: str = "music-research"
    _log_channel: str = "logs"
    _alert_channel: str = "alerts"


def make_discord_client(settings) -> DiscordClient:
    client = DiscordClient(
        bot_token=settings.discord_bot_token,
        guild_id=settings.discord_guild_id,
    )
    client._report_channel = settings.discord_report_channel
    client._log_channel = settings.discord_log_channel
    client._alert_channel = settings.discord_alert_channel
    return client
