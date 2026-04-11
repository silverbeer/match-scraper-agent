"""Environment-based configuration for match-scraper-agent."""

from __future__ import annotations

from pydantic_settings import BaseSettings

from pathlib import Path

ENVS_DIR = Path(__file__).resolve().parents[2] / "envs"


def env_file_path(env: str) -> Path | None:
    """Resolve the dotenv file for a given environment name.

    Args:
        env: Environment name (e.g. "local", "prod").

    Returns:
        Path to envs/.env.<env>, or None if it doesn't exist (e.g. in a
        container where settings come from environment variables).
    """
    path = ENVS_DIR / f".env.{env}"
    if not path.is_file():
        return None
    return path


class AgentSettings(BaseSettings):
    """Agent configuration loaded from a dotenv file + environment variables.

    Precedence: env vars > dotenv file > defaults.
    Pass _env_file to the constructor to select which env file to load.
    """

    model_config = {"env_prefix": "AGENT_", "extra": "ignore"}

    rabbitmq_url: str = "amqp://guest:guest@localhost:5672/"
    exchange_name: str = "matches-fanout"
    queue_name: str = ""
    league: str = "Homegrown"
    age_group: str = "U14"
    division: str = "Northeast"
    missing_table_api_url: str = "http://localhost:8000"
    missing_table_api_key: str = ""
    dry_run: bool = False
    headless: bool = True
    json_logs: bool = False
    log_level: str = "info"

    # Run journal (cross-run memory) — stored in S3
    journal_s3_bucket: str = ""
    journal_s3_key: str = "journal/latest.json"

    # Telegram notifications (run summary report)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Resend email fallback (used when Telegram fails)
    resend_api_key: str = ""
    alert_email_from: str = "contact@missingtable.com"
    alert_email_to: str = "silverbeer.io@gmail.com"

    # Audit settings
    audit_season_start: str = "2026-02-01"  # Spring segment start date

    # Database (missing-table Supabase — used by trigger.sh post-run verification)
    db_host: str = "127.0.0.1"
    db_port: int = 54332
    db_user: str = "postgres"
    db_password: str = "postgres"
    db_name: str = "postgres"
