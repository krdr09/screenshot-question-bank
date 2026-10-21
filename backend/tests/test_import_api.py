from __future__ import annotations

from io import BytesIO
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.main import create_app
from app.models import Course, ImportBatch, SourceImage
from app.services import import_service


def png_bytes(color: tuple[int, int, int] = (20, 90, 60)) -> bytes:
    output = BytesIO()
    Image.new("RGB", (32, 48), color).save(output, format="PNG")
    return output.getvalue()


def make_client(
    tmp_path: Path, *, max_import_bytes: int = 2 * 1024 * 1024 * 1024
) -> TestClient:
    settings = Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        auto_create_schema=True,
        start_background_worker=False,
        frontend_dist=tmp_path / "missing-dist",
        max_import_bytes=max_import_bytes,
        _env_file=None,
    )
    return TestClient(create_app(settings))


def test_nested_folder_preview_uses_first_directory_as_course(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.post(
            "/api/import-jobs/preview",
            json={
                "root_name": "待导入题库",
                "files": [
                    {
                        "relative_path": "待导入题库/运筹学/第一章/作业1/a.png",
                        "size": 120,
                    },
                    {
                        "relative_path": "待导入题库/运筹学/第一章/作业1/b.jpg",
                        "size": 130,
                    },
                    {
                        "relative_path": "待导入题库/其他课程/作业2/c.webp",
                        "size": 140,
                    },
                ],
            },
        )

    assert response.status_code == 200
    assert response.json()["mappings"] == [
        {
            "course_name": "运筹学",
            "category_path": "第一章",
            "batch_name": "作业1",
            "source_relative_path": "运筹学/第一章/作业1",
            "file_count": 2,
        },
        {
            "course_name": "其他课程",
            "category_path": "",
            "batch_name": "作业2",
            "source_relative_path": "其他课程/作业2",
            "file_count": 1,
        },
    ]


def test_flat_folder_preview_requires_explicit_course_and_batch(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        missing = client.post(
            "/api/import-jobs/preview",
            json={
                "root_name": "截图",
                "files": [{"relative_path": "截图/a.png", "size": 100}],
            },
        )
        complete = client.post(
            "/api/import-jobs/preview",
            json={
                "root_name": "截图",
                "flat_course_name": "运筹学",
                "flat_batch_name": "临时作业",
                "files": [{"relative_path": "截图/a.png", "size": 100}],
            },
        )

    assert missing.status_code == 422
    assert "课程名和批次名" in missing.json()["detail"]
    assert complete.status_code == 200
    assert complete.json()["mappings"][0]["course_name"] == "运筹学"
    assert complete.json()["mappings"][0]["batch_name"] == "临时作业"


def create_job(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/api/import-jobs",
        json={
            "course_name": "运筹学",
            "category_path": "第一章",
            "batch_name": "作业1",
            "source_relative_path": "运筹学/第一章/作业1",
        },
    )
    assert response.status_code == 201
    return response.json()


def upload_one(
    client: TestClient,
    job_id: str,
    name: str,
    relative_path: str,
    content: bytes,
):
    return client.post(
        f"/api/import-jobs/{job_id}/files",
        files=[
            ("files", (name, content, "image/png")),
            ("relative_paths", (None, relative_path)),
        ],
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [("course_name", "   "), ("batch_name", "\t")],
)
def test_import_job_rejects_whitespace_only_required_names(
    tmp_path: Path, field: str, value: str
) -> None:
    with make_client(tmp_path) as client:
        payload = {
            "course_name": "运筹学",
            "category_path": "第一章",
            "batch_name": "作业1",
            "source_relative_path": "运筹学/第一章/作业1",
        }
        payload[field] = value
        response = client.post("/api/import-jobs", json=payload)
        with client.app.state.database.session_factory() as session:
            courses = session.scalar(select(func.count()).select_from(Course))
            batches = session.scalar(select(func.count()).select_from(ImportBatch))

    assert response.status_code == 422
    assert courses == 0
    assert batches == 0


def test_import_job_uses_uuid_and_persists_classification(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        first = create_job(client)
        second = create_job(client)
        fetched = client.get(f"/api/import-jobs/{first['id']}")

    assert UUID(str(first["id"])) != UUID(str(second["id"]))
    assert fetched.status_code == 200
    assert fetched.json()["course_name"] == "运筹学"
    assert fetched.json()["category_path"] == "第一章"
    assert fetched.json()["batch_name"] == "作业1"
    assert fetched.json()["status"] == "created"


def test_upload_validates_image_and_stores_only_relative_safe_path(tmp_path: Path) -> None:
    content = png_bytes()
    with make_client(tmp_path) as client:
        job = create_job(client)
        response = client.post(
            f"/api/import-jobs/{job['id']}/files",
            files=[
                ("files", ("课堂 截图.png", content, "image/png")),
                (
                    "relative_paths",
                    (None, "运筹学/第一章/作业1/课堂 截图.png"),
                ),
            ],
        )

    assert response.status_code == 200
    image = response.json()["images"][0]
    assert image["sha256"]
    assert image["width"] == 32
    assert image["height"] == 48
    assert image["relative_path"].startswith(f"imports/{job['id']}/")
    assert "课堂 截图" not in image["relative_path"]
    assert not Path(image["relative_path"]).is_absolute()
    assert (tmp_path / "data" / image["relative_path"]).read_bytes() == content


def test_upload_rejects_batch_after_processing_is_queued(tmp_path: Path) -> None:
    first_content = png_bytes((10, 20, 30))
    late_content = png_bytes((40, 50, 60))
    with make_client(tmp_path) as client:
        job = create_job(client)
        first = client.post(
            f"/api/import-jobs/{job['id']}/files",
            files=[
                ("files", ("first.png", first_content, "image/png")),
                ("relative_paths", (None, "作业1/first.png")),
            ],
        )
        queued = client.post(f"/api/import-jobs/{job['id']}/process")
        late = client.post(
            f"/api/import-jobs/{job['id']}/files",
            files=[
                ("files", ("late.png", late_content, "image/png")),
                ("relative_paths", (None, "作业1/late.png")),
            ],
        )
        state = client.get(f"/api/import-jobs/{job['id']}").json()
        files = list((tmp_path / "data" / "imports" / str(job["id"])).glob("*"))

    assert first.status_code == 200
    assert queued.status_code == 202
    assert late.status_code == 409
    assert "当前状态不允许上传" in late.json()["detail"]
    assert state["status"] == "queued"
    assert state["uploaded_count"] == 1
    assert len(state["images"]) == 1
    assert len(files) == 1


def test_upload_rejects_path_traversal_and_non_image_content(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        job = create_job(client)
        traversal = client.post(
            f"/api/import-jobs/{job['id']}/files",
            files=[
                ("files", ("safe.png", png_bytes(), "image/png")),
                ("relative_paths", (None, "../safe.png")),
            ],
        )
        invalid = client.post(
            f"/api/import-jobs/{job['id']}/files",
            files=[
                ("files", ("fake.png", b"MZ-not-an-image", "image/png")),
                ("relative_paths", (None, "运筹学/作业1/fake.png")),
            ],
        )

    assert traversal.status_code == 400
    assert "路径" in traversal.json()["detail"]
    assert invalid.status_code == 400
    assert "图片" in invalid.json()["detail"]
    assert not list((tmp_path / "data" / "imports").rglob("*.png"))


def test_upload_rejects_extension_that_disagrees_with_image_format(
    tmp_path: Path,
) -> None:
    with make_client(tmp_path) as client:
        job = create_job(client)
        response = client.post(
            f"/api/import-jobs/{job['id']}/files",
            files=[
                ("files", ("伪装图片.jpg", png_bytes(), "image/jpeg")),
                ("relative_paths", (None, "运筹学/作业1/伪装图片.jpg")),
            ],
        )

    assert response.status_code == 400
    assert "扩展名与实际格式不一致" in response.json()["detail"]
    assert not list((tmp_path / "data" / "imports").rglob("*.*"))


def test_same_sha256_is_recorded_as_duplicate_without_second_copy(tmp_path: Path) -> None:
    content = png_bytes((120, 30, 40))
    with make_client(tmp_path) as client:
        job = create_job(client)
        response = client.post(
            f"/api/import-jobs/{job['id']}/files",
            files=[
                ("files", ("a.png", content, "image/png")),
                ("files", ("b.png", content, "image/png")),
                ("relative_paths", (None, "运筹学/第一章/作业1/a.png")),
                ("relative_paths", (None, "运筹学/第一章/作业1/b.png")),
            ],
        )

    assert response.status_code == 200
    body = response.json()
    assert body["uploaded_count"] == 2
    assert body["duplicate_count"] == 1
    assert body["images"][1]["duplicate_of_id"] == body["images"][0]["id"]
    assert body["images"][1]["relative_path"] == body["images"][0]["relative_path"]
    assert len(list((tmp_path / "data" / "imports").rglob("*.png"))) == 1


def test_same_source_path_and_hash_is_idempotent(tmp_path: Path) -> None:
    content = png_bytes((80, 90, 100))
    with make_client(tmp_path) as client:
        job = create_job(client)
        first = upload_one(client, job["id"], "first.png", "作业1/same.png", content)
        retry = upload_one(client, job["id"], "first.png", "作业1/same.png", content)
        state = client.get(f"/api/import-jobs/{job['id']}").json()
        files = list((tmp_path / "data" / "imports" / str(job["id"])).glob("*"))

    assert first.status_code == 200
    assert retry.status_code == 200
    assert retry.json()["uploaded_count"] == 0
    assert retry.json()["duplicate_count"] == 0
    assert retry.json()["images"][0]["id"] == first.json()["images"][0]["id"]
    assert state["uploaded_count"] == 1
    assert len(state["images"]) == 1
    assert len(files) == 1


def test_same_source_path_with_different_content_returns_conflict(
    tmp_path: Path,
) -> None:
    first_content = png_bytes((10, 20, 30))
    replacement = png_bytes((30, 20, 10))
    with make_client(tmp_path) as client:
        job = create_job(client)
        first = upload_one(
            client, job["id"], "first.png", "作业1/same.png", first_content
        )
        conflict = upload_one(
            client, job["id"], "first.png", "作业1/same.png", replacement
        )
        state = client.get(f"/api/import-jobs/{job['id']}").json()
        stored = tmp_path / "data" / first.json()["images"][0]["relative_path"]

    assert conflict.status_code == 409
    assert "相对路径已存在且内容不同" in conflict.json()["detail"]
    assert state["uploaded_count"] == 1
    assert len(state["images"]) == 1
    assert stored.read_bytes() == first_content


def test_upload_integrity_race_returns_conflict_and_removes_new_file(
    tmp_path: Path, monkeypatch
) -> None:
    with make_client(tmp_path) as client:
        job = create_job(client)
        original_flush = Session.flush
        raised = False

        def fail_import_flush(self, *args, **kwargs):
            nonlocal raised
            if not raised and any(
                isinstance(item, SourceImage) for item in self.new
            ):
                raised = True
                raise IntegrityError("INSERT", {}, Exception("unique race"))
            return original_flush(self, *args, **kwargs)

        monkeypatch.setattr(Session, "flush", fail_import_flush)
        response = upload_one(
            client, job["id"], "race.png", "作业1/race.png", png_bytes()
        )
        state = client.get(f"/api/import-jobs/{job['id']}").json()

    assert response.status_code == 409
    assert "并发冲突" in response.json()["detail"]
    assert state["uploaded_count"] == 0
    assert state["images"] == []
    assert not list((tmp_path / "data" / "imports").rglob("*.png"))


def test_upload_removes_partial_temporary_file_when_write_fails(
    tmp_path: Path, monkeypatch
) -> None:
    with make_client(tmp_path) as client:
        job = create_job(client)
        original_write_bytes = Path.write_bytes

        def fail_temporary_write(path: Path, data: bytes) -> int:
            if path.suffix == ".tmp":
                original_write_bytes(path, data[:1])
                raise OSError("temporary write failed")
            return original_write_bytes(path, data)

        monkeypatch.setattr(Path, "write_bytes", fail_temporary_write)
        with pytest.raises(OSError, match="temporary write failed"):
            upload_one(
                client,
                job["id"],
                "write-failure.png",
                "作业1/write-failure.png",
                png_bytes(),
            )
        state = client.get(f"/api/import-jobs/{job['id']}").json()
        files = [
            path.relative_to(tmp_path / "data" / "imports").as_posix()
            for path in (tmp_path / "data" / "imports").rglob("*")
            if path.is_file()
        ]

    assert state["status"] == "created"
    assert state["uploaded_count"] == 0
    assert state["duplicate_count"] == 0
    assert state["images"] == []
    assert files == []


def test_upload_removes_temporary_file_when_replace_fails(
    tmp_path: Path, monkeypatch
) -> None:
    with make_client(tmp_path) as client:
        job = create_job(client)

        def fail_replace(source, destination) -> None:
            raise OSError(f"replace failed: {source} -> {destination}")

        monkeypatch.setattr(import_service.os, "replace", fail_replace)
        with pytest.raises(OSError, match="replace failed"):
            upload_one(
                client,
                job["id"],
                "replace-failure.png",
                "作业1/replace-failure.png",
                png_bytes(),
            )
        state = client.get(f"/api/import-jobs/{job['id']}").json()
        files = [
            path.relative_to(tmp_path / "data" / "imports").as_posix()
            for path in (tmp_path / "data" / "imports").rglob("*")
            if path.is_file()
        ]

    assert state["status"] == "created"
    assert state["uploaded_count"] == 0
    assert state["duplicate_count"] == 0
    assert state["images"] == []
    assert files == []


def test_import_total_size_limit_accumulates_across_requests(tmp_path: Path) -> None:
    first_content = png_bytes((10, 20, 30))
    second_content = png_bytes((40, 50, 60))
    limit = len(first_content) + len(second_content) - 1

    with make_client(tmp_path, max_import_bytes=limit) as client:
        job = create_job(client)
        first = client.post(
            f"/api/import-jobs/{job['id']}/files",
            files=[
                ("files", ("first.png", first_content, "image/png")),
                ("relative_paths", (None, "first.png")),
            ],
        )
        second = client.post(
            f"/api/import-jobs/{job['id']}/files",
            files=[
                ("files", ("second.png", second_content, "image/png")),
                ("relative_paths", (None, "second.png")),
            ],
        )
        state = client.get(f"/api/import-jobs/{job['id']}").json()

    assert first.status_code == 200
    assert second.status_code == 400
    assert "导入图片总大小超过限制" in second.json()["detail"]
    assert state["uploaded_count"] == 1
    assert state["duplicate_count"] == 0
    assert len(state["images"]) == 1
    assert len(list((tmp_path / "data" / "imports" / str(job["id"])).glob("*"))) == 1


def test_import_total_size_limit_allows_exact_cumulative_boundary(tmp_path: Path) -> None:
    first_content = png_bytes((70, 80, 90))
    second_content = png_bytes((100, 110, 120))
    limit = len(first_content) + len(second_content)

    with make_client(tmp_path, max_import_bytes=limit) as client:
        job = create_job(client)
        first = client.post(
            f"/api/import-jobs/{job['id']}/files",
            files=[
                ("files", ("first.png", first_content, "image/png")),
                ("relative_paths", (None, "first.png")),
            ],
        )
        second = client.post(
            f"/api/import-jobs/{job['id']}/files",
            files=[
                ("files", ("second.png", second_content, "image/png")),
                ("relative_paths", (None, "second.png")),
            ],
        )
        state = client.get(f"/api/import-jobs/{job['id']}").json()

    assert first.status_code == 200
    assert second.status_code == 200
    assert state["uploaded_count"] == 2
    assert len(state["images"]) == 2


def test_import_total_size_limit_counts_duplicate_payloads(tmp_path: Path) -> None:
    content = png_bytes((130, 140, 150))
    limit = len(content) * 2 - 1

    with make_client(tmp_path, max_import_bytes=limit) as client:
        job = create_job(client)
        first = client.post(
            f"/api/import-jobs/{job['id']}/files",
            files=[
                ("files", ("first.png", content, "image/png")),
                ("relative_paths", (None, "first.png")),
            ],
        )
        duplicate = client.post(
            f"/api/import-jobs/{job['id']}/files",
            files=[
                ("files", ("duplicate.png", content, "image/png")),
                ("relative_paths", (None, "duplicate.png")),
            ],
        )
        state = client.get(f"/api/import-jobs/{job['id']}").json()

    assert first.status_code == 200
    assert duplicate.status_code == 400
    assert "导入图片总大小超过限制" in duplicate.json()["detail"]
    assert state["uploaded_count"] == 1
    assert state["duplicate_count"] == 0
    assert len(state["images"]) == 1
    assert len(list((tmp_path / "data" / "imports" / str(job["id"])).glob("*"))) == 1
