"""Run three QLoRA epochs on one row without publishing weights."""

import argparse
import hashlib
import json
import math
import tempfile
from pathlib import Path
from urllib.request import urlopen

from app.service.training_csv import encode_training_csv, parse_training_csv

MODEL_COMMIT = "8a2b94ec95754d875dc546461ef15dc2a279ce5a"
NOTEBOOK_SHA256 = "9e3d311cf86e062d0fa9622f5c3ab9047d767ed32608626f1574b74888a084e6"
NOTEBOOK_URL = (
    f"https://raw.githubusercontent.com/FYP-Team-5/model/{MODEL_COMMIT}/"
    "Rubrics/Qlora%20Comparison/qlora%20%2848r%2C96a%29.ipynb"
)
DEFAULT_DATASET = (
    Path(__file__).parents[1] / "dataset" / "training_data_moh_eng_rubrics.csv"
)
TRAINING_CELLS = (4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 26, 28, 30, 32)


def prepare_one_row(source: Path, output: Path) -> dict:
    with source.open("rb") as stream:
        rows = parse_training_csv(stream)
    for number, row in enumerate(rows, 1):
        scores, maxima = row["criteria_score[]"], row["criteria_max_score[]"]
        if row["desired_answer"].strip() and all(
            score in (0, maximum) for score, maximum in zip(scores, maxima, strict=True)
        ):
            break
    else:
        raise ValueError(
            "The dataset has no row with a reference answer and binary labels."
        )
    row = dict(row, **{"criteria_score[]": [int(score != 0) for score in scores]})
    output.mkdir(parents=True, exist_ok=True)
    selected = output / "single-row.csv"
    selected.write_bytes(encode_training_csv([row]))
    return {
        "status": "prepared",
        "source_rows": len(rows),
        "training_rows": 1,
        "selected_data_row": number,
        "source_checksum": hashlib.sha256(source.read_bytes()).hexdigest(),
        "single_row_checksum": hashlib.sha256(selected.read_bytes()).hexdigest(),
        "hf_uploaded": False,
        "held_out_evaluation": False,
    }


def load_notebook(path: Path | None, output: Path) -> dict:
    if path is None:
        path = output / "reference-notebook.ipynb"
        with urlopen(NOTEBOOK_URL, timeout=60) as response:
            payload = response.read()
    else:
        payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != NOTEBOOK_SHA256:
        raise ValueError("Notebook differs from the pinned training reference.")
    if path == output / "reference-notebook.ipynb":
        path.write_bytes(payload)
    return json.loads(payload)


def train_one_row(output: Path, notebook: dict, report: dict) -> dict:
    from huggingface_hub import HfApi

    adapter = output / "adapter"
    if adapter.exists():
        raise FileExistsError(
            "Choose a new output directory to preserve existing weights."
        )
    base_revision = HfApi(token=False).model_info("Qwen/Qwen3.5-9B").sha
    scope = {"__name__": "__main__", "BASE_REVISION": base_revision}
    for number in TRAINING_CELLS:
        code = notebook["cells"][number]["source"]
        code = "".join(code) if isinstance(code, list) else code
        if number == 4:
            code = code.replace('hf_token = os.environ["HF_TOKEN"]', "hf_token = None")
        if number == 14:
            code = code.replace(
                'print(dataset[0]["prompt"])',
                'print("Single-row prompt prepared; contents redacted")',
            )
        if number in (18, 22):
            code = code.replace(
                "from_pretrained(MODEL_ID)",
                "from_pretrained(MODEL_ID, revision=BASE_REVISION, token=False)",
            ).replace(
                "MODEL_ID, quantization_config=",
                "MODEL_ID, revision=BASE_REVISION, token=False, quantization_config=",
            )
        if number == 16:
            if len(scope["records"]) != 1:
                raise RuntimeError("The smoke test must use exactly one row.")
            scope.update(
                train_dataset=scope["dataset"],
                validation_dataset=scope["dataset"],
                test_dataset=scope["dataset"],
            )
            print("One unique row; validation reuses it for execution checks only.")
            continue
        # Only training cells from the checksum-verified notebook execute.
        exec(compile(code, f"single_row_cell_{number}", "exec"), scope)  # noqa: S102
        if number == 4:
            scope["display"] = lambda *_: None
        if number == 6:
            scope.update(
                DATASET_PATH=str(output / "single-row.csv"),
                OUTPUT_DIR=str(adapter),
                RUN_TRAINING=True,
                RUN_TEST_EVALUATION=False,
                RUN_HF_UPLOAD=False,
                MAX_SEQ_LENGTH=4096,
            )
    trainer, result = scope["trainer"], scope["train_result"]
    if (
        len(trainer.train_dataset) != 1
        or trainer.state.global_step != 3
        or trainer.state.epoch != 3
        or not math.isfinite(float(result.training_loss))
    ):
        raise RuntimeError("Single-row training did not complete three valid epochs.")
    artifacts = {
        name: (adapter / name).stat().st_size
        for name in ("adapter_config.json", "adapter_model.safetensors")
    }
    if not all(artifacts.values()):
        raise RuntimeError("Training did not save a nonempty adapter.")
    return {
        **report,
        "status": "completed",
        "base_model": scope["MODEL_ID"],
        "base_revision": base_revision,
        "model_repo_commit": MODEL_COMMIT,
        "epochs": trainer.state.epoch,
        "global_step": trainer.state.global_step,
        "training_loss": float(result.training_loss),
        "training_runtime_seconds": result.metrics["train_runtime"],
        "lora_rank": scope["LORA_R"],
        "lora_alpha": scope["LORA_ALPHA"],
        "adapter_bytes": artifacts,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--notebook", type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    output = (args.output or Path(tempfile.mkdtemp(prefix="fyp-one-row-"))).resolve()
    report = prepare_one_row(args.dataset, output)
    if not args.prepare_only:
        report = train_one_row(output, load_notebook(args.notebook, output), report)
    (output / "result.json").write_text(
        json.dumps(report, indent=2, allow_nan=False) + "\n"
    )
    print(json.dumps(report, allow_nan=False))
    print(f"Results: {output}")


if __name__ == "__main__":
    main()
