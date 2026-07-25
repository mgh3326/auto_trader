"""Immutable train, validation, sealed-OOS, and CV window authority.

This module is deliberately stdlib-only so data preparation and registry
identity construction consume the same ex-ante evaluation schedule.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

__all__ = [
    "CANONICAL_EVALUATION_WINDOWS",
    "CVFoldWindow",
    "ClosedWindow",
    "EvaluationWindows",
]


@dataclass(frozen=True)
class ClosedWindow:
    start: str
    end: str

    def __post_init__(self) -> None:
        try:
            start = date.fromisoformat(self.start)
            end = date.fromisoformat(self.end)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid_evaluation_window") from exc
        if start > end:
            raise ValueError("invalid_evaluation_window")

    def to_dict(self) -> dict[str, str]:
        return {"start": self.start, "end": self.end}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ClosedWindow:
        if not isinstance(value, dict) or set(value) != {"start", "end"}:
            raise ValueError("invalid_evaluation_window")
        return cls(start=value["start"], end=value["end"])


@dataclass(frozen=True)
class CVFoldWindow:
    train: ClosedWindow
    validation: ClosedWindow

    def __post_init__(self) -> None:
        if self.train.end >= self.validation.start:
            raise ValueError("overlapping_evaluation_windows")

    def to_dict(self) -> dict[str, str]:
        return {
            "train_start": self.train.start,
            "train_end": self.train.end,
            "val_start": self.validation.start,
            "val_end": self.validation.end,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CVFoldWindow:
        expected = {"train_start", "train_end", "val_start", "val_end"}
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("invalid_evaluation_window")
        return cls(
            train=ClosedWindow(value["train_start"], value["train_end"]),
            validation=ClosedWindow(value["val_start"], value["val_end"]),
        )


@dataclass(frozen=True)
class EvaluationWindows:
    train: ClosedWindow
    validation: ClosedWindow
    sealed_oos: ClosedWindow
    cv_folds: tuple[CVFoldWindow, ...]

    def __post_init__(self) -> None:
        if not self.cv_folds:
            raise ValueError("missing_evaluation_windows")
        if self.train.end >= self.validation.start:
            raise ValueError("overlapping_evaluation_windows")
        if self.validation.end >= self.sealed_oos.start:
            raise ValueError("overlapping_evaluation_windows")
        scored = [fold.validation for fold in self.cv_folds]
        for left, right in zip(scored, scored[1:], strict=False):
            if left.end >= right.start:
                raise ValueError("overlapping_evaluation_windows")
        if any(window.end >= self.sealed_oos.start for window in scored):
            raise ValueError("overlapping_evaluation_windows")

    def to_dict(self) -> dict[str, Any]:
        return {
            "train": self.train.to_dict(),
            "validation": self.validation.to_dict(),
            "sealed_oos": self.sealed_oos.to_dict(),
            "cv_folds": [fold.to_dict() for fold in self.cv_folds],
        }

    def to_splits(self) -> dict[str, dict[str, str]]:
        return {
            "train": self.train.to_dict(),
            "val": self.validation.to_dict(),
            "test": self.sealed_oos.to_dict(),
        }

    def to_cv_folds(self) -> list[dict[str, str]]:
        return [fold.to_dict() for fold in self.cv_folds]

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> EvaluationWindows:
        expected = {"train", "validation", "sealed_oos", "cv_folds"}
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("invalid_evaluation_windows")
        raw_folds = value["cv_folds"]
        if not isinstance(raw_folds, list | tuple):
            raise ValueError("invalid_evaluation_windows")
        return cls(
            train=ClosedWindow.from_dict(value["train"]),
            validation=ClosedWindow.from_dict(value["validation"]),
            sealed_oos=ClosedWindow.from_dict(value["sealed_oos"]),
            cv_folds=tuple(CVFoldWindow.from_dict(fold) for fold in raw_folds),
        )


CANONICAL_EVALUATION_WINDOWS = EvaluationWindows(
    train=ClosedWindow("2024-04-01", "2025-06-30"),
    validation=ClosedWindow("2025-07-01", "2026-01-31"),
    sealed_oos=ClosedWindow("2026-02-01", "2026-03-22"),
    cv_folds=(
        CVFoldWindow(
            train=ClosedWindow("2024-04-01", "2025-03-31"),
            validation=ClosedWindow("2025-04-01", "2025-06-30"),
        ),
        CVFoldWindow(
            train=ClosedWindow("2024-04-01", "2025-06-30"),
            validation=ClosedWindow("2025-07-01", "2025-09-30"),
        ),
        CVFoldWindow(
            train=ClosedWindow("2024-04-01", "2025-09-30"),
            validation=ClosedWindow("2025-10-01", "2025-12-31"),
        ),
        CVFoldWindow(
            train=ClosedWindow("2024-04-01", "2025-12-31"),
            validation=ClosedWindow("2026-01-01", "2026-01-31"),
        ),
    ),
)
