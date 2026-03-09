import os

import yaml
from dotenv import load_dotenv

from src.logger import get_logger

load_dotenv()

logger = get_logger(__name__)

_REQUIRED_ENV_VARS = [
    "ANTHROPIC_API_KEY",
    "DISCORD_BOT_TOKEN",
    "DISCORD_GUILD_ID",
    "MISTRAL_API_KEY",
]

_OPTIONAL_ENV_VARS = [
    "GROQ_API_KEY",
    "GEMINI_API_KEY",
    "MINIMAX_API_KEY",
    "OPENROUTER_API_KEY",
]

# Maps provider name → environment variable name (for cascade config check)
PROVIDER_ENV_VAR = {
    "mistral": "MISTRAL_API_KEY",
    "groq": "GROQ_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "minimax": "MINIMAX_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "ollama": None,
}

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

    # --- LLM ---

    @property
    def mistral_api_key(self) -> str:
        return os.getenv("MISTRAL_API_KEY", "")

    @property
    def groq_api_key(self) -> str:
        return os.getenv("GROQ_API_KEY", "")

    @property
    def gemini_api_key(self) -> str:
        return os.getenv("GEMINI_API_KEY", "")

    @property
    def minimax_api_key(self) -> str:
        return os.getenv("MINIMAX_API_KEY", "")

    @property
    def openrouter_api_key(self) -> str:
        return os.getenv("OPENROUTER_API_KEY", "")

    @property
    def anthropic_api_key(self) -> str:
        return os.getenv("ANTHROPIC_API_KEY", "")

    @property
    def llm_stage1(self) -> dict:
        return self._data.get("llm", {}).get("stage1", {})

    @property
    def llm_stage2(self) -> dict:
        return self._data.get("llm", {}).get("stage2", {})

    @property
    def llm_fallback_chain(self) -> list[dict]:
        return self._data.get("llm", {}).get("fallback_chain", [])

    # --- Sources ---

    def source_enabled(self, source_name: str) -> bool:
        return self._data.get("sources", {}).get(source_name, {}).get("enabled", False)

    def get_source_config(self, source_name: str) -> dict:
        return self._data.get("sources", {}).get(source_name, {})

    # --- Pipeline ---

    @property
    def pipeline_max_candidates(self) -> int:
        return self._data.get("pipeline", {}).get("max_candidates", 100)

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
                    "(Stage 1 fallback chain will skip this provider)"
                )

        logger.info("[config] Validated — all required environment variables present.")


def load_settings() -> Settings:
    if not os.path.exists(_CONFIG_PATH):
        raise FileNotFoundError(f"Settings file not found: {_CONFIG_PATH}")
    with open(_CONFIG_PATH, "r") as f:
        data = yaml.safe_load(f) or {}
    return Settings(data)
