from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# config.py 位于 backend/app/，仓库根目录的 .env 要再往上两级。
# 改 .env 后需重启 API 进程。
ROOT_ENV = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    """进程启动时读取一次。密钥留空时服务仍可启动，此时不会去调用模型。"""

    model_config = SettingsConfigDict(env_file=ROOT_ENV, extra="ignore")

    database_url: str = "postgresql://eduagent:eduagent@localhost:5432/eduagent"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    # 讲题用主模型，抽取用较小模型；第 1 天只把名字暴露出来，尚未发起调用。
    tutor_model: str = "qwen3.8-max-0902"
    extractor_model: str = "qwen3.8-max-0902"


@lru_cache
def get_settings() -> Settings:
    """缓存配置，避免每次请求都重新读 .env。"""
    return Settings()
