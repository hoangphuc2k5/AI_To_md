from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

logger = logging.getLogger("uvicorn.error")


@dataclass(frozen=True)
class Settings:
    mineru_api_url: str = "http://127.0.0.1:8000"
    mineru_api_key: str = ""
    request_timeout_seconds: float = 60
    processing_timeout_seconds: int = 1800
    max_file_size_mb: int = 50
    max_concurrent_requests: int = 1
    poll_interval_seconds: float = 1
    temp_dir: Path = Path("data/gateway-tmp")
    output_dir: Path = Path("data/output")
    include_structured_content: bool = True
    max_response_bytes: int | None = None
    cors_origins: tuple[str, ...] = ("http://localhost:3000", "http://localhost:5173")
    allowed_extensions: frozenset[str] = frozenset(
        {
            ".pdf",
            ".png",
            ".jpg",
            ".jpeg",
            ".bmp",
            ".tif",
            ".tiff",
            ".webp",
            ".gif",
            ".doc",
            ".docx",
            ".ppt",
            ".pptx",
            ".xls",
            ".xlsx",
            ".rtf",
            ".odt",
            ".ods",
            ".odp",
            ".epub",
            ".ofd",
            ".html",
            ".htm",
            ".mhtml",
            ".mht",
            ".csv",
            ".tsv",
        }
    )
    mineru_config_error: str | None = None

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            mineru_api_url=os.getenv("MINERU_API_URL", cls.mineru_api_url).rstrip("/"),
            mineru_api_key=os.getenv("MINERU_API_KEY", ""),
            request_timeout_seconds=float(os.getenv("REQUEST_TIMEOUT_SECONDS", "60")),
            processing_timeout_seconds=int(os.getenv("PROCESSING_TIMEOUT_SECONDS", "1800")),
            max_file_size_mb=int(os.getenv("MAX_FILE_SIZE_MB", "50")),
            max_concurrent_requests=int(os.getenv("MAX_CONCURRENT_REQUESTS", "1")),
            poll_interval_seconds=float(os.getenv("POLL_INTERVAL_SECONDS", "1")),
            temp_dir=Path(os.getenv("TEMP_DIR", "data/gateway-tmp")),
            output_dir=Path(os.getenv("OUTPUT_DIR", "data/output")),
            cors_origins=tuple(
                item.strip()
                for item in os.getenv(
                    "CORS_ORIGINS", "http://localhost:3000,http://localhost:5173"
                ).split(",")
                if item.strip()
            ),
        )


def _origin(url: str) -> tuple[str, str, int] | None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None
    return parsed.scheme.lower(), parsed.hostname.lower(), port


def _auth_headers(settings: Settings) -> dict[str, str]:
    if not settings.mineru_api_key:
        return {}
    return {"Authorization": f"Bearer {settings.mineru_api_key}"}


async def _json_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    settings: Settings,
    json_body: dict[str, Any] | None = None,
    content: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    request_headers = _auth_headers(settings)
    if headers:
        request_headers.update(headers)
    try:
        response = await client.request(
            method, url, json=json_body, content=content, headers=request_headers
        )
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="MinerU service request timed out") from exc
    except httpx.HTTPError as exc:
        logger.warning(
            "MinerU request failed (%s %s): %s: %s",
            method,
            urlparse(url).path,
            type(exc).__name__,
            exc,
        )
        raise HTTPException(status_code=502, detail="MinerU service is unavailable") from exc
    if response.is_error:
        raise HTTPException(
            status_code=502,
            detail=f"MinerU service returned HTTP {response.status_code}",
        )
    return response


async def _download_artifact(
    client: httpx.AsyncClient, file_id: str, settings: Settings
) -> bytes:
    url = f"{settings.mineru_api_url}/v1/files/{file_id}/content"
    try:
        response = await client.get(url, headers=_auth_headers(settings), follow_redirects=False)
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="MinerU output download timed out") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Could not download MinerU output") from exc
    if response.is_redirect:
        location = response.headers.get("location")
        download_url = urljoin(url, location or "")
        if _origin(download_url) is None:
            raise HTTPException(status_code=502, detail="MinerU returned an invalid artifact URL")
        # Signed object-store URLs are a different origin: never forward the API key.
        download_headers = _auth_headers(settings) if _origin(url) == _origin(download_url) else {}
        try:
            response = await client.get(
                download_url, headers=download_headers, follow_redirects=False
            )
        except httpx.TimeoutException as exc:
            raise HTTPException(status_code=504, detail="MinerU output download timed out") from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="Could not download MinerU output") from exc
    if response.is_error or response.is_redirect:
        raise HTTPException(status_code=502, detail="Could not download MinerU output")
    return response.content


async def _stage_upload(upload: UploadFile, settings: Settings) -> tuple[Path, int, str]:
    limit = settings.max_file_size_mb * 1024 * 1024
    digest = hashlib.sha256()
    size = 0
    suffix = Path(upload.filename or "document").suffix.lower()
    file_descriptor, temp_name = tempfile.mkstemp(
        prefix="mineru-", suffix=suffix, dir=settings.temp_dir
    )
    path = Path(temp_name)
    try:
        with os.fdopen(file_descriptor, "wb") as staged:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(status_code=413, detail="File exceeds the configured size limit")
                digest.update(chunk)
                staged.write(chunk)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()
    return path, size, digest.hexdigest()


