import logging
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from gateway.app import Settings, create_app


def test_settings_use_writable_vercel_paths(monkeypatch) -> None:
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.delenv("TEMP_DIR", raising=False)
    monkeypatch.delenv("OUTPUT_DIR", raising=False)

    settings = Settings.from_env()

    assert settings.temp_dir == Path("/tmp/mineru-gateway-tmp")
    assert settings.output_dir == Path("/tmp/mineru-gateway-output")


def test_demo_ui_is_served_at_root(tmp_path: Path) -> None:
    app = create_app(
        Settings(temp_dir=tmp_path / "temp", output_dir=tmp_path / "output"),
        httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    with TestClient(app) as client:
        response = client.get("/")
        alias = client.get("/index.html")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "MinerU | Document studio" in response.text
    assert "/api/mineru/convert" in response.text
    assert alias.status_code == 200
    assert alias.text == response.text


def test_vercel_public_ui_matches_gateway_ui() -> None:
    root = Path(__file__).resolve().parents[2]
    gateway_ui = (root / "gateway" / "index.html").read_text(encoding="utf-8")
    public_ui = (root / "public" / "index.html").read_text(encoding="utf-8")
    assert public_ui == gateway_ui
    assert "MinerU | Document studio" in public_ui


def test_ui_remains_available_without_mineru_configuration(tmp_path: Path) -> None:
    config_error = "Set MINERU_API_URL to the public HTTPS base URL of your MinerU service."
    app = create_app(
        Settings(
            mineru_config_error=config_error,
            temp_dir=tmp_path / "temp",
            output_dir=tmp_path / "output",
        ),
        httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    with TestClient(app) as client:
        page = client.get("/")
        health = client.get("/health")
        conversion = client.post(
            "/api/mineru/convert",
            files={"file": ("report.pdf", b"pdf bytes", "application/pdf")},
        )

    assert page.status_code == 200
    assert health.status_code == 503
    assert health.json()["detail"] == config_error
    assert conversion.status_code == 503
    assert conversion.json()["detail"] == config_error


def test_convert_runs_v1_flow_and_returns_markdown_and_metadata(
    tmp_path: Path, caplog
) -> None:
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    uploaded = {"body": b""}

    def mineru_api(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/uploads":
            return httpx.Response(
                200,
                json={
                    "id": "upload-1",
                    "status": "pending",
                    "upload_url": "/v1/uploads/upload-1/content",
                    "upload_headers": {},
                },
            )
        if request.method == "PUT" and request.url.path == "/v1/uploads/upload-1/content":
            uploaded["body"] = request.content
            return httpx.Response(200)
        if request.method == "POST" and request.url.path == "/v1/uploads/upload-1/complete":
            return httpx.Response(200, json={"file": {"id": "file-1"}})
        if request.method == "POST" and request.url.path == "/v1/parse/jobs":
            assert request.read()
            return httpx.Response(200, json={"job_id": "job-1", "status": "queued"})
        if request.method == "GET" and request.url.path == "/v1/parse/jobs/job-1":
            return httpx.Response(
                200,
                json={
                    "job_id": "job-1",
                    "status": "completed",
                    "tier": "flash",
                    "files": [
                        {
                            "status": "completed",
                            "output_files": {
                                "markdown": {"file_id": "markdown-1"},
                                "structured_content": {"file_id": "structured-1"},
                            },
                        }
                    ],
                },
            )
        if request.method == "GET" and request.url.path == "/v1/files/markdown-1/content":
            return httpx.Response(200, content=b"# Parsed document\n")
        if request.method == "GET" and request.url.path == "/v1/files/structured-1/content":
            return httpx.Response(
                200,
                json={
                    "metadata": {"file_suffix": "pdf"},
                    "extensions": {"mineru": {"tier": "flash"}},
                    "pages": [],
                },
            )
        if request.method == "GET" and request.url.path == "/v1/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404)

    settings = Settings(
        mineru_api_url="http://mineru.test",
        request_timeout_seconds=2,
        processing_timeout_seconds=10,
        max_file_size_mb=1,
        poll_interval_seconds=0,
        temp_dir=tmp_path / "temp",
        output_dir=tmp_path / "output",
    )
    app = create_app(settings, httpx.MockTransport(mineru_api))

    with TestClient(app) as client:
        response = client.post(
            "/api/mineru/convert",
            files={"file": ("report.pdf", b"pdf bytes", "application/pdf")},
            data={"tier": "flash"},
        )

    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "completed"
    assert result["markdown"] == "# Parsed document\n"
    assert result["metadata"]["mineru"]["document"]["file_suffix"] == "pdf"
    assert result["metadata"]["mineru"]["tier"] == "flash"
    assert result["structured_content"] == {
        "metadata": {"file_suffix": "pdf"},
        "extensions": {"mineru": {"tier": "flash"}},
        "pages": [],
    }
    assert "Received file 'report.pdf' (tier=flash)" in caplog.text
    assert "Staged file 'report.pdf' (9 bytes)" in caplog.text
    assert "Starting MinerU processing for 'report.pdf'" in caplog.text
    assert "MinerU finished 'report.pdf' (job_id=job-1, status=completed" in caplog.text
    assert "artifacts" not in result["metadata"]
    assert "Processed file 'report.pdf'" in caplog.text
    assert uploaded["body"] == b"pdf bytes"
    assert not (tmp_path / "output").exists()


def test_logs_mineru_connection_errors(tmp_path: Path, caplog) -> None:
    caplog.set_level(logging.WARNING, logger="uvicorn.error")

    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    app = create_app(
        Settings(temp_dir=tmp_path / "temp", output_dir=tmp_path / "output"),
        httpx.MockTransport(unavailable),
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/mineru/convert",
            files={"file": ("report.pdf", b"pdf bytes", "application/pdf")},
        )

    assert response.status_code == 502
    assert response.json()["detail"] == "MinerU service is unavailable"
    assert "MinerU request failed (POST /v1/uploads): ConnectError: connection refused" in caplog.text


def test_rejects_unsupported_extension(tmp_path: Path) -> None:
    app = create_app(
        Settings(temp_dir=tmp_path / "temp", output_dir=tmp_path / "output"),
        httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/mineru/convert",
            files={"file": ("script.exe", b"not allowed", "application/octet-stream")},
        )
    assert response.status_code == 415


def test_rejects_files_over_limit(tmp_path: Path) -> None:
    app = create_app(
        Settings(max_file_size_mb=0, temp_dir=tmp_path / "temp", output_dir=tmp_path / "output"),
        httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/mineru/convert",
            files={"file": ("document.pdf", b"x", "application/pdf")},
        )
    assert response.status_code == 413