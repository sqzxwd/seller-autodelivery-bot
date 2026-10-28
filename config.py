import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    api_key: str
    api_url: str
    database_path: Path
    poll_interval: float
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    @classmethod
    def from_env(cls, env_path: Path | str = ".env") -> Settings:
        env_file = Path(env_path)
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())

        return cls(
            api_key=os.getenv("MARKETPLACE_API_KEY", ""),
            api_url=os.getenv("MARKETPLACE_API_URL", "https://api.marketplace.local/v1"),
            database_path=Path(os.getenv("DATABASE_PATH", "keys.db")),
            poll_interval=float(os.getenv("POLL_INTERVAL_SECONDS", "5.0")),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        )
