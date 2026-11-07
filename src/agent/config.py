"""Runtime settings, read from environment variables (.env)."""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"


class ConfigError(Exception):
    """Raised when required settings are missing."""


@dataclass(frozen=True)
class Settings:
    """All runtime settings. Only GOOGLE_API_KEY and GCP_PROJECT are required."""

    gcp_project: str
    model: str
    fallback_model: str
    llm_timeout_s: float
    fallback_timeout_s: float
    classifier_timeout_s: float
    query_timeout_s: float
    max_query_bytes: int
    max_result_rows: int
    db_path: Path
    log_dir: Path
    golden_dir: Path
    llm_min_interval_s: float
    log_level: str
    customer_key_salt: str | None
    llm_usd_per_million_tokens: float
    bigquery_usd_per_tib: float

    @classmethod
    def from_env(cls) -> "Settings":
        """Read settings from the environment and .env, failing clearly if any are missing."""
        load_dotenv(ROOT / ".env")
        missing = [v for v in ("GOOGLE_API_KEY", "GCP_PROJECT") if not os.getenv(v)]
        if missing:
            raise ConfigError(
                f"Missing {', '.join(missing)}. Copy .env.example to .env and fill it in."
            )
        # google-auth warns when it can't infer a project; we always pass one explicitly.
        os.environ.setdefault("GOOGLE_CLOUD_PROJECT", os.environ["GCP_PROJECT"])
        return cls(
            gcp_project=os.environ["GCP_PROJECT"],
            model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash"),
            fallback_model=os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash-lite"),
            llm_timeout_s=float(os.getenv("LLM_TIMEOUT_S", "30")),
            fallback_timeout_s=float(os.getenv("FALLBACK_TIMEOUT_S", "60")),
            classifier_timeout_s=float(os.getenv("CLASSIFIER_TIMEOUT_S", "10")),
            query_timeout_s=float(os.getenv("QUERY_TIMEOUT_S", "30")),
            max_query_bytes=int(os.getenv("MAX_QUERY_BYTES", str(10**9))),
            max_result_rows=int(os.getenv("MAX_RESULT_ROWS", "50")),
            db_path=Path(os.getenv("APP_DB_PATH", ROOT / "data" / "app.db")),
            log_dir=Path(os.getenv("LOG_DIR", ROOT / "logs")),
            golden_dir=Path(os.getenv("GOLDEN_DIR", ROOT / "data" / "golden_trios")),
            llm_min_interval_s=float(os.getenv("LLM_MIN_INTERVAL_S", "4")),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            customer_key_salt=os.getenv("CUSTOMER_KEY_SALT") or None,
            llm_usd_per_million_tokens=float(os.getenv("LLM_USD_PER_MILLION_TOKENS", "0.50")),
            bigquery_usd_per_tib=float(os.getenv("BIGQUERY_USD_PER_TIB", "6.25")),
        )
