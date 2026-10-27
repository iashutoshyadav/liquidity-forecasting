"""Configuration loading, scale resolution and config hashing."""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import yaml

from liqfc.core.enums import CURRENCIES

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_DIR = PROJECT_ROOT / "config"
SCALES = ("starter", "full")


def _load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class Settings:
    """Resolved, immutable-by-convention configuration for one scale."""

    raw: dict
    scale_name: str

    @property
    def seed(self) -> int:
        return int(self.raw["seed"])

    @property
    def scale(self) -> dict:
        return self.raw["scale"]

    @property
    def reporting_currency(self) -> str:
        return self.raw["reporting_currency"]

    @property
    def minor_units(self) -> dict[str, int]:
        return {c: int(v["minor_units"]) for c, v in self.raw["currencies"].items()}

    @property
    def id_year(self) -> int:
        return int(self.raw["id_year"])

    @property
    def start(self) -> pd.Timestamp:
        return pd.Timestamp(self.raw["calendar"]["start_date"])

    @property
    def end(self) -> pd.Timestamp:
        return pd.Timestamp(self.raw["calendar"]["end_date"])

    @property
    def generation_ts(self) -> pd.Timestamp:
        return pd.Timestamp(self.raw["generation_timestamp"]).tz_localize(None)

    @property
    def gen(self) -> dict:
        return self.raw["generation"]

    @property
    def forecast(self) -> dict:
        return self.raw["forecast"]

    @property
    def scenarios(self) -> dict:
        return self.raw["scenarios"]

    @property
    def anomaly_counts(self) -> dict[str, int]:
        return self.raw["anomalies"]["counts"][self.scale_name]

    @property
    def config_hash(self) -> str:
        return sha256_text(canonical_json(self.raw))

    @property
    def external(self) -> dict:
        return self.raw["external_data"]

    def external_path(self, key: str) -> Path:
        return PROJECT_ROOT / self.raw["external_data"][key]

    @property
    def generator_version(self) -> str:
        return self.raw["project"]["generator_version"]

    @property
    def calculation_version(self) -> str:
        return self.raw["project"]["calculation_version"]


DATA_MODES = ("hybrid", "synthetic")


def load_settings(scale: str = "starter", seed: int | None = None, config_dir: Path | None = None,
                  overrides: dict | None = None, data_mode: str | None = None) -> Settings:
    """Merge default.yaml + scales.yaml[scale] + scenarios.yaml + anomalies.yaml.

    ``data_mode='synthetic'`` switches off both external datasets; ``'hybrid'`` (or None) keeps the
    configured external_data settings. Checksums of the external files used are added to the
    resolved config so the config hash changes whenever those files change.
    """
    if scale not in SCALES:
        raise ValueError(f"unknown scale {scale!r}; expected one of {SCALES}")
    cdir = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR
    raw = _load_yaml(cdir / "default.yaml")
    raw["scale"] = copy.deepcopy(_load_yaml(cdir / "scales.yaml")["scales"][scale])
    raw["scenarios"] = _load_yaml(cdir / "scenarios.yaml")["scenarios"]
    raw["anomalies"] = _load_yaml(cdir / "anomalies.yaml")["anomalies"]
    if seed is not None:
        raw["seed"] = int(seed)
    if overrides:
        _deep_update(raw, overrides)
    if data_mode is not None:
        if data_mode not in DATA_MODES:
            raise ValueError(f"unknown data mode {data_mode!r}; expected one of {DATA_MODES}")
        if data_mode == "synthetic":
            raw["external_data"]["fx_source"] = "synthetic"
            raw["external_data"]["receivable_delay_profile"] = "synthetic"
    _resolve_external(raw)
    _validate(raw, scale)
    return Settings(raw=raw, scale_name=scale)


def _resolve_external(raw: dict) -> None:
    ext = raw["external_data"]
    used = []
    if ext["fx_source"] == "kaggle":
        used.append("fx_file")
    elif ext["fx_source"] != "synthetic":
        raise ValueError("external_data.fx_source must be kaggle or synthetic")
    if ext["receivable_delay_profile"] == "calibrated":
        used.append("calibration_file")
    elif ext["receivable_delay_profile"] != "synthetic":
        raise ValueError("external_data.receivable_delay_profile must be calibrated or synthetic")
    resolved = {}
    for key in used:
        path = PROJECT_ROOT / ext[key]
        if not path.exists():
            raise FileNotFoundError(f"{path} missing; run scripts/prepare_external_data.py or use --data-mode synthetic")
        resolved[ext[key]] = hashlib.sha256(path.read_bytes()).hexdigest()
    ext["resolved_sha256"] = resolved
    ext["mode"] = "synthetic" if not used else ("hybrid" if len(used) == 2 else "partial-hybrid")


def _deep_update(dst: dict, src: dict) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_update(dst[k], v)
        else:
            dst[k] = v


def _validate(raw: dict, scale: str) -> None:
    if tuple(raw["currencies"]) != CURRENCIES:
        raise ValueError(f"configured currencies {tuple(raw['currencies'])} must equal {CURRENCIES}")
    if raw["reporting_currency"] not in raw["currencies"]:
        raise ValueError("reporting currency must be one of the supported currencies")
    if raw["anomalies"]["counts"].get(scale) is None:
        raise ValueError(f"no anomaly counts configured for scale {scale}")
    s = raw["scale"]
    if s["bank_accounts"] < s["legal_entities"] * raw["generation"]["account"]["min_per_entity"]:
        raise ValueError("bank_accounts too small for min_per_entity")
