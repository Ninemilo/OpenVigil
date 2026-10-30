"""Numeric review commands; offsets refer to immutable Unicode code points."""

from __future__ import annotations

import re
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Reason = Annotated[str, Field(min_length=3, max_length=2000)]


class NumericReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class NumericSourceBinding(NumericReviewRequest):
    expected_document_version: str = Field(min_length=1, max_length=32)
    expected_source_sha256: Digest
    expected_text_sha256: Digest
    expected_parser_version: str = Field(min_length=1, max_length=160)
    expected_locator_sha256: Digest


class NumericCorrection(NumericReviewRequest):
    start: int = Field(ge=0, le=8000)
    end: int = Field(ge=1, le=8000)
    original_value: str = Field(min_length=1, max_length=80)
    corrected_value: str = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,3})?$",
    )
    original_unit: str = Field(max_length=32)
    corrected_unit: str = Field(max_length=32)
    unit_start: int | None = Field(default=None, ge=0, le=8000)
    unit_end: int | None = Field(default=None, ge=1, le=8000)

    @field_validator("original_value")
    @classmethod
    def numeric_ocr_lexeme(cls, value: str) -> str:
        if re.fullmatch(r"[0-9OoIl|+\-−.,eE]+", value) is None or not re.search(
            r"[0-9OoIl|]", value
        ):
            raise ValueError("original value must be a numeric OCR lexeme")
        return value

    @field_validator("original_unit", "corrected_unit")
    @classmethod
    def unit_literal(cls, value: str) -> str:
        if value and (value != value.strip() or any(ord(char) < 32 for char in value)):
            raise ValueError("units must be explicit printable literals without edge whitespace")
        return value

    @model_validator(mode="after")
    def spans_and_changes(self) -> Self:
        if self.end <= self.start:
            raise ValueError("numeric span must have positive length")
        if (self.unit_start is None) != (self.unit_end is None):
            raise ValueError("unit offsets must both be present or both be null")
        if self.unit_start is not None and self.unit_end is not None:
            if (
                self.unit_end <= self.unit_start
                or not self.original_unit
                or not self.corrected_unit
            ):
                raise ValueError("unit span must contain a nonempty explicit unit")
        elif self.original_unit != self.corrected_unit:
            raise ValueError("contextual units cannot be changed without an original text span")
        if (
            self.original_value == self.corrected_value
            and self.original_unit == self.corrected_unit
        ):
            raise ValueError("a correction must change a value or unit")
        return self


class NumericReviewProposal(NumericSourceBinding):
    expected_revision: int = Field(ge=0)
    kind: Literal["confirm_original", "correct_values"]
    corrections: list[NumericCorrection] = Field(max_length=40)
    reason: Reason

    @field_validator("reason")
    @classmethod
    def reason_content(cls, value: str) -> str:
        if len(value.strip()) < 3:
            raise ValueError("a substantive reason is required")
        return value

    @model_validator(mode="after")
    def corrections_match_kind(self) -> Self:
        if (self.kind == "correct_values") != bool(self.corrections):
            raise ValueError("correction entries must match the proposal kind")
        return self


class NumericReviewDecision(NumericSourceBinding):
    expected_revision: int = Field(ge=1)
    action: Literal["approve", "reject", "withdraw"]
    reason: Reason

    @field_validator("reason")
    @classmethod
    def reason_content(cls, value: str) -> str:
        if len(value.strip()) < 3:
            raise ValueError("a substantive reason is required")
        return value
