from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

TaintSource = Literal["user_input", "model_output", "retrieved_text", "tool_output", "memory"]
TaintLevel = Literal["untrusted", "validated", "trusted"]


@dataclass(frozen=True, slots=True)
class TaintedString:
    """String wrapper carrying taint metadata with propagation semantics."""

    value: str
    source: TaintSource
    source_id: str
    taint_level: TaintLevel = "untrusted"

    def __str__(self) -> str:
        return self.value

    def __repr__(self) -> str:
        return (
            f"TaintedString(value={self.value!r}, source={self.source!r}, "
            f"source_id={self.source_id!r}, taint_level={self.taint_level!r})"
        )

    def __len__(self) -> int:
        return len(self.value)

    def __getitem__(self, item):
        return TaintedString(
            value=self.value[item],
            source=self.source,
            source_id=self.source_id,
            taint_level=self.taint_level,
        )

    def __add__(self, other: object) -> TaintedString:
        other_ts = ensure_tainted(other, default_source=self.source, default_source_id=self.source_id)
        return TaintedString(
            value=self.value + other_ts.value,
            source=self.source,
            source_id=f"{self.source_id}|{other_ts.source_id}",
            taint_level=merge_taint_level(self.taint_level, other_ts.taint_level),
        )

    def __radd__(self, other: object) -> TaintedString:
        other_ts = ensure_tainted(other, default_source=self.source, default_source_id=self.source_id)
        return TaintedString(
            value=other_ts.value + self.value,
            source=other_ts.source,
            source_id=f"{other_ts.source_id}|{self.source_id}",
            taint_level=merge_taint_level(self.taint_level, other_ts.taint_level),
        )

    def format(self, *args: object, **kwargs: object) -> TaintedString:
        merged_level = self.taint_level
        rendered_args = []
        for arg in args:
            arg_ts = ensure_tainted(arg, default_source=self.source, default_source_id=self.source_id)
            rendered_args.append(arg_ts.value)
            merged_level = merge_taint_level(merged_level, arg_ts.taint_level)
        rendered_kwargs = {}
        for key, value in kwargs.items():
            value_ts = ensure_tainted(value, default_source=self.source, default_source_id=self.source_id)
            rendered_kwargs[key] = value_ts.value
            merged_level = merge_taint_level(merged_level, value_ts.taint_level)
        return TaintedString(
            value=self.value.format(*rendered_args, **rendered_kwargs),
            source=self.source,
            source_id=self.source_id,
            taint_level=merged_level,
        )

    def with_level(self, taint_level: TaintLevel) -> TaintedString:
        return TaintedString(
            value=self.value,
            source=self.source,
            source_id=self.source_id,
            taint_level=taint_level,
        )


def merge_taint_level(left: TaintLevel, right: TaintLevel) -> TaintLevel:
    order = {"trusted": 0, "validated": 1, "untrusted": 2}
    return left if order[left] >= order[right] else right


def ensure_tainted(value: object, default_source: TaintSource, default_source_id: str) -> TaintedString:
    if isinstance(value, TaintedString):
        return value
    return TaintedString(value=str(value), source=default_source, source_id=default_source_id, taint_level="trusted")


def validate_for_tool_arg(tainted_str: TaintedString | str, allowed_pattern: str | re.Pattern[str]) -> str:
    """Extract and validate tool argument from tainted text before use."""
    if isinstance(tainted_str, TaintedString):
        text = tainted_str.value
        level = tainted_str.taint_level
    else:
        text = tainted_str
        level = "trusted"

    pattern = re.compile(allowed_pattern) if isinstance(allowed_pattern, str) else allowed_pattern
    match = pattern.search(text)
    if not match:
        raise ValueError("Tool argument validation failed: value does not match allowed pattern")

    extracted = match.group(0)
    if level == "untrusted" and extracted.strip() != text.strip() and not pattern.fullmatch(text.strip()):
        raise ValueError("Tool argument validation failed: untrusted value contains extra text")
    return extracted
