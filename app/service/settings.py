"""All configuration in one place. Values come from .env / environment variables."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    # LLM
    llm_provider: str = "openai"
    llm_temperature: float = 0.1
    llm_max_tokens: int = 2000
    llm_timeout_seconds: int = 120
    llm_max_retries: int = 2
    ollama_max_retries: int = 0
    ollama_max_concurrent_requests: int = 2
    # Context window requested from the local server per call. Ollama's default is only 4096, which is too small for
    # the agent prompts + their JSON answer. 0 disables sending it (then rely on OLLAMA_CONTEXT_LENGTH or a Modelfile).
    ollama_num_ctx: int = 8192
    # Local models (Ollama/OLMX) emit far fewer malformed JSON replies when the server is told to
    # return a JSON object; this removes most validation re-asks. Does not change OpenAI/stub behaviour.
    llm_json_object: bool = True
    llm_force_provider: str = ""  # overrides provider in model_config.json for a run
    llm_force_model: str = ""  # optional model ID override for a run
    openai_api_key: str = ""
    ollama_base_url: str = "http://localhost:11434"
    olmx_base_url: str = "http://127.0.0.1:8008/v1"
    olmx_api_key: str = ""

    # Market data
    fyers_app_id: str = ""
    fyers_access_token: str = ""
    fyers_secret_id: str = ""      # FYERS app "Secret ID" (needed only to generate the daily token via scripts.fyers_login)
    fyers_redirect_uri: str = ""   # must exactly match the redirect URI registered on your FYERS app
    fred_api_key: str = ""
    nse_cookies: str = ""
    nse_user_agent: str = ""

    # App
    universe_file: str = "app/config/universe.csv"
    mcp_mode: str = "stdio"  # stdio | inprocess
    max_concurrent_stocks: int = 3
    output_dir: str = "data/output"
    events_csv: str = "data/events.csv"
    db_path: str = "data/app.db"  # SQLite: fundamentals, news, events, FII/DII, caches, run I/O
    force_refresh: bool = False  # True = ignore the "refreshed today" gate and re-fetch everything
    log_level: str = "INFO"
    log_dir: str = "logs"
    place_order: bool = False  # informational: nothing in this app can place an order

    def path(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else ROOT / p


@lru_cache
def get_settings() -> Settings:
    return Settings()
