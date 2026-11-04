"""The eight-column rubric CSV contract shared with the QLoRA repository."""

from __future__ import annotations

import csv
import io
import json
import math

COLUMNS = [
    "id",
    "question",
    "desired_answer",
    "student_answer",
    "criteria[]",
    "criteria_score[]",
    "criteria_max_score[]",
    "student_score",
]
ARRAY_COLUMNS = COLUMNS[4:7]


class TrainingCsvError(ValueError):
    pass


def validate_row(row: dict, line: int = 0) -> dict:
    prefix = f"Row {line}: " if line else ""

    def fail(message):
        raise TrainingCsvError(prefix + message)

    if set(row) != set(COLUMNS):
        fail("columns must match the reference rubric CSV.")
    result = dict(row)
    for key in COLUMNS[:4]:
        if not isinstance(row[key], str):
            fail(f"{key} must be text.")
        if key in {"id", "question"} and not row[key].strip():
            fail(f"{key} must not be blank.")
    arrays = []
    for key in ARRAY_COLUMNS:
        value = row[key]
        try:
            value = json.loads(value) if isinstance(value, str) else value
        except (ValueError, TypeError):
            fail(f"{key} must be a JSON array.")
        if not isinstance(value, list) or not value:
            fail(f"{key} must be a non-empty JSON array.")
        arrays.append(value)
    criteria, scores, maxima = arrays
    if len({len(value) for value in arrays}) != 1:
        fail("criterion arrays must have matching lengths.")
    if not all(isinstance(value, str) and value.strip() for value in criteria):
        fail("criterion descriptions must be non-empty text.")

    def number(value):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            fail("criterion scores and maxima must be finite numbers.")
        return float(value)

    scores, maxima = [list(map(number, values)) for values in (scores, maxima)]
    if any(
        limit <= 0 or score < 0 or score > limit for score, limit in zip(scores, maxima)
    ):
        fail("criterion scores must be within their positive maxima.")
    try:
        total = float(row["student_score"])
    except (ValueError, TypeError):
        fail("student_score must be numeric.")
    if (
        not math.isfinite(total)
        or total < 0
        or not math.isclose(total, sum(scores), rel_tol=1e-9, abs_tol=1e-7)
    ):
        fail("student_score must equal the sum of criterion scores.")
    for key, values in zip(ARRAY_COLUMNS, (criteria, scores, maxima)):
        result[key] = values
    result["student_score"] = total
    if not row["student_answer"].strip() and total != 0:
        fail("a blank student_answer must have a zero score.")
    return result


def parse_training_csv(source) -> list[dict]:
    """Read a binary file without evaluating array cell contents as Python."""
    text = io.TextIOWrapper(source, encoding="utf-8-sig", newline="")
    try:
        reader = csv.DictReader(text, strict=True)
        if reader.fieldnames != COLUMNS:
            raise TrainingCsvError(
                "CSV header and column order must be: " + ",".join(COLUMNS)
            )
        rows = []
        for row in reader:
            rows.append(validate_row(row, reader.line_num))
        return rows
    except (UnicodeError, csv.Error) as exc:
        raise TrainingCsvError(f"Invalid UTF-8 CSV: {exc}") from exc
    finally:
        text.detach()


def write_training_csv(rows, target) -> None:
    text = io.TextIOWrapper(target, encoding="utf-8", newline="", write_through=True)
    try:
        writer = csv.DictWriter(text, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            values = dict(row)
            for key in ARRAY_COLUMNS:
                values[key] = json.dumps(
                    values[key],
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            writer.writerow(values)
        text.flush()
    finally:
        text.detach()


def encode_training_csv(rows) -> bytes:
    output = io.BytesIO()
    write_training_csv(rows, output)
    return output.getvalue()
