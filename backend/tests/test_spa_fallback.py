from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def _create_client(tmp_path: Path) -> TestClient:
    frontend_dist = tmp_path / "dist"
    frontend_dist.mkdir()
    (frontend_dist / "index.html").write_text(
        '<!doctype html><div id="root">production-spa</div>',
        encoding="utf-8",
    )
    assets = frontend_dist / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("window.productionBundle = true;", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        auto_create_schema=True,
        start_background_worker=False,
        frontend_dist=frontend_dist,
        _env_file=None,
    )
    return TestClient(create_app(settings))


def test_missing_api_is_json_404_instead_of_spa_index(tmp_path: Path) -> None:
    with _create_client(tmp_path) as client:
        response = client.get("/api/does-not-exist")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert "production-spa" not in response.text


def test_non_api_route_falls_back_to_production_index(tmp_path: Path) -> None:
    with _create_client(tmp_path) as client:
        response = client.get("/practice/history")

    assert response.status_code == 200
    assert "production-spa" in response.text


def test_static_assets_are_served_and_missing_assets_stay_404(tmp_path: Path) -> None:
    with _create_client(tmp_path) as client:
        existing = client.get("/assets/app.js")
        missing = client.get("/assets/missing.js")

    assert existing.status_code == 200
    assert existing.text == "window.productionBundle = true;"
    assert missing.status_code == 404
    assert "production-spa" not in missing.text


def test_frontend_fallback_never_reads_outside_dist(tmp_path: Path) -> None:
    (tmp_path / "private.txt").write_text("private-outside-dist", encoding="utf-8")

    with _create_client(tmp_path) as client:
        response = client.get("/..%2Fprivate.txt")

    assert "private-outside-dist" not in response.text
