import hashlib
import json
from unittest.mock import MagicMock, patch

import pytest

from app.demo.training.single_row import (
    DEFAULT_DATASET,
    load_notebook,
    prepare_one_row,
    train_one_row,
)
from app.service.training_csv import encode_training_csv


def row(**changes):
    return {
        "id": "demo",
        "question": "Explain a concept.",
        "desired_answer": "Reference answer",
        "student_answer": "Student answer",
        "criteria[]": ["First concept", "Second concept"],
        "criteria_score[]": [2, 0],
        "criteria_max_score[]": [2, 3],
        "student_score": 2,
        **changes,
    }


def test_bundled_dataset_selects_exactly_one_row(tmp_path):
    report = prepare_one_row(DEFAULT_DATASET, tmp_path)
    assert report["source_rows"] == 3778
    assert report["training_rows"] == 1
    assert (
        report["source_checksum"]
        == hashlib.sha256(DEFAULT_DATASET.read_bytes()).hexdigest()
    )
    assert report["hf_uploaded"] is False
    assert report["held_out_evaluation"] is False
    import csv

    with (tmp_path / "single-row.csv").open() as stream:
        assert len(list(csv.DictReader(stream))) == 1


def test_selection_skips_partial_credit_and_normalizes_weighted_labels(tmp_path):
    source = tmp_path / "source.csv"
    source.write_bytes(
        encode_training_csv(
            [
                row(**{"criteria_score[]": [1, 0], "student_score": 1}),
                row(),
            ]
        )
    )
    report = prepare_one_row(source, tmp_path / "result")
    assert report["selected_data_row"] == 2
    import csv

    with (tmp_path / "result/single-row.csv").open() as stream:
        selected = next(csv.DictReader(stream))
    assert json.loads(selected["criteria_score[]"]) == [1, 0]
    assert json.loads(selected["criteria_max_score[]"]) == [2, 3]


def test_single_question_is_supported_without_production_split(tmp_path):
    source = tmp_path / "source.csv"
    source.write_bytes(encode_training_csv([row()]))
    assert prepare_one_row(source, tmp_path / "result")["training_rows"] == 1


def test_no_compatible_row_fails_before_training(tmp_path):
    source = tmp_path / "source.csv"
    source.write_bytes(encode_training_csv([row(desired_answer="")]))
    with pytest.raises(ValueError, match="no row"):
        prepare_one_row(source, tmp_path / "result")


def test_unpinned_notebook_is_rejected(tmp_path):
    notebook = tmp_path / "untrusted.ipynb"
    notebook.write_text('{"cells": []}')
    with pytest.raises(ValueError, match="pinned"):
        load_notebook(notebook, tmp_path)


def test_smoke_runner_never_executes_upload_cells(tmp_path):
    cells = [{"source": ""} for _ in range(47)]
    cells[8]["source"] = "records = [{}]; dataset = records"
    cells[6]["source"] = (
        'MODEL_ID = "Qwen/Qwen3.5-9B"; LORA_R = 48; LORA_ALPHA = 96; RUN_HF_UPLOAD = True'
    )
    cells[26]["source"] = "assert not RUN_HF_UPLOAD and not RUN_TEST_EVALUATION"
    cells[32]["source"] = """
from pathlib import Path
from types import SimpleNamespace
path = Path(OUTPUT_DIR)
path.mkdir(parents=True)
for name in ("adapter_config.json", "adapter_model.safetensors"):
    (path / name).write_bytes(b"test")
trainer = SimpleNamespace(train_dataset=dataset, state=SimpleNamespace(global_step=3, epoch=3.0))
train_result = SimpleNamespace(training_loss=0.1, metrics={"train_runtime": 1.0})
"""
    cells[46]["source"] = 'raise AssertionError("HF upload must never execute")'
    api = MagicMock()
    api.model_info.return_value.sha = "a" * 40
    with patch("huggingface_hub.HfApi", return_value=api):
        report = train_one_row(tmp_path, {"cells": cells}, {"hf_uploaded": False})
    assert report["status"] == "completed"
    assert report["global_step"] == 3
    assert report["hf_uploaded"] is False
