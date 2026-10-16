"""Azure adapter — the SECOND provider (Lab 5 Task 4). upload, download and invoke only.

The primary is GCP. This file exists to prove the seam is real, not to migrate: the other
eight methods stay NotImplementedError on purpose.

No SDK. The three methods talk to the Blob REST API and to the service over HTTPS with the
standard library, the same way cloudlayer/gcp.py's _download_rest does, for the same
reason: the serving image ships cloudlayer/ and must stay free of provider SDKs.

Equivalences, chosen like-for-like with what the primary actually runs:
    Cloud Storage bucket   -> Storage account + blob container
    Cloud Run service      -> Container Apps app (scale to zero, plain HTTPS ingress)
    service account        -> user-assigned managed identity
    PROJECT_ID             -> resource group

BLOB_URI has two accepted forms:
    https://<account>.blob.core.windows.net/<container>/<prefix>    (Azure)
    http://<host>:10000/<account>/<container>/<prefix>              (Azurite, path-style)

Auth for blob calls, first match wins:
    AZURE_STORAGE_KEY        account key, Shared Key signing. For Azurite, whose key is
                             public; do not use one for a real account.
    IDENTITY_ENDPOINT        managed identity inside Container Apps (the deployed service),
                             with AZURE_CLIENT_ID naming a user-assigned identity.
    az CLI                   `az account get-access-token` after `az login` (the laptop).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import formatdate
from pathlib import Path
from typing import Any

from cloudlayer.base import CloudAdapter

API_VERSION = "2021-12-02"          # Blob REST version; Azurite and Azure both accept it
STORAGE_SCOPE = "https://storage.azure.com/"
AZURE_BLOB_SUFFIX = ".blob.core.windows.net"


def _run(cmd: list[str]) -> str:
    if not shutil.which(cmd[0]):
        raise RuntimeError(f"{cmd[0]} is not on PATH: install the Azure CLI, then `az login`")
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed ({out.returncode}):\n{out.stderr.strip()}")
    return out.stdout.strip()


class AzureAdapter(CloudAdapter):
    _token: tuple[float, str] | None = None

    # --- blob addressing ------------------------------------------------------
    def _blob_uri(self, key: str) -> str:
        base = self.cfg.blob_uri.rstrip("/")
        if not base.startswith(("https://", "http://")):
            raise ValueError(
                "BLOB_URI must be https://<account>.blob.core.windows.net/<container>/<prefix>"
                f" for Azure, got {base!r}")
        return f"{base}/{key.lstrip('/')}"

    @staticmethod
    def _account(uri: str) -> str:
        """The storage account: the first host label on Azure, the first path segment on
        Azurite. Shared Key signs over it, so it has to be right."""
        u = urllib.parse.urlsplit(uri)
        if u.hostname and u.hostname.endswith(AZURE_BLOB_SUFFIX):
            return u.hostname.split(".", 1)[0]
        if u.scheme == "http" and u.hostname not in ("127.0.0.1", "localhost") \
                and "." in (u.hostname or ""):
            raise ValueError(f"refusing plain http to a non-emulator host: {uri!r}")
        return u.path.lstrip("/").split("/", 1)[0]

    # --- auth -----------------------------------------------------------------
    def _bearer(self) -> str:
        now = time.time()
        if self._token and now - self._token[0] < 3000:
            return self._token[1]
        if os.environ.get("IDENTITY_ENDPOINT") and os.environ.get("IDENTITY_HEADER"):
            # Container Apps managed identity. Not the IMDS address a VM uses: Container
            # Apps and App Service inject their own endpoint, and that difference is
            # invisible until the container runs there.
            query = {"resource": STORAGE_SCOPE, "api-version": "2019-08-01"}
            if os.environ.get("AZURE_CLIENT_ID"):
                query["client_id"] = os.environ["AZURE_CLIENT_ID"]
            req = urllib.request.Request(
                f"{os.environ['IDENTITY_ENDPOINT']}?{urllib.parse.urlencode(query)}",
                headers={"X-IDENTITY-HEADER": os.environ["IDENTITY_HEADER"]})
            with urllib.request.urlopen(req, timeout=10) as r:
                token = json.load(r)["access_token"]
        elif shutil.which("az"):
            token = _run(["az", "account", "get-access-token", "--resource", STORAGE_SCOPE,
                          "--query", "accessToken", "-o", "tsv"])
        else:
            raise RuntimeError(
                "No Azure credential: set AZURE_STORAGE_KEY (Azurite), run inside Container "
                "Apps with a managed identity, or install the az CLI and run `az login`.")
        self._token = (now, token)
        return token

    def _shared_key(self, method: str, url: str, headers: dict[str, str], key: str) -> str:
        """Shared Key signature, Blob service, version 2015-02-21 and later."""
        u = urllib.parse.urlsplit(url)
        account = self._account(url)
        length = headers.get("Content-Length", "")
        std = [method, "", "", "" if length == "0" else length, "",
               headers.get("Content-Type", ""), "", "", "", "", "", headers.get("Range", "")]
        ms = sorted((k.lower(), v.strip()) for k, v in headers.items()
                    if k.lower().startswith("x-ms-"))
        resource = f"/{account}{u.path}"
        for name, value in sorted(urllib.parse.parse_qsl(u.query, keep_blank_values=True)):
            resource += f"\n{name.lower()}:{value}"
        to_sign = "\n".join(std) + "\n" + "".join(f"{k}:{v}\n" for k, v in ms) + resource
        sig = hmac.new(base64.b64decode(key), to_sign.encode(), hashlib.sha256).digest()
        return f"SharedKey {account}:{base64.b64encode(sig).decode()}"

    def _request(self, method: str, url: str, body: bytes | None = None,
                 headers: dict[str, str] | None = None):
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("https", "http"):
            raise ValueError(f"not an Azure blob URL: {url!r}")
        url = urllib.parse.urlunsplit(u._replace(path=urllib.parse.quote(u.path, safe="/")))
        h = {"x-ms-date": formatdate(usegmt=True), "x-ms-version": API_VERSION,
             **(headers or {})}
        if body is not None:
            h["Content-Length"] = str(len(body))
            # Set it ourselves: urllib otherwise adds a form-encoded Content-Type after
            # signing, and the signature no longer matches the request.
            h.setdefault("Content-Type", "application/octet-stream")
        key = os.environ.get("AZURE_STORAGE_KEY")
        h["Authorization"] = (self._shared_key(method, url, h, key) if key
                              else f"Bearer {self._bearer()}")
        req = urllib.request.Request(url, data=body, method=method, headers=h)
        try:
            return urllib.request.urlopen(req, timeout=60)
        except urllib.error.HTTPError as e:
            # The storage error code (AuthorizationPermissionMismatch, ContainerNotFound...)
            # is in a header and an XML body; urllib's message has neither.
            code = e.headers.get("x-ms-error-code", "")
            raise RuntimeError(f"{method} {url} -> {e.code} {code}".rstrip()) from None

    # --- Lab 1 methods, ported ----------------------------------------------------
    def upload(self, local_path: str, key: str) -> str:
        uri = self._blob_uri(key)
        data = Path(local_path).read_bytes()   # single Put Blob; fine to 5000 MiB
        with self._request("PUT", uri, data, {"x-ms-blob-type": "BlockBlob",
                                              "Content-Type": "application/octet-stream"}):
            pass
        return uri

    def download(self, uri: str, local_path: str) -> None:
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        with self._request("GET", uri) as r, open(local_path, "wb") as fh:
            shutil.copyfileobj(r, fh)

    def push_image(self, local_tag: str) -> str:
        raise NotImplementedError(
            "Not part of the Task 4 swap. infra/azure/lab5-swap.sh pushes with az acr login.")

    # --- Lab 3 method, ported -----------------------------------------------------
    def endpoint_url(self, endpoint: str) -> str:
        """Container Apps gives each app a generated FQDN; find it by name. AZURE_ENDPOINT_URL
        overrides it, for a local container standing in for the app."""
        if os.environ.get("AZURE_ENDPOINT_URL"):
            return os.environ["AZURE_ENDPOINT_URL"].rstrip("/")
        fqdn = _run(["az", "containerapp", "show", "--name", endpoint,
                     "--resource-group", self.cfg.project_id,
                     "--query", "properties.configuration.ingress.fqdn", "-o", "tsv"])
        return f"https://{fqdn}"

    def invoke(self, endpoint: str, payload: dict[str, Any], *,
               url: str | None = None) -> dict[str, Any]:
        """POST one payload; same contract as GcpAdapter.invoke.

        Container Apps forwards the path and body unchanged, so the service's own response
        shape comes back. What differs is auth: Cloud Run rejects a call without an
        identity token, Container Apps' external ingress accepts anyone. No header is sent
        here because none is checked, which is the leak, not a simplification.
        """
        base = url or self.endpoint_url(endpoint)
        path = "/predict/batch" if "rows" in payload else "/predict"
        req = urllib.request.Request(
            base + path, data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        started = time.perf_counter()
        try:
            # 120 s, not 60: a scaled-to-zero app pulls the image and loads the model first.
            with urllib.request.urlopen(req, timeout=120) as r:
                status, headers, raw = r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            status, headers, raw = e.code, e.headers, e.read()
        latency_ms = (time.perf_counter() - started) * 1000
        try:
            body = json.loads(raw)
        except ValueError:
            body = {"raw": raw.decode(errors="replace")[:500]}
        return {
            "status": status,
            "body": body,
            "model_version": headers.get("x-model-version"),
            "request_id": headers.get("x-request-id"),
            "server_timing": headers.get("server-timing"),
            "latency_ms": round(latency_ms, 2),
        }

    # Everything else stays unimplemented: submit_training, register_model, deploy,
    # set_traffic, emit_metric, recent_inputs, generate, teardown. Deploy and teardown for
    # the demo are infra/azure/lab5-swap.sh, a script, because they are a one-off.
