import os

import yaml
from dotenv import load_dotenv

from src.logger import get_logger

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

    # --- Testing ---

    @property
    def testing_use_fixtures(self) -> bool:
        return self._data.get("testing", {}).get("use_fixtures", False)

    @property
    def testing_fixtures_dir(self) -> str:
        return self._data.get("testing", {}).get("fixtures_dir", "fixtures")

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

        logger.info("[config] Validated — all required environment variables present.")


def load_settings() -> Settings:
    if not os.path.exists(_CONFIG_PATH):
        raise FileNotFoundError(f"Settings file not found: {_CONFIG_PATH}")
    with open(_CONFIG_PATH, "r") as f:
        data = yaml.safe_load(f) or {}
    return Settings(data)
