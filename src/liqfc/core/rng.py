"""Deterministic, independent random streams.

Every generator module draws from its own named stream derived from (master seed, name), so
changing the draws of one module never shifts the random numbers of another.
"""
from __future__ import annotations

import hashlib

import numpy as np


def derive_seed(master_seed: int, name: str) -> int:
    digest = hashlib.sha256(f"{master_seed}:{name}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def stream(master_seed: int, name: str) -> np.random.Generator:
    return np.random.Generator(np.random.PCG64(derive_seed(master_seed, name)))


def stable_unit(master_seed: int, salt: str, key: str) -> float:
    """Hash-based uniform in [0, 1): stable per key, independent of row order."""
    digest = hashlib.sha256(f"{master_seed}|{salt}|{key}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / 2**64
