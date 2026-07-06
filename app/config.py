"""Environment configuration and DB URL resolution for mobius-db-agent."""
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# .env loading (same pattern as mobius-chat/app/chat_config.py)
# ---------------------------------------------------------------------------

def _load_dotenv() -> None:
    """Best-effort load of .env files (module-local, then mobius-config)."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    module_dir = Path(__file__).resolve().parent.parent
    repo_root = module_dir.parent
    for env_path in [
        module_dir / ".env",
        repo_root / "mobius-config" / ".env",
        repo_root / ".env",
    ]:
        if env_path.is_file():
            load_dotenv(env_path, override=False)

_load_dotenv()

# ---------------------------------------------------------------------------
# URL normalisation
# ---------------------------------------------------------------------------

_CLOUD_SQL_IP = "34.135.72.145"
_PROXY_HOST = "127.0.0.1"
_PROXY_PORT = "5433"


def _use_cloud_sql_proxy() -> bool:
    return os.environ.get("USE_CLOUD_SQL_PROXY", "").strip().lower() in ("1", "true", "yes")


def normalise_pg_url(url: str) -> str:
    """Normalise a PostgreSQL URL for sync psycopg2 access.

    1. Strip async driver suffixes (+asyncpg, +psycopg).
    2. Rewrite Cloud SQL IP to localhost proxy when USE_CLOUD_SQL_PROXY is set.
    3. Ensure connect_timeout=10 is present.
    """
    if not url:
        return url
    # Strip async driver suffixes
    url = re.sub(r"postgresql\+\w+://", "postgresql://", url)
    # Cloud SQL proxy rewrite
    if _use_cloud_sql_proxy() and _CLOUD_SQL_IP in url:
        url = url.replace(f"{_CLOUD_SQL_IP}:5432", f"{_PROXY_HOST}:{_PROXY_PORT}")
        url = url.replace(_CLOUD_SQL_IP, f"{_PROXY_HOST}:{_PROXY_PORT}")
    # Ensure connect_timeout
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if "connect_timeout" not in qs:
        qs["connect_timeout"] = ["10"]
    new_query = urlencode(qs, doseq=True)
    url = urlunparse(parsed._replace(query=new_query))
    return url


def _derive_url(base_url: str, db_name: str) -> str:
    """Derive a DB URL from a base URL by swapping the database name."""
    if not base_url:
        return ""
    parsed = urlparse(base_url)
    new_path = f"/{db_name}"
    return urlunparse(parsed._replace(path=new_path))


# ---------------------------------------------------------------------------
# Pool configuration
# ---------------------------------------------------------------------------

_POOL_DEFAULTS = {
    "chat": 5,
    "rag": 3,
    "user": 3,
    "qa": 2,
}


@dataclass
class PoolConfig:
    min_size: int = 1
    max_size: int = 5
    pool_recycle: int = 300
    pool_pre_ping: bool = True


@dataclass
class DbAgentConfig:
    """Resolved configuration for all databases."""
    db_urls: dict[str, str] = field(default_factory=dict)
    pool_configs: dict[str, PoolConfig] = field(default_factory=dict)
    pool_total_max: int = 15
    manifests_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent / "manifests")
    dbt_project_root: Path | None = None
    allow_admin: bool = False

    @classmethod
    def from_env(cls) -> "DbAgentConfig":
        # Resolve base URL (most services derive from CHAT_RAG_DATABASE_URL)
        chat_url = (
            os.environ.get("DB_AGENT_CHAT_URL")
            or os.environ.get("CHAT_RAG_DATABASE_URL")
            or ""
        ).strip()

        rag_url = (
            os.environ.get("DB_AGENT_RAG_URL")
            or os.environ.get("DATABASE_URL")
            or ""
        ).strip()

        user_url = (
            os.environ.get("DB_AGENT_USER_URL")
            or os.environ.get("USER_DATABASE_URL")
            or _derive_url(chat_url, "mobius_user")
        ).strip()

        qa_url = (
            os.environ.get("DB_AGENT_QA_URL")
            or os.environ.get("QA_DATABASE_URL")
            or _derive_url(chat_url, "mobius_qa")
        ).strip()

        db_urls = {}
        for name, url in [("chat", chat_url), ("rag", rag_url), ("user", user_url), ("qa", qa_url)]:
            normalised = normalise_pg_url(url)
            if normalised:
                db_urls[name] = normalised

        # Pool configs
        pool_configs = {}
        total_max = int(os.environ.get("DB_AGENT_POOL_TOTAL_MAX", "15"))
        for name in ("chat", "rag", "user", "qa"):
            max_size = int(os.environ.get(f"DB_AGENT_POOL_{name.upper()}_MAX", str(_POOL_DEFAULTS.get(name, 3))))
            pool_configs[name] = PoolConfig(min_size=1, max_size=max_size)

        # dbt project root
        module_dir = Path(__file__).resolve().parent.parent
        repo_root = module_dir.parent
        dbt_root = repo_root / "mobius-dbt"
        dbt_project_root = dbt_root if dbt_root.is_dir() else None

        allow_admin = os.environ.get("DB_AGENT_ALLOW_ADMIN", "").strip().lower() in ("1", "true", "yes")

        return cls(
            db_urls=db_urls,
            pool_configs=pool_configs,
            pool_total_max=total_max,
            dbt_project_root=dbt_project_root,
            allow_admin=allow_admin,
        )
