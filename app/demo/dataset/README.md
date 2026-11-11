# Demo training datasets

`mock_training_data.csv` contains ten compatible examples with ten unique
questions, selected from the full `training_data_moh_eng_rubrics.csv` reference.
Each row includes a generated `feedback` target tailored to its student answer.
The normal **Train now** workflow splits these into eight training questions,
one validation question and one test question using seed 42.

With `SEED_DEMO_USERS=true`, backend startup submits `mock_training_data.csv`
through the dataset upload service, including databases with an older demo dataset. The
worker publishes it as the newest ready version. The upload is idempotent:
restarting the backend does not add another version or replace subsequent
instructor uploads. Existing demo databases receive the version with feedback
once. Wait for **Training dataset** to show `mock_training_data.csv` as active
and ready with ten rows before selecting **Trained models → Train now**.
No training starts during seeding.

For a running backend, upload this CSV from **Training dataset** to make it the
latest version. Existing automatically collected closed-test examples are still
appended by the dataset service; check the published row count before training.
Use a demo database without collected closed-test rows for an exactly ten-row run.
Collected rows include their saved feedback, and instructor feedback corrections
produce a new immutable dataset version. Older eight-column uploads remain
accepted with blank feedback; previously published files are not rewritten.

The deployed training runner supports any nonempty compatible dataset. With
fewer than ten unique questions it reuses the training data for validation and
evaluation, recording `held_out_evaluation=false` and
`split_strategy=reuse_training_data` in the candidate configuration. These
metrics exercise the workflow and do not measure held-out model quality. Ten or
more questions retain the reference notebook's grouped 80/10/10 split.

The normal workflow saves and publishes a candidate; model activation remains a
separate instructor action. Deploy the updated backend and training runner
before using this dataset on AWS.
Autotraining validates feedback targets, trains grading first, then trains feedback
on the same question split. Both adapters are published as a related pair, with
grading metrics only. A dataset without feedback targets in both the training and
validation split cannot start the paired GPU training stages.
