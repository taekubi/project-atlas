"""Load and validate Project Atlas configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib
from typing import Any


@dataclass(frozen=True)
class AtlasSettings:
    """Atlas platform storage settings."""

    storage_region: str
    bucket: str
    s3_prefix: str
    source_root: str


@dataclass(frozen=True)
class CollectionSettings:
    """CloudWatch metric collection settings."""

    lookback_minutes: int
    period_seconds: int
    metrics: list[str]


@dataclass(frozen=True)
class TargetSettings:
    """AWS account and Region monitored by Atlas."""

    name: str
    account_id: str
    role_name: str
    regions: list[str]
    enabled: bool

    @property
    def role_arn(self) -> str:
        """Return the cross-account observer role ARN."""

        return (
            f"arn:aws:iam::{self.account_id}:"
            f"role/{self.role_name}"
        )


@dataclass(frozen=True)
class AtlasConfig:
    """Complete Project Atlas configuration."""

    atlas: AtlasSettings
    collection: CollectionSettings
    targets: list[TargetSettings]

    @property
    def enabled_targets(self) -> list[TargetSettings]:
        """Return only enabled target accounts."""

        return [
            target
            for target in self.targets
            if target.enabled
        ]


def _required_string(
    data: dict[str, Any],
    key: str,
    section: str,
) -> str:
    """Read and validate a required string."""

    value = data.get(key)

    if not isinstance(value, str):
        raise ValueError(
            f"{section}.{key} must be a string"
        )

    value = value.strip()

    if not value:
        raise ValueError(
            f"{section}.{key} must not be empty"
        )

    return value


def _positive_int(
    data: dict[str, Any],
    key: str,
    section: str,
) -> int:
    """Read and validate a positive integer."""

    value = data.get(key)

    if not isinstance(value, int) or value <= 0:
        raise ValueError(
            f"{section}.{key} must be a positive integer"
        )

    return value


def _string_list(
    data: dict[str, Any],
    key: str,
    section: str,
) -> list[str]:
    """Read and validate a non-empty string list."""

    value = data.get(key)

    if not isinstance(value, list) or not value:
        raise ValueError(
            f"{section}.{key} must be a non-empty list"
        )

    normalized: list[str] = []

    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(
                f"{section}.{key} must contain "
                "non-empty strings"
            )

        normalized.append(item.strip())

    return normalized


def load_config(
    config_path: str | Path,
) -> AtlasConfig:
    """Load Project Atlas configuration from TOML."""

    path = Path(config_path)

    if not path.is_file():
        raise FileNotFoundError(
            f"Atlas config file not found: {path}"
        )

    with path.open("rb") as file:
        raw = tomllib.load(file)

    atlas_raw = raw.get("atlas")

    if not isinstance(atlas_raw, dict):
        raise ValueError(
            "[atlas] configuration is required"
        )

    collection_raw = raw.get("collection")

    if not isinstance(collection_raw, dict):
        raise ValueError(
            "[collection] configuration is required"
        )

    targets_raw = raw.get("targets")

    if not isinstance(targets_raw, list) or not targets_raw:
        raise ValueError(
            "At least one [[targets]] entry is required"
        )

    atlas = AtlasSettings(
        storage_region=_required_string(
            atlas_raw,
            "storage_region",
            "atlas",
        ),
        bucket=_required_string(
            atlas_raw,
            "bucket",
            "atlas",
        ),
        s3_prefix=_required_string(
            atlas_raw,
            "s3_prefix",
            "atlas",
        ),
        source_root=_required_string(
            atlas_raw,
            "source_root",
            "atlas",
        ),
    )

    collection = CollectionSettings(
        lookback_minutes=_positive_int(
            collection_raw,
            "lookback_minutes",
            "collection",
        ),
        period_seconds=_positive_int(
            collection_raw,
            "period_seconds",
            "collection",
        ),
        metrics=_string_list(
            collection_raw,
            "metrics",
            "collection",
        ),
    )

    targets: list[TargetSettings] = []

    for index, target_raw in enumerate(
        targets_raw,
        start=1,
    ):
        section = f"targets[{index}]"

        if not isinstance(target_raw, dict):
            raise ValueError(
                f"{section} must be a table"
            )

        enabled = target_raw.get(
            "enabled",
            True,
        )

        if not isinstance(enabled, bool):
            raise ValueError(
                f"{section}.enabled must be a boolean"
            )

        account_id = _required_string(
            target_raw,
            "account_id",
            section,
        )

        if not account_id.isdigit() or len(account_id) != 12:
            raise ValueError(
                f"{section}.account_id "
                "must be a 12-digit AWS account ID"
            )

        targets.append(
            TargetSettings(
                name=_required_string(
                    target_raw,
                    "name",
                    section,
                ),
                account_id=account_id,
                role_name=_required_string(
                    target_raw,
                    "role_name",
                    section,
                ),
                regions=_string_list(
                    target_raw,
                    "regions",
                    section,
                ),
                enabled=enabled,
            )
        )

    return AtlasConfig(
        atlas=atlas,
        collection=collection,
        targets=targets,
    )