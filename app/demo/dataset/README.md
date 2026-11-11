# Demo training datasets

`single_row.csv` contains one compatible example for the normal **Train now**
workflow. The full `training_data_moh_eng_rubrics.csv` is retained as a reference.

With `SEED_DEMO_USERS=true`, backend startup submits `single_row.csv` through the
dataset upload service, including on databases with an older demo dataset. The
worker publishes it as the newest ready version. The upload is idempotent:
restarting the backend does not add another version or replace subsequent
instructor uploads. Wait for **Training dataset** to show `single_row.csv` as
active and ready with one row before selecting **Trained models → Train now**.
No training starts during seeding.

For a running backend, upload this CSV from **Training dataset** to make it the
latest version. Existing automatically collected closed-test examples are still
appended by the dataset service; check the published row count before training.
Use a demo database without collected closed-test rows for an exactly one-row run.

The deployed training runner supports any nonempty compatible dataset. With
fewer than ten unique questions it reuses the training data for validation and
evaluation, recording `held_out_evaluation=false` and
`split_strategy=reuse_training_data` in the candidate configuration. These
metrics exercise the workflow and do not measure held-out model quality. Ten or
more questions retain the reference notebook's grouped 80/10/10 split.

The normal workflow saves and publishes a candidate; model activation remains a
separate instructor action. Deploy the updated backend and training runner
before using this dataset on AWS.