async def _file_chunks(path: Path):
    with path.open("rb") as source:
        while chunk := await asyncio.to_thread(source.read, 1024 * 1024):
            yield chunk


async def _convert(
    client: httpx.AsyncClient,
    staged_path: Path,
    *,
    filename: str,
    size: int,
    sha256sum: str,
    tier: str,
    settings: Settings,
) -> tuple[str, dict[str, Any], dict[str, Any], int, str]:
    mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    upload_response = await _json_request(
        client,
        "POST",
        f"{settings.mineru_api_url}/v1/uploads",
        settings=settings,
        json_body={
            "filename": filename,
            "bytes": size,
            "mime_type": mime_type,
            "purpose": "parse",
            "sha256sum": sha256sum,
        },
    )
    upload_data = upload_response.json()
    if upload_data.get("status") == "completed":
        file_id = upload_data.get("file", {}).get("id")
    else:
        upload_id = upload_data.get("id")
        upload_url = upload_data.get("upload_url")
        upload_headers = upload_data.get("upload_headers", {})
        if not isinstance(upload_id, str) or not isinstance(upload_url, str):
            raise HTTPException(status_code=502, detail="MinerU returned an invalid upload response")
        if not isinstance(upload_headers, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in upload_headers.items()
        ):
            raise HTTPException(status_code=502, detail="MinerU returned invalid upload headers")
        absolute_upload_url = urljoin(f"{settings.mineru_api_url}/", upload_url)
        if _origin(absolute_upload_url) is None:
            raise HTTPException(status_code=502, detail="MinerU returned an invalid upload URL")
        put_headers = dict(upload_headers)
        if _origin(absolute_upload_url) == _origin(settings.mineru_api_url):
            put_headers.update(_auth_headers(settings))
        try:
            response = await client.put(
                absolute_upload_url,
                content=_file_chunks(staged_path),
                headers=put_headers,
                follow_redirects=False,
            )
        except httpx.TimeoutException as exc:
            raise HTTPException(status_code=504, detail="MinerU upload timed out") from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="Could not upload file to MinerU") from exc
        if response.is_error:
            raise HTTPException(status_code=502, detail="Could not upload file to MinerU")
        completed = await _json_request(
            client,
            "POST",
            f"{settings.mineru_api_url}/v1/uploads/{upload_id}/complete",
            settings=settings,
        )
        file_id = completed.json().get("file", {}).get("id")

    if not isinstance(file_id, str) or not file_id:
        raise HTTPException(status_code=502, detail="MinerU did not return an uploaded file id")

    submitted = await _json_request(
        client,
        "POST",
        f"{settings.mineru_api_url}/v1/parse/jobs",
        settings=settings,
        json_body={
            "files": [{"source": {"type": "file_id", "file_id": file_id}}],
            "tier": tier,
            "output_formats": [
                "markdown",
                *( ["structured_content"] if settings.include_structured_content else [] ),
            ],
        },
    )
    job = submitted.json()
    job_id = job.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise HTTPException(status_code=502, detail="MinerU did not return a parse job id")

    deadline = time.monotonic() + settings.processing_timeout_seconds
    while job.get("status") not in {"completed", "partial", "failed", "canceled"}:
        if time.monotonic() >= deadline:
            raise HTTPException(status_code=504, detail="Document processing timed out")
        await asyncio.sleep(settings.poll_interval_seconds)
        polled = await _json_request(
            client,
            "GET",
            f"{settings.mineru_api_url}/v1/parse/jobs/{job_id}",
            settings=settings,
        )
        job = polled.json()

    if job.get("status") != "completed":
        files = job.get("files") or []
        error = files[0].get("error") if files and isinstance(files[0], dict) else None
        raise HTTPException(
            status_code=502,
            detail={"message": "MinerU could not complete this document", "status": job.get("status"), "error": error},
        )

    files = job.get("files") or []
    output_files = files[0].get("output_files", {}) if files and isinstance(files[0], dict) else {}
    markdown_ref = output_files.get("markdown")
    structured_ref = output_files.get("structured_content")
    if not isinstance(markdown_ref, dict):
        raise HTTPException(status_code=502, detail="MinerU response is missing required output artifacts")
    markdown_bytes = await _download_artifact(client, markdown_ref.get("file_id", ""), settings)
    structured_content: dict[str, Any] = {}
    if settings.include_structured_content:
        if not isinstance(structured_ref, dict):
            raise HTTPException(status_code=502, detail="MinerU response is missing structured content")
        structured_bytes = await _download_artifact(
            client, structured_ref.get("file_id", ""), settings
        )
        try:
            structured_content = json.loads(structured_bytes)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=502, detail="MinerU returned malformed structured output") from exc
    try:
        markdown = markdown_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=502, detail="MinerU returned malformed output") from exc
    if not isinstance(structured_content, dict):
        raise HTTPException(status_code=502, detail="MinerU structured output is not a JSON object")
    return markdown, structured_content, job, size, mime_type


