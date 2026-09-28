from dataclasses import replace
import os
from pathlib import Path
from urllib.parse import urlparse

vercel_env = os.getenv("VERCEL_ENV")
if os.getenv("VERCEL") == "1" and vercel_env in {"preview", "production"}:
    mineru_url = os.getenv("MINERU_API_URL", "").strip()
    parsed_mineru_url = urlparse(mineru_url)
    if parsed_mineru_url.scheme != "https" or not parsed_mineru_url.netloc:
        raise RuntimeError("Set MINERU_API_URL to the public HTTPS base URL of your MinerU service.")
    if parsed_mineru_url.path.rstrip("/").endswith("/v1"):
        raise RuntimeError("MINERU_API_URL must be the service base URL without the trailing /v1 path.")

os.environ.setdefault("TEMP_DIR", "/tmp/mineru-gateway-tmp")
os.environ.setdefault("OUTPUT_DIR", "/tmp/mineru-gateway-output")

from gateway.app import Settings, create_app

settings = Settings.from_env()
settings = replace(
    settings,
    temp_dir=Path("/tmp/mineru-gateway-tmp"),
    output_dir=Path("/tmp/mineru-gateway-output"),
    max_file_size_mb=min(settings.max_file_size_mb, 4),
    processing_timeout_seconds=min(settings.processing_timeout_seconds, 240),
    max_response_bytes=4_000_000,
)

app = create_app(settings)