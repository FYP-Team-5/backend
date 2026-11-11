import asyncio
from types import SimpleNamespace

from app.service.demo_data import DEMO_TRAINING_DATASET, DemoDataService
from app.service.training_csv import parse_training_csv
from tests.service import test_training_dataset
from tests.service.test_training_dataset import (
    download,
    drain,
    seed_row,
)

dataset = test_training_dataset.dataset


def demo_service(service):
    return DemoDataService(
        grading_store=service.repository.store,
        user_repository=SimpleNamespace(
            list=lambda **_: [SimpleNamespace(id="2000001")]
        ),
        dataset_service=service,
    )


def test_bundled_single_row_is_compatible():
    with DEMO_TRAINING_DATASET.open("rb") as source:
        rows = parse_training_csv(source)
    assert len(rows) == 1
    row = rows[0]
    assert row["desired_answer"].strip()
    assert all(
        score in (0, maximum)
        for score, maximum in zip(
            row["criteria_score[]"], row["criteria_max_score[]"], strict=True
        )
    )


def test_demo_upload_replaces_old_dataset_once_and_preserves_later_uploads(dataset):
    import io

    from app.service.training_csv import encode_training_csv

    service, _, _, _ = dataset
    old = asyncio.run(
        service.upload(
            io.BytesIO(encode_training_csv([seed_row(), seed_row("Another answer")])),
            "2000001",
            "previous-demo.csv",
        )
    )
    drain(service)
    assert download(service)[1]["id"] == old["id"]

    demo = demo_service(service)
    asyncio.run(demo._seed_training_dataset())
    drain(service)
    rows, active = download(service)
    assert len(rows) == active["row_count"] == 1
    assert active["is_active"] and active["status"] == "ready"
    assert active["filename"] == "single_row.csv"
    assert active["number"] > old["number"]

    asyncio.run(demo._seed_training_dataset())
    drain(service)
    assert download(service)[1]["id"] == active["id"]
    assert len(asyncio.run(service.list_versions())) == 2

    later = asyncio.run(
        service.upload(
            io.BytesIO(encode_training_csv([seed_row("Later instructor upload")])),
            "2000001",
            "later.csv",
        )
    )
    drain(service)
    asyncio.run(demo._seed_training_dataset())
    drain(service)
    assert download(service)[1]["id"] == later["id"]
    assert len(asyncio.run(service.list_versions())) == 3
