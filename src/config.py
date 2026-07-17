import os

import yaml
from dotenv import load_dotenv

from src.logger import get_logger

# Import here to avoid circular dependency; Settings doesn't depend on ranker internals
def _get_scoring_weights_class():
    from src.pipeline.ranker import ScoringWeights
    return ScoringWeights

load_dotenv()

logger = get_logger(__name__)

_REQUIRED_ENV_VARS = [
    "DISCORD_BOT_TOKEN",
    "DISCORD_GUILD_ID",
]

_OPTIONAL_ENV_VARS: list[str] = []

_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config",
    "settings.yaml",
)
_ALIASES_PATH = os.path.join(os.path.dirname(_CONFIG_PATH), "aliases.yaml")


class Settings:
    def __init__(self, data: dict):
        self._data = data

    # --- Catalog API ---

    @property
    def catalog_user_url(self) -> str:
        return self._data.get("catalog", {}).get("user_url", "")

    # --- Discord ---

    @property
    def discord_bot_token(self) -> str:
        return os.getenv("DISCORD_BOT_TOKEN", "")

    @property
    def discord_guild_id(self) -> str:
        return os.getenv("DISCORD_GUILD_ID", self._data.get("discord", {}).get("guild_id", ""))

    @property
    def discord_report_channel(self) -> str:
        return self._data.get("discord", {}).get("report_channel", "music-research")

    @property
    def discord_log_channel(self) -> str:
        return self._data.get("discord", {}).get("log_channel", "logs")

    @property
    def discord_alert_channel(self) -> str:
        return self._data.get("discord", {}).get("alert_channel", "alerts")

    @property
    def discord_mix_prep_channel(self) -> str:
        return self._data.get("discord", {}).get("mix_prep_channel", "mix-prep")

    # --- Beatport ---

    @property
    def beatport_username(self) -> str:
        return os.getenv("BEATPORT_USERNAME", "")

    @property
    def beatport_password(self) -> str:
        return os.getenv("BEATPORT_PASSWORD", "")

    # --- SoundCloud ---

    @property
    def soundcloud_client_id(self) -> str:
        return os.getenv("SOUNDCLOUD_CLIENT_ID", "")

    @property
    def soundcloud_client_secret(self) -> str:
        return os.getenv("SOUNDCLOUD_CLIENT_SECRET", "")

    # --- Sources ---

    def source_enabled(self, source_name: str) -> bool:
        return self._data.get("sources", {}).get(source_name, {}).get("enabled", False)

    def get_source_config(self, source_name: str) -> dict:
        return self._data.get("sources", {}).get(source_name, {})

    # --- Pipeline ---

    @property
    def pipeline_top_picks_count(self) -> int:
        return self._data.get("pipeline", {}).get("top_picks_count", 5)

    @property
    def pipeline_label_watch_count(self) -> int:
        return self._data.get("pipeline", {}).get("label_watch_count", 5)

    @property
    def pipeline_artist_watch_count(self) -> int:
        return self._data.get("pipeline", {}).get("artist_watch_count", 5)

    @property
    def pipeline_wildcard_count(self) -> int:
        return self._data.get("pipeline", {}).get("wildcard_count", 3)

    @property
    def pipeline_mix_prep_top_picks_count(self) -> int:
        return self._data.get("pipeline", {}).get("mix_prep_top_picks_count", 20)

    @property
    def pipeline_mix_prep_deep_cuts_count(self) -> int:
        return self._data.get("pipeline", {}).get("mix_prep_deep_cuts_count", 20)

    @property
    def pipeline_release_date_window_days(self) -> int | None:
        return self._data.get("pipeline", {}).get("release_date_window_days")

    @property
    def pipeline_section_min_score(self) -> float:
        return float(self._data.get("pipeline", {}).get("section_min_score", 0.0))

    @property
    def pipeline_genre_exclusions(self) -> dict[str, list[str]]:
        return self._data.get("pipeline", {}).get("genre_exclusions", {})

    @property
    def pipeline_remix_aware_identity(self) -> bool:
        """Whether named remixes get a distinct track identity (issue #9).

        Default False — flag-off behaviour is byte-identical to the legacy
        make_dedup_key. Enable only after a home validation run (rebuild
        known_tracks.json, then a dry-run diff that shows no owned tracks
        resurfacing). See issue #9.
        """
        return bool(self._data.get("pipeline", {}).get("remix_aware_identity", False))

    # --- Alerts ---

    @property
    def alerts_source_drop_threshold_pct(self) -> int:
        return int(self._data.get("alerts", {}).get("source_drop_threshold_pct", 50))

    @property
    def alerts_min_history_runs(self) -> int:
        return int(self._data.get("alerts", {}).get("min_history_runs", 2))

    # --- Data ---

    @property
    def data_dir(self) -> str:
        return self._data.get("data_dir", "data")

    # --- Audition server ---

    @property
    def audition_base_url(self) -> str:
        return os.getenv("TUNEFINDER_AUDITION_BASE_URL", "").rstrip("/")

    # --- Web service (src/web) ---

    @property
    def web_api_secret(self) -> str:
        """Bearer secret required on every /api call (except /api/health)."""
        return os.getenv("TUNEFINDER_API_SECRET", "")

    @property
    def web_insecure(self) -> bool:
        """Explicit opt-out of auth for LAN-only use — TUNEFINDER_WEB_INSECURE=1."""
        return os.getenv("TUNEFINDER_WEB_INSECURE", "") == "1"

    @property
    def web_allowed_origins(self) -> list[str]:
        """CORS origins for the SPA. Env (comma-separated) overrides YAML.

        Empty by default — the zero-CORS paths (static mount, same-origin
        reverse proxy) need nothing here.
        """
        env_val = os.getenv("TUNEFINDER_WEB_ALLOWED_ORIGINS", "")
        if env_val:
            return [o.strip() for o in env_val.split(",") if o.strip()]
        origins = self._data.get("web", {}).get("allowed_origins", [])
        return [str(o) for o in origins] if isinstance(origins, list) else []

    @property
    def web_base_url(self) -> str:
        """Public URL of the web app — when set, Discord reports link to it
        (superseding the audition-page link)."""
        return os.getenv("TUNEFINDER_WEB_BASE_URL", "").rstrip("/")

    @property
    def web_static_dir(self) -> str:
        """Optional built-SPA directory served by `tunefinder serve` (zero-CORS LAN mode)."""
        return os.getenv("TUNEFINDER_WEB_STATIC_DIR", "")

    # --- Testing ---

    @property
    def testing_use_fixtures(self) -> bool:
        return self._data.get("testing", {}).get("use_fixtures", False)

    @property
    def testing_fixtures_dir(self) -> str:
        return self._data.get("testing", {}).get("fixtures_dir", "fixtures")

    # --- Scoring weights ---

    def scoring_weights(self):
        """Build ScoringWeights from config, using defaults for missing keys."""
        ScoringWeights = _get_scoring_weights_class()
        scoring_config = self._data.get("scoring", {})

        # Ignore unknown keys with a logged warning
        known_fields = {f.name for f in ScoringWeights.__dataclass_fields__.values()}
        unknown_keys = set(scoring_config.keys()) - known_fields
        if unknown_keys:
            logger.warning(f"[config] Unknown scoring keys ignored: {', '.join(sorted(unknown_keys))}")

        # Build kwargs, filtering out unknown keys
        kwargs = {k: v for k, v in scoring_config.items() if k in known_fields}
        return ScoringWeights(**kwargs)

    # --- Artist aliases ---

    def artist_aliases(self) -> dict[str, str]:
        """Load config/aliases.yaml and invert to {alias_lower: canonical_lower}.

        Format: `canonical_name: [alias1, alias2, ...]`. A missing file or
        empty/commented-only content is the expected default state — returns
        {} with no warning. A malformed file (not a mapping of str -> list)
        logs a warning and also returns {} — never raises.
        """
        if not os.path.exists(_ALIASES_PATH):
            return {}
        try:
            with open(_ALIASES_PATH, "r") as f:
                data = yaml.safe_load(f)
            if not data:
                return {}
            if not isinstance(data, dict):
                raise ValueError(f"expected a mapping at top level, got {type(data).__name__}")

            aliases: dict[str, str] = {}
            for canonical, alias_list in data.items():
                if not isinstance(alias_list, list):
                    raise ValueError(f"aliases for {canonical!r} must be a list")
                for alias in alias_list:
                    aliases[str(alias).lower().strip()] = str(canonical).lower().strip()
            return aliases
        except Exception as exc:
            logger.warning(f"[config] Malformed aliases file {_ALIASES_PATH}: {exc}")
            return {}

    # --- Validation ---

    def validate(self) -> None:
        missing = [key for key in _REQUIRED_ENV_VARS if not os.getenv(key)]
        if missing:
            raise EnvironmentError(
                f"Missing required environment variables: {', '.join(missing)}\n"
                "Copy .env.example to .env and fill in the values."
            )

        for key in _OPTIONAL_ENV_VARS:
            if not os.getenv(key):
                logger.warning(
                    f"[config] Optional env var not set: {key} "
                    "(configured provider will be skipped)"
                )

        beatport = self._data.get("sources", {}).get("beatport", {})
        if beatport.get("enabled") and not (self.beatport_username and self.beatport_password):
            logger.warning(
                "[config] Beatport is enabled but BEATPORT_USERNAME/BEATPORT_PASSWORD "
                "are not both set — the source will report a failure until they are set."
            )

        soundcloud = self._data.get("sources", {}).get("soundcloud", {})
        if soundcloud.get("enabled") and not (self.soundcloud_client_id and self.soundcloud_client_secret):
            logger.warning(
                "[config] SoundCloud is enabled but SOUNDCLOUD_CLIENT_ID/SOUNDCLOUD_CLIENT_SECRET "
                "are not both set — the source will report a failure until they are set."
            )

        logger.info("[config] Validated — all required environment variables present.")


def load_settings() -> Settings:
    if not os.path.exists(_CONFIG_PATH):
        raise FileNotFoundError(f"Settings file not found: {_CONFIG_PATH}")
    with open(_CONFIG_PATH, "r") as f:
        data = yaml.safe_load(f) or {}
    return Settings(data)
