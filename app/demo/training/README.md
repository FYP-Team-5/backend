# One-row QLoRA smoke test

Run the same Qwen3.5-9B, NF4, rank-48 / alpha-96 training setup as the deployment
on one compatible row for three epochs. The default source is the bundled
3,778-row dataset. The test saves a local adapter and `result.json`; it never
uploads weights, merges a branch, registers a candidate, or changes the active model.
It runs only when invoked explicitly.

From `backend/`, on an NVIDIA GPU host with Python 3.12 and enough GPU memory
(the verified deployment type is `g5.xlarge`):

```bash
python3.12 -m venv /tmp/fyp-one-row-venv
/tmp/fyp-one-row-venv/bin/pip install -r requirements.txt \
  -r ../deployment/autotraining/requirements.txt
/tmp/fyp-one-row-venv/bin/python -m app.demo.training.single_row \
  --output /tmp/fyp-one-row-training
```

The runner downloads a checksum-verified notebook from the pinned model commit.
To use the matching local checkout, add:

```bash
--notebook '../model/Rubrics/Qlora Comparison/qlora (48r,96a).ipynb'
```

With the backend dependencies installed, check row selection without a GPU,
training dependencies, or network access:

```bash
python -m app.demo.training.single_row --prepare-only
```

`--dataset /path/to/dataset.csv` selects another CSV using the existing eight-column
rubric format. Rows without reference answers or with partial-credit labels are
skipped. The selected row uses binary labels, and validation reuses that same row
to exercise the trainer. This is an execution test, with no held-out quality
evaluation. It checks three optimiser steps, finite loss, and nonempty adapter files.

Use a fresh output directory for each training run. Model/dependency downloads
still take time on a cold host even with one row. GPU instances incur compute
charges while running; stop or destroy temporary infrastructure separately when done.
