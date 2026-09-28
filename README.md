# MinerU API Gateway

This workspace keeps the unmodified upstream [MinerU](https://github.com/opendatalab/MinerU) checkout in `MinerU/` and adds a small API gateway beside it. The gateway accepts a document, calls MinerU's self-hosted V1 API, and returns Markdown, structured content, and metadata. MinerU and the gateway run as separate long-lived Python processes, so its models are reused between requests. Docker is not required.

Supported file families follow MinerU 4: PDF, common image formats, DOC/DOCX, PPT/PPTX, XLS/XLSX, RTF, ODT/ODS/ODP, EPUB, OFD, HTML/MHTML, CSV, and TSV. Exact parsing tiers depend on the MinerU server configuration; Office and several native formats use its Flash parser.

## Run

- Windows 10/11 with PowerShell
- Python 3.13 (supported by MinerU 4) and the Python launcher (`py`)
- [`uv`](https://docs.astral.sh/uv/) for dependency installation
- Sufficient disk space for MinerU dependencies and downloaded model files

The default Windows installation uses MinerU's compatible local runtime. NVIDIA acceleration requires installing the matching PyTorch/runtime separately as described in `MinerU/README.md`.

Double-click `run.py` to start MinerU and the API gateway together. It creates the two Python environments and installs MinerU from the local checkout on first run, waits for MinerU readiness, starts the gateway, and opens the demo UI. Keep the console window open while using the API; press `Ctrl+C` there to stop both services.

The demo UI is available at `http://127.0.0.1:8080/`; it can upload documents, select a parsing tier, preview Markdown/metadata, and copy or download the result. On Vercel the same UI is served from the deployment root.

The first dependency and model downloads can take a while. MinerU model files use its normal user cache; upload and gateway temporary files use directories configured in `.env`.

To customize ports, limits, CORS origins, tiers, and data directories, copy `.env.example` to `.env` before starting. Python 3.13 and `uv` are required for first-time setup. To run without double-clicking, use `py -3.13 run.py` from PowerShell.

Check readiness at `http://127.0.0.1:8080/health`.

## Deploy The Gateway To Vercel

Vercel hosts only the lightweight API gateway. MinerU itself must remain running on a separate machine or provider with enough CPU/GPU memory; expose its V1 API over HTTPS. The Vercel Function must be able to reach that URL. Do not point `MINERU_API_URL` at `localhost` or a private LAN address.

1. Import the workspace root into Vercel (or link the local folder with `vercel link`). Keep the project root at this directory so Vercel detects `app.py` and the root `requirements.txt`.
2. Add these environment variables in Vercel Project Settings before deploying, for both Preview and Production:

  - `MINERU_API_URL`: public HTTPS base URL for MinerU, without a trailing `/v1` (for example `https://mineru.example.com`).
  - `MINERU_API_KEY`: set only if the remote MinerU API requires a bearer key.
  - `MAX_FILE_SIZE_MB`: optional, capped to 4 MB by the Vercel entrypoint.
  - `MINERU_TIER`: optional gateway default is `standard`; clients can pass `tier=flash` for faster parsing.
  `MINERU_API_URL` is required for Preview and Production. The app fails during startup if it is missing, uses plain HTTP, or ends in `/v1`.
3. Deploy from the Vercel dashboard or run these commands from this directory after installing the Vercel CLI:

  ```powershell
  vercel login
  vercel link
  vercel --prod
  ```

The Vercel entrypoint is `app.py`; its Function limit is set to 300 seconds. The Vercel runtime limits request and response bodies to 4.5 MB, so this deployment caps input files to 4 MB, omits the extra structured-content payload, and returns `413` when Markdown plus metadata would exceed 4,000,000 bytes. Parsing is capped at 240 seconds. Larger documents or long-running parses should call the self-hosted gateway instead of Vercel.

The conversion response is returned to the caller; converted outputs are not saved as files. Restrict public access with Vercel Deployment Protection or your application's authentication; CORS is not authentication.

## API

`POST /api/mineru/convert` accepts `multipart/form-data`:

- `file`: required document
- `tier`: optional `flash`, `basic`, `standard`, or `advanced`; defaults to `standard`

Example with cURL:

```bash
curl -X POST http://127.0.0.1:8080/api/mineru/convert \
  -F "file=@./report.pdf" \
  -F "tier=standard"
```

PowerShell:

```powershell
curl.exe -X POST http://127.0.0.1:8080/api/mineru/convert `
  -F "file=@.\report.pdf" `
  -F "tier=standard"
```

The JSON response contains `status`, `markdown`, `metadata`, and MinerU `structured_content`. These outputs are returned in the response and are not saved as files. Unsupported extensions return `415`, oversized files return `413`, upstream failures return `502`, and request/processing timeouts return `504`.

Interactive API documentation is available at `http://127.0.0.1:8080/docs`.

## Frontend Example

```javascript
const form = new FormData();
form.append("file", selectedFile);
form.append("tier", "standard");

const response = await fetch("http://127.0.0.1:8080/api/mineru/convert", {
  method: "POST",
  body: form,
});

if (!response.ok) {
  throw new Error(`Conversion failed (${response.status}): ${await response.text()}`);
}

const result = await response.json();
console.log(result.status, result.markdown, result.metadata);
```

Set `CORS_ORIGINS` in `.env` to the frontend's origin. Do not set the browser's `Content-Type` header manually; `fetch` adds the multipart boundary.

## Configuration

The key controls are documented in `.env.example`. `MAX_FILE_SIZE_MB`, `PROCESSING_TIMEOUT_SECONDS`, and `MAX_CONCURRENT_REQUESTS` protect the gateway. `MINERU_CONCURRENCY` caps parse jobs inside MinerU; keeping both concurrency values low avoids model-memory spikes. Models are preloaded at service startup, not recreated for each API request. `MINERU_MODEL_SOURCE` selects MinerU's model source.

For a separate MinerU installation, set `MINERU_API_URL` and optionally `MINERU_API_KEY` in `.env`. The gateway preserves MinerU's upload headers and sends the API key only to the configured service origin, never to a different-origin signed upload URL.

## Local Tests

```powershell
python -m pip install -r gateway/requirements.txt
python -m pip install pytest
python -m pytest gateway/tests -q
```

The tests exercise the gateway against a mocked MinerU V1 service, including upload, polling, artifact retrieval, Markdown/metadata response, and validation failures. To test real inference locally, run `py -3.13 run.py` and upload a sample document through the demo UI or the cURL command above.