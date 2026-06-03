import json
import os
from dataclasses import dataclass
from dotenv import load_dotenv


@dataclass
class SearchConfig:
    roles: list[str]
    location: str
    time_range: str
    work_mode: list[str] = None

    def __post_init__(self):
        if self.work_mode is None:
            self.work_mode = []


@dataclass
class ScoringConfig:
    threshold: int
    exclude_keywords: list[str]
    priority_keywords: list[str]
    candidate_profile: str


@dataclass
class TelegramConfig:
    greeting: str


@dataclass
class AppConfig:
    search: SearchConfig
    scoring: ScoringConfig
    telegram: TelegramConfig
    groq_api_key: str
    telegram_token: str
    telegram_chat_id: str
    obsidian_vault_path: str
    dedup_log_path: str


def load_config(config_path: str = "config/config.json") -> AppConfig:
    load_dotenv()
    with open(config_path) as f:
        data = json.load(f)
    return AppConfig(
        search=SearchConfig(**data["search"]),
        scoring=ScoringConfig(**data["scoring"]),
        telegram=TelegramConfig(**data["telegram"]),
        groq_api_key=os.environ["GROQ_API_KEY"],
        telegram_token=os.environ["TELEGRAM_TOKEN"],
        telegram_chat_id=os.environ["TELEGRAM_CHAT_ID"],
        obsidian_vault_path=os.environ["OBSIDIAN_VAULT_PATH"],
        dedup_log_path=os.environ["DEDUP_LOG_PATH"],
    )
