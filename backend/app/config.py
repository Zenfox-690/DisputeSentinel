from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List


class Settings(BaseSettings):
    PAYPAL_ENV: str = "sandbox"
    PAYPAL_CLIENT_ID: str = ""
    PAYPAL_CLIENT_SECRET: str = ""
    PAYPAL_WEBHOOK_ID: str = ""

    # AI Model Settings
    AI_PROVIDER: str = "gemini"  # "gemini" or "groq"
    GEMINI_API_KEY: str = ""
    GROQ_API_KEY: str = ""

    # Infrastructure & Policies
    PORT: int = 8000
    DATABASE_URL: str = "sqlite+aiosqlite:///./disputesentinel.db"
    CORS_ORIGINS: str = "http://localhost:3000,http://127.0.0.1:3000"
    CONFIDENCE_THRESHOLD: float = 0.85
    MAX_AUTO_SUBMIT_AMOUNT: float = 500.00
    AUTH_SECRET_KEY: str = "dev-disputesentinel-hmac-secret-key-32b"

    @property
    def paypal_base_url(self) -> str:
        if self.PAYPAL_ENV.lower() == "production":
            return "https://api-m.paypal.com"
        return "https://api-m.sandbox.paypal.com"

    @property
    def cors_origin_list(self) -> List[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
