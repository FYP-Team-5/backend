"""Validation at the boundaries between grading, feedback and saved results."""

import json

import pytest
from pydantic import ValidationError

from app.dto import (
    BinaryCriteriaResult,
    CriteriaCreate,
    CriteriaEvaluationResult,
    CriteriaGradingResult,
    ExampleCreate,
    FeedbackResult,
    FewShotGradingResult,
    QuestionCreate,
)


def test_model_outputs_and_assembled_rubric_result_have_separate_schemas():
    labels = BinaryCriteriaResult.model_validate_json("[1, 0]")
    assert labels.root == [1, 0]
    assert labels.model_dump_json() == "[1,0]"
    assert labels.model_json_schema()["type"] == "array"
    legacy = CriteriaEvaluationResult.model_validate_json(
        '{"criteria_met":[{"criteria_id":"c1","is_met":true}]}'
    )
    feedback = FeedbackResult(feedback="You identified the main idea.")
    result = CriteriaGradingResult(
        score=2, feedback=feedback.feedback, criteria_met=legacy.criteria_met
    )
    assert result.score == 2
    with pytest.raises(ValidationError):
        CriteriaGradingResult.model_validate(legacy.model_dump())


@pytest.mark.parametrize(
    "value", [[], [True], [False], [1.0], ["1"], [-1], [2], {"criteria_met": [1]}]
)
def test_binary_schema_rejects_wrong_shapes_and_noninteger_labels(value):
    with pytest.raises(ValidationError):
        BinaryCriteriaResult.model_validate_json(json.dumps(value))


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_grading_and_assessment_scores_must_be_finite(value):
    for model, fields in (
        (CriteriaCreate, {"description": "Accuracy", "score": value}),
        (
            ExampleCreate,
            {"band": "excellent", "example_answer": "Example", "score": value},
        ),
        (
            QuestionCreate,
            {"prompt": "Question", "max_score": value, "score_increment": 1},
        ),
        (
            QuestionCreate,
            {"prompt": "Question", "max_score": 4, "score_increment": value},
        ),
        (
            CriteriaGradingResult,
            {"score": value, "feedback": "Feedback", "criteria_met": []},
        ),
        (FewShotGradingResult, {"score": value, "feedback": "Feedback"}),
    ):
        with pytest.raises(ValidationError):
            model.model_validate(fields)
