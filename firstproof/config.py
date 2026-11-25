"""Server-only deployment configuration. Never expose credentials to templates or logs."""
from functools import lru_cache
from urllib.parse import urlsplit
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Explicit settings; the safe default is an isolated local replay, not a live experiment."""
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')
    mode: str = 'local'  # local = replay only; live = actual Flagsmith operations
    database_url: str = 'sqlite:///./firstproof.db'
    operator_token: SecretStr = SecretStr('')
    public_origin: str = 'http://localhost:8000'
    secure_cookies: bool = False
    flagsmith_environment_key: SecretStr = SecretStr('')
    flagsmith_release_environment_key: SecretStr = SecretStr('')
    flagsmith_admin_key: SecretStr = SecretStr('')
    flagsmith_api_url: str = 'https://api.flagsmith.com/api/v1/'
    flagsmith_events_url: str = ''
    flagsmith_dashboard_url: str = 'https://app.flagsmith.com'
    flagsmith_project_id: int = 0
    flagsmith_feature_id: int = 0
    flagsmith_candidate_id: int = 0
    flagsmith_flag: str = 'firstproof_retry_policy'
    flagsmith_release_flag: str = 'firstproof_retry_policy'
    # Explicit native feature state ID for a dedicated sandbox release environment.
    flagsmith_release_state_id: int = 0
    serpapi_key: SecretStr = SecretStr('')
    openrouter_api_key: SecretStr = SecretStr('')
    openrouter_model: str = 'openai/gpt-oss-120b'
    faultlab_url: str = 'http://127.0.0.1:8001'
    faultlab_token: SecretStr = SecretStr('local-faultlab-only')
    max_jobs: int = Field(8, ge=1, le=100)
    workload_concurrency: int = Field(12, ge=1, le=64)
    max_workloads: int = Field(5000, ge=100, le=10000)
    search_budget: int = Field(12, ge=2, le=30)
    session_search_limit: int = Field(100, ge=1, le=10000)

    @model_validator(mode='after')
    def safety(self):
        """Refuse unsafe live deployment instead of silently entering mock mode."""
        if self.mode not in {'local', 'live'}:
            raise ValueError('MODE must be local or live')
        if self.mode == 'live':
            origin = urlsplit(self.public_origin)
            if origin.hostname not in {'localhost','127.0.0.1','::1'} and (origin.scheme != 'https' or not self.secure_cookies):
                raise ValueError('Public live deployments require HTTPS and SECURE_COOKIES=true')
            if len(self.faultlab_token.get_secret_value()) < 32:
                raise ValueError('Live mode requires a randomly generated FaultLab token')
            if not self.database_url.startswith('postgresql+psycopg://'):
                raise ValueError('Live mode requires PostgreSQL with psycopg')
            if len(self.operator_token.get_secret_value()) < 32:
                raise ValueError('Set a random operator token of at least 32 characters')
            if not all((self.flagsmith_environment_key.get_secret_value(),
                        self.flagsmith_admin_key.get_secret_value(), self.flagsmith_feature_id,
                        self.flagsmith_candidate_id, self.flagsmith_project_id)):
                raise ValueError('Live mode requires a dedicated Flagsmith environment and keyed flag IDs')
            if self.flagsmith_release_environment_key.get_secret_value() == self.flagsmith_environment_key.get_secret_value():
                raise ValueError('Release and experiment environments must differ')
        return self


@lru_cache
def settings() -> Settings:
    """Cache immutable configuration for the process lifetime."""
    return Settings()
