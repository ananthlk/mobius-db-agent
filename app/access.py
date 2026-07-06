"""YAML-based access control: per-service read/write permissions on tables."""
import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


@dataclass
class ServiceLimits:
    max_rows: int = 5000
    timeout_seconds: int = 15


@dataclass
class ServiceManifest:
    service: str
    permissions: dict[str, dict[str, list[str]]]  # {db_name: {read: [...], write: [...]}}
    limits: ServiceLimits = field(default_factory=ServiceLimits)


class AccessControl:
    """Load YAML manifests and check read/write permissions per caller."""

    def __init__(self, manifests_dir: Path, allow_admin: bool = False) -> None:
        self._manifests: dict[str, ServiceManifest] = {}
        self._allow_admin = allow_admin
        self._load(manifests_dir)

    def _load(self, manifests_dir: Path) -> None:
        if not manifests_dir.is_dir():
            logger.warning("Manifests directory not found: %s", manifests_dir)
            return
        for path in sorted(manifests_dir.glob("*.yml")):
            try:
                data = yaml.safe_load(path.read_text())
                if not data or "service" not in data:
                    continue
                perms = data.get("permissions", {})
                limits_raw = data.get("limits", {})
                limits = ServiceLimits(
                    max_rows=limits_raw.get("max_rows", 5000),
                    timeout_seconds=limits_raw.get("timeout_seconds", 15),
                )
                manifest = ServiceManifest(
                    service=data["service"],
                    permissions=perms,
                    limits=limits,
                )
                self._manifests[manifest.service] = manifest
                logger.info("Loaded manifest: %s (%s)", manifest.service, path.name)
            except Exception as exc:
                logger.warning("Failed to load manifest %s: %s", path, exc)

    def _is_admin(self, caller_id: str) -> bool:
        return self._allow_admin and caller_id == "_admin"

    def check_read(self, caller_id: str, db_name: str, table: str) -> bool:
        if self._is_admin(caller_id):
            return True
        manifest = self._manifests.get(caller_id)
        if not manifest:
            return False
        db_perms = manifest.permissions.get(db_name, {})
        read_tables = db_perms.get("read", [])
        return "*" in read_tables or table in read_tables

    def check_write(self, caller_id: str, db_name: str, table: str) -> bool:
        if self._is_admin(caller_id):
            return True
        manifest = self._manifests.get(caller_id)
        if not manifest:
            return False
        db_perms = manifest.permissions.get(db_name, {})
        write_tables = db_perms.get("write", [])
        return "*" in write_tables or table in write_tables

    def get_limits(self, caller_id: str) -> ServiceLimits:
        if self._is_admin(caller_id):
            return ServiceLimits(max_rows=100000, timeout_seconds=120)
        manifest = self._manifests.get(caller_id)
        if not manifest:
            return ServiceLimits()
        return manifest.limits

    def known_callers(self) -> list[str]:
        return list(self._manifests.keys())
