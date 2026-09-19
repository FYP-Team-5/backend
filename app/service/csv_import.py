from __future__ import annotations

import csv
import io

from app.dto import CriteriaCreate, ExampleCreate, QuestionCreate, RubricCreate

QUESTION_COLUMNS = {"id", "prompt", "max_score", "score_increment"}
CRITERIA_COLUMNS = {"id", "criteria", "criteria_max_score"}
EXAMPLES_COLUMNS = {
    "id",
    "good_answer",
    "good_score",
    "average_answer",
    "average_score",
    "bad_answer",
    "bad_score",
}
EXAMPLES_BAND_COLUMNS: dict[str, tuple[str, str]] = {
    "excellent": ("good_answer", "good_score"),
    "average": ("average_answer", "average_score"),
    "poor": ("bad_answer", "bad_score"),
}


class CsvFormatError(ValueError):
    pass


def _read_rows(content: str, required_columns: set[str], label: str) -> csv.DictReader:
    reader = csv.DictReader(io.StringIO(content))
    if reader.fieldnames is None:
        raise CsvFormatError(f"{label} CSV has no header row.")
    missing = required_columns - set(reader.fieldnames)
    if missing:
        raise CsvFormatError(f"{label} CSV is missing column(s): {sorted(missing)}.")
    return reader


def _required_field(row: dict, column: str, line_number: int) -> str:
    value = (row.get(column) or "").strip()
    if not value:
        raise CsvFormatError(f"Row {line_number}: '{column}' is required.")
    return value


def _required_float(row: dict, column: str, line_number: int) -> float:
    raw = row.get(column)
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise CsvFormatError(f"Row {line_number}: '{column}' must be numeric.") from exc


def parse_questions_csv(content: str) -> list[QuestionCreate]:
    """Parse an instructor-uploaded questions CSV.

    Expected columns: id, prompt, max_score, score_increment, and an
    optional model_answer. 'id' is the stable external identifier later
    joined against the criteria CSV — it is never matched by text or row
    position. A model_answer set here is only a starting value; uploading a
    criteria CSV with its own model_answer for the same id overwrites it.
    """
    reader = _read_rows(content, QUESTION_COLUMNS, "Questions")
    questions: list[QuestionCreate] = []
    seen_ids: set[str] = set()
    for line_number, row in enumerate(reader, start=2):
        external_id = _required_field(row, "id", line_number)
        if external_id in seen_ids:
            raise CsvFormatError(f"Row {line_number}: duplicate question id '{external_id}'.")
        seen_ids.add(external_id)
        questions.append(
            QuestionCreate(
                external_id=external_id,
                prompt=_required_field(row, "prompt", line_number),
                max_score=_required_float(row, "max_score", line_number),
                score_increment=_required_float(row, "score_increment", line_number),
                model_answer=(row.get("model_answer") or "").strip() or None,
            )
        )
    if not questions:
        raise CsvFormatError("Questions CSV has no data rows.")
    return questions


def parse_criteria_csv(content: str) -> dict[str, RubricCreate]:
    """Parse an instructor-uploaded criteria + model-answer CSV.

    Expected columns: id, criteria, criteria_max_score, model_answer (optional).
    One row per criterion; rows sharing the same 'id' are grouped into a
    single RubricCreate for that question. 'id' must match an 'id' from the
    questions CSV exactly — the caller resolves that join.
    """
    reader = _read_rows(content, CRITERIA_COLUMNS, "Criteria")
    criteria_by_question: dict[str, list[CriteriaCreate]] = {}
    model_answer_by_question: dict[str, str | None] = {}
    for line_number, row in enumerate(reader, start=2):
        external_id = _required_field(row, "id", line_number)
        criteria_by_question.setdefault(external_id, []).append(
            CriteriaCreate(
                description=_required_field(row, "criteria", line_number),
                score=_required_float(row, "criteria_max_score", line_number),
            )
        )
        model_answer = (row.get("model_answer") or "").strip() or None
        if external_id not in model_answer_by_question:
            model_answer_by_question[external_id] = model_answer
        elif model_answer is not None and model_answer != model_answer_by_question[external_id]:
            raise CsvFormatError(
                f"Row {line_number}: model_answer for question '{external_id}' "
                "conflicts with an earlier row for the same question."
            )
    if not criteria_by_question:
        raise CsvFormatError("Criteria CSV has no data rows.")
    return {
        external_id: RubricCreate(
            criteria=criteria_list,
            model_answer=model_answer_by_question[external_id],
        )
        for external_id, criteria_list in criteria_by_question.items()
    }


def parse_examples_csv(content: str) -> dict[str, list[ExampleCreate]]:
    """Parse an instructor-uploaded few-shot examples CSV.

    Expected columns: id, good_answer, good_score, average_answer,
    average_score, bad_answer, bad_score. One row per question, supplying
    all three exemplar bands at once. 'id' must match an 'id' from the
    questions CSV exactly — the caller resolves that join.
    """
    reader = _read_rows(content, EXAMPLES_COLUMNS, "Examples")
    examples_by_question: dict[str, list[ExampleCreate]] = {}
    for line_number, row in enumerate(reader, start=2):
        external_id = _required_field(row, "id", line_number)
        if external_id in examples_by_question:
            raise CsvFormatError(f"Row {line_number}: duplicate question id '{external_id}'.")
        examples_by_question[external_id] = [
            ExampleCreate(
                band=band,
                example_answer=_required_field(row, answer_column, line_number),
                score=_required_float(row, score_column, line_number),
            )
            for band, (answer_column, score_column) in EXAMPLES_BAND_COLUMNS.items()
        ]
    if not examples_by_question:
        raise CsvFormatError("Examples CSV has no data rows.")
    return examples_by_question