def create_app(
    settings: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    config = settings or Settings.from_env()
    config.temp_dir.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        limits = httpx.Limits(max_connections=max(4, config.max_concurrent_requests * 4))
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(config.request_timeout_seconds),
            limits=limits,
            transport=transport,
        ) as client:
            app.state.mineru_client = client
            app.state.request_semaphore = asyncio.Semaphore(config.max_concurrent_requests)
            yield

    app = FastAPI(title="MinerU Gateway", version="1.0.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(config.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Authorization"],
    )

    def ensure_mineru_configured() -> None:
        if config.mineru_config_error:
            raise HTTPException(status_code=503, detail=config.mineru_config_error)

    @app.get("/", include_in_schema=False)
    async def demo_ui() -> FileResponse:
        return FileResponse(Path(__file__).with_name("index.html"), media_type="text/html")

    @app.get("/health")
    async def health() -> dict[str, str]:
        ensure_mineru_configured()
        response = await _json_request(
            app.state.mineru_client,
            "GET",
            f"{config.mineru_api_url}/v1/health",
            settings=config,
        )
        return {"status": "ok", "mineru": response.json().get("status", "ok")}

    @app.post("/api/mineru/convert")
    async def convert(
        file: UploadFile = File(...),
        tier: str = Form("standard"),
    ) -> dict[str, Any]:
        ensure_mineru_configured()
        filename = Path(file.filename or "").name
        suffix = Path(filename).suffix.lower()
        logger.info("Received file '%s' (tier=%s)", filename, tier)
        if suffix not in config.allowed_extensions:
            await file.close()
            logger.warning("Rejected file '%s': unsupported extension '%s'", filename, suffix)
            raise HTTPException(status_code=415, detail=f"Unsupported file extension: {suffix or '(none)'}")
        if tier not in {"flash", "basic", "standard", "advanced"}:
            await file.close()
            logger.warning("Rejected file '%s': unsupported tier '%s'", filename, tier)
            raise HTTPException(status_code=422, detail="tier must be flash, basic, standard, or advanced")
        content_length = file.headers.get("content-length")
        if content_length and content_length.isdigit() and int(content_length) > config.max_file_size_mb * 1024 * 1024:
            await file.close()
            logger.warning("Rejected file '%s': content length exceeds the configured limit", filename)
            raise HTTPException(status_code=413, detail="File exceeds the configured size limit")

        staged_path: Path | None = None
        try:
            async with app.state.request_semaphore:
                staged_path, size, digest = await _stage_upload(file, config)
                logger.info("Staged file '%s' (%d bytes)", filename, size)
                started = time.monotonic()
                logger.info("Starting MinerU processing for '%s' (tier=%s)", filename, tier)
                try:
                    async with asyncio.timeout(config.processing_timeout_seconds):
                        markdown, structured_content, job, size, mime_type = await _convert(
                            app.state.mineru_client,
                            staged_path,
                            filename=filename,
                            size=size,
                            sha256sum=digest,
                            tier=tier,
                            settings=config,
                        )
                except TimeoutError as exc:
                    raise HTTPException(status_code=504, detail="Document processing timed out") from exc
                except httpx.TimeoutException as exc:
                    raise HTTPException(status_code=504, detail="MinerU service request timed out") from exc
                except httpx.HTTPError as exc:
                    raise HTTPException(status_code=502, detail="MinerU service is unavailable") from exc
                logger.info(
                    "MinerU finished '%s' (job_id=%s, status=%s, duration_ms=%d)",
                    filename,
                    job.get("job_id"),
                    job.get("status"),
                    round((time.monotonic() - started) * 1000),
                )

                metadata = {
                    "source": {
                        "filename": filename,
                        "mime_type": mime_type,
                        "size_bytes": size,
                        "sha256": digest,
                    },
                    "mineru": {
                        "job_id": job.get("job_id"),
                        "status": job.get("status"),
                        "tier": job.get("tier", tier),
                        "created_at": job.get("created_at"),
                        "finished_at": job.get("finished_at"),
                        "document": structured_content.get("metadata", {}),
                        "extensions": structured_content.get("extensions", {}),
                        "is_full_document": structured_content.get("is_full_document"),
                    },
                    "processing_time_ms": round((time.monotonic() - started) * 1000),
                }
                result = {
                    "status": "completed",
                    "markdown": markdown,
                    "metadata": metadata,
                }
                if config.include_structured_content:
                    result["structured_content"] = structured_content
                response_size = len(
                    json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                )
                if config.max_response_bytes is not None and response_size > config.max_response_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail="Conversion output exceeds this deployment's response size limit",
                    )

                logger.info("Processed file '%s'", filename)
                return result
        except HTTPException as exc:
            logger.warning(
                "Processing failed for '%s' (HTTP %d): %s",
                filename,
                exc.status_code,
                exc.detail,
            )
            raise
        except Exception:
            logger.exception("Unexpected error processing file '%s'", filename)
            raise
        finally:
            if staged_path is not None:
                staged_path.unlink(missing_ok=True)

    return app


app = create_app()