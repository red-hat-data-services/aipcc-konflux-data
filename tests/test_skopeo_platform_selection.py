import hashlib
import json
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import urlsplit

import pytest


def _descriptor(payload, media_type):
    digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    return {"digest": digest, "size": len(payload), "mediaType": media_type}


def _registry_handler(index, index_digest, manifest_digest, blobs):
    class RegistryHandler(BaseHTTPRequestHandler):
        def _respond(self, send_body):
            path = urlsplit(self.path).path
            if path == "/v2/":
                self.send_response(200)
                self.send_header("Docker-Distribution-Api-Version", "registry/2.0")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            if path in ("/v2/test/manifests/latest", f"/v2/test/manifests/{index_digest}"):
                payload = index
                content_type = "application/vnd.oci.image.index.v1+json"
                digest = index_digest
            elif path == f"/v2/test/manifests/{manifest_digest}":
                payload = blobs[manifest_digest]
                content_type = "application/vnd.oci.image.manifest.v1+json"
                digest = manifest_digest
            elif path.startswith("/v2/test/blobs/"):
                digest = path.rsplit("/", 1)[-1]
                payload = blobs.get(digest)
                if payload is None:
                    self.send_error(404)
                    return
                content_type = "application/octet-stream"
            else:
                self.send_error(404)
                return

            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Docker-Content-Digest", digest)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if send_body:
                self.wfile.write(payload)

        def do_GET(self):
            self._respond(send_body=True)

        def do_HEAD(self):
            self._respond(send_body=False)

        def log_message(self, *_args):
            pass

    return RegistryHandler


@pytest.mark.skipif(shutil.which("skopeo") is None, reason="Skopeo is not installed")
def test_skopeo_override_selects_amd64_from_platform_annotated_registry_index(tmp_path):
    config = json.dumps(
        {
            "architecture": "amd64",
            "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": []},
        }
    ).encode()
    layer = b"small disk-image fixture"
    config_descriptor = _descriptor(config, "application/vnd.oci.image.config.v1+json")
    layer_descriptor = _descriptor(layer, "application/vnd.oci.image.layer.v1.tar")
    manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": config_descriptor,
            "layers": [layer_descriptor],
        }
    ).encode()
    manifest_descriptor = _descriptor(
        manifest, "application/vnd.oci.image.manifest.v1+json"
    )
    index = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [
                {
                    **manifest_descriptor,
                    "platform": {"architecture": "amd64", "os": "linux"},
                }
            ],
        }
    ).encode()
    index_digest = f"sha256:{hashlib.sha256(index).hexdigest()}"
    blobs = {
        config_descriptor["digest"]: config,
        layer_descriptor["digest"]: layer,
        manifest_descriptor["digest"]: manifest,
    }

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        _registry_handler(
            index, index_digest, manifest_descriptor["digest"], blobs
        ),
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    source = f"docker://localhost:{server.server_port}/test:latest"

    try:
        wrong_platform = subprocess.run(
            [
                "skopeo",
                "copy",
                "--src-tls-verify=false",
                "--override-os",
                "linux",
                "--override-arch",
                "arm64",
                source,
                f"dir:{tmp_path / 'arm64'}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert wrong_platform.returncode != 0
        assert "no image found" in wrong_platform.stderr

        selected_platform = subprocess.run(
            [
                "skopeo",
                "copy",
                "--src-tls-verify=false",
                "--override-os",
                "linux",
                "--override-arch",
                "amd64",
                source,
                f"dir:{tmp_path / 'amd64'}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert selected_platform.returncode == 0, selected_platform.stderr
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
