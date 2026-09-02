"""Deterministic JSON serialization for validation, persistence, and audit hashes."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from hashlib import sha256
from math import isfinite
from typing import Any


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {field.name: _jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, float) and not isfinite(value):
        return f"NONFINITE:{value!r}"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"cannot serialize {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        _jsonable(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def content_hash(value: Any) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()
