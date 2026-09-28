from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path


ROOT = Path(__file__).resolve().parent
MINERU_PYTHON = ROOT / ".venv-mineru" / "Scripts" / "python.exe"
GATEWAY_PYTHON = ROOT / ".venv-api" / "Scripts" / "python.exe"


def load_env() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ[name.strip()] = value


def run_checked(command: list[str], label: str) -> None:
    print(f"\n{label}...", flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def ensure_environment(python: Path, packages: tuple[str, ...], label: str) -> bool:
    if python.exists():
        check = subprocess.run(
            [str(python), "-c", ";".join(f"import {package}" for package in packages)],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if check.returncode == 0:
            return False
        return True

    launcher = shutil.which("py")
    if not launcher:
        raise RuntimeError("Python launcher 'py' was not found. Install Python 3.13 and try again.")
    run_checked([launcher, "-3.13", "-m", "venv", str(python.parent.parent)], f"Creating {label} environment")
    return True


def install_dependencies() -> None:
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv is required for first-time setup. Install it with: py -m pip install uv")
    if not (ROOT / "MinerU" / "pyproject.toml").exists():
        raise RuntimeError("MinerU source checkout is missing from the MinerU folder.")

    if ensure_environment(MINERU_PYTHON, ("mineru",), "MinerU"):
        run_checked(
            [uv, "pip", "install", "--python", str(MINERU_PYTHON), "--editable", str(ROOT / "MinerU")],
            "Installing MinerU from the local source",
        )
    if ensure_environment(GATEWAY_PYTHON, ("fastapi", "httpx", "uvicorn"), "gateway"):
        run_checked(
            [
                uv,
                "pip",
                "install",
                "--python",
                str(GATEWAY_PYTHON),
                "--requirements",
                str(ROOT / "gateway" / "requirements.txt"),
            ],
            "Installing gateway dependencies",
        )


def mineru_url() -> str:
    configured_url = os.getenv("MINERU_API_URL")
    if configured_url:
        return configured_url.rstrip("/")
    port = os.getenv("MINERU_PORT", "8000")
    return f"http://127.0.0.1:{port}"


def wait_for_mineru(process: subprocess.Popen[bytes], base_url: str) -> None:
    timeout = int(os.getenv("MINERU_STARTUP_TIMEOUT_SECONDS", "900"))
    deadline = time.monotonic() + timeout
    headers = {}
    api_key = os.getenv("MINERU_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    print(f"Waiting for MinerU at {base_url} (first startup can take several minutes)...", flush=True)
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"MinerU exited during startup (exit code {process.returncode}).")
        request = urllib.request.Request(f"{base_url}/v1/health", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError):
            time.sleep(2)
    raise TimeoutError(f"MinerU did not become healthy within {timeout} seconds.")


def stop_process(process: subprocess.Popen[bytes] | None, name: str) -> None:
    if process is None or process.poll() is not None:
        return
    print(f"Stopping {name}...", flush=True)
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def main() -> None:
    os.chdir(ROOT)
    load_env()
    install_dependencies()

    mineru_port = os.getenv("MINERU_PORT", "8000")
    mineru_host = os.getenv("MINERU_HOST", "127.0.0.1")
    upload_dir = Path(os.getenv("MINERU_UPLOAD_DIR", "data/mineru-uploads"))
    if not upload_dir.is_absolute():
        upload_dir = ROOT / upload_dir
    upload_dir.mkdir(parents=True, exist_ok=True)

    mineru_args = [
        str(MINERU_PYTHON),
        "-m",
        "mineru.kit.main",
        "api-server",
        "--host",
        mineru_host,
        "--port",
        mineru_port,
        "--tier",
        os.getenv("MINERU_TIER", "standard"),
        "--concurrency",
        os.getenv("MINERU_CONCURRENCY", "1"),
        "--upload-dir",
        str(upload_dir),
        "--log-level",
        os.getenv("MINERU_LOG_LEVEL", "info"),
    ]
    if os.getenv("MINERU_API_KEY"):
        mineru_args.extend(["--api-key", os.environ["MINERU_API_KEY"]])
    if os.getenv("MINERU_PRELOAD_MODELS", "true").lower() == "true":
        mineru_args.append("--preload-models")

    mineru_process: subprocess.Popen[bytes] | None = None
    gateway_process: subprocess.Popen[bytes] | None = None
    try:
        print("Starting MinerU API...", flush=True)
        mineru_process = subprocess.Popen(mineru_args, cwd=ROOT)
        base_url = mineru_url()
        wait_for_mineru(mineru_process, base_url)

        gateway_host = os.getenv("API_BIND_HOST", "127.0.0.1")
        gateway_port = os.getenv("API_PORT", "8080")
        gateway_env = os.environ.copy()
        gateway_env["MINERU_API_URL"] = base_url
        gateway_args = [
            str(GATEWAY_PYTHON),
            "-m",
            "uvicorn",
            "app:app",
            "--app-dir",
            str(ROOT / "gateway"),
            "--host",
            gateway_host,
            "--port",
            gateway_port,
        ]
        print(f"Starting gateway at http://{gateway_host}:{gateway_port}", flush=True)
        gateway_process = subprocess.Popen(gateway_args, cwd=ROOT, env=gateway_env)

        demo_url = f"http://127.0.0.1:{gateway_port}/"
        docs_url = f"http://127.0.0.1:{gateway_port}/docs"
        print(f"Demo UI: {demo_url}", flush=True)
        print(f"API docs: {docs_url}", flush=True)
        print("Press Ctrl+C to stop both services.", flush=True)
        webbrowser.open(demo_url)

        while True:
            if mineru_process.poll() is not None:
                raise RuntimeError(f"MinerU stopped (exit code {mineru_process.returncode}).")
            if gateway_process.poll() is not None:
                raise RuntimeError(f"Gateway stopped (exit code {gateway_process.returncode}).")
            time.sleep(1)
    finally:
        stop_process(gateway_process, "gateway")
        stop_process(mineru_process, "MinerU")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nServices stopped.", flush=True)
    except Exception as error:
        print(f"\nStartup failed: {error}", file=sys.stderr, flush=True)
        if os.name == "nt" and sys.stdin.isatty():
            input("Press Enter to close this window...")
        raise