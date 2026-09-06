from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.projects import ExportProjectCatalog


def test_create_project_makes_safe_namespace_and_lists_it(tmp_path: Path) -> None:
    media = tmp_path / "media"
    client = TestClient(create_app(media))

    assert client.get("/api/projects").json() == {"projects": []}
    response = client.post("/api/projects", json={"project_name": "New Project"})

    assert response.status_code == 201
    assert response.json() == {"project_name": "New Project", "project_slug": "new_project"}
    assert (media / "exports" / "new_project").is_dir()
    assert (media / "exports" / "new_project" / ".project.json").is_file()
    assert client.get("/api/projects").json()["projects"] == [response.json()]


def test_existing_project_slug_cannot_be_created_twice(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "media"))
    assert client.post("/api/projects", json={"project_name": "Claru Pilot"}).status_code == 201
    duplicate = client.post("/api/projects", json={"project_name": "Claru / Pilot"})
    assert duplicate.status_code == 409
    assert "select it from the list" in duplicate.json()["detail"]


def test_project_catalog_ignores_unsafe_folders_and_symlinks(tmp_path: Path) -> None:
    exports = tmp_path / "exports"
    exports.mkdir()
    (exports / "valid_project").mkdir()
    (exports / "Bad Project").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (exports / "linked_project").symlink_to(outside, target_is_directory=True)

    assert ExportProjectCatalog(exports).list() == [
        {"project_name": "Valid Project", "project_slug": "valid_project"}
    ]


def test_traversal_style_project_name_stays_inside_exports(tmp_path: Path) -> None:
    exports = tmp_path / "exports"
    project = ExportProjectCatalog(exports).ensure("../Claru", require_new=True)
    assert project["project_slug"] == "claru"
    assert (exports / "claru").is_dir()
    assert not (tmp_path / "Claru").exists()
