from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

SampleCallback = Callable[[Any], None]
ErrorCallback = Callable[[str, str], None]
PreviewCallback = Callable[[str, Any], None]
MetadataCallback = Callable[[str, dict[str, Any]], None]


@dataclass(slots=True)
class SourceCallbacks:
    on_sample: SampleCallback
    on_error: ErrorCallback
    on_preview: PreviewCallback | None = None
    on_metadata: MetadataCallback | None = None


@dataclass(slots=True)
class SourceMetadata:
    values: dict[str, Any] = field(default_factory=dict)
