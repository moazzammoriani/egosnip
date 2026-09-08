from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_media_workspace_is_hidden_until_a_project_is_chosen() -> None:
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    javascript = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert 'id="createProjectButton"' in html
    assert '>New Project</button>' in html
    assert 'id="sourceTools" class="source-tools" hidden' in html
    assert 'id="uploadPanel" class="upload-panel panel" tabindex="0" hidden' in html
    assert 'id="emptyState" class="empty panel" hidden' in html
    assert 'id="workspace" hidden' in html
    assert 'id="shortcutFooter" hidden' in html
    assert not javascript.rstrip().endswith("loadFiles();")
    assert "async function activateProject(selected)" in javascript


def test_frontend_uses_egosnip_branding() -> None:
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert "<title>EgoSnip</title>" in html
    assert "<h1>EgoSnip</h1>" in html


def test_upload_reports_server_side_validation_after_transfer() -> None:
    javascript = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert "validating video and GoPro identity" in javascript
