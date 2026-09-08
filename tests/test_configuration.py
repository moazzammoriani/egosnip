from pathlib import Path

from app.media import DEFAULT_MEDIA_DIR, configured_media_dir


def test_default_media_dir_is_project_media_not_working_directory(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.delenv("MEDIA_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    assert configured_media_dir() == DEFAULT_MEDIA_DIR
    assert DEFAULT_MEDIA_DIR.name == "media"
    assert (DEFAULT_MEDIA_DIR.parent / "pyproject.toml").is_file()


def test_media_dir_environment_and_explicit_value_override_default(
    tmp_path: Path, monkeypatch,
) -> None:
    environment_dir = tmp_path / "environment-media"
    explicit_dir = tmp_path / "explicit-media"
    monkeypatch.setenv("MEDIA_DIR", str(environment_dir))
    assert configured_media_dir() == environment_dir
    assert configured_media_dir(explicit_dir) == explicit_dir
