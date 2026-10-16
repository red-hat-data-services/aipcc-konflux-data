import hashlib
import json
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import urlsplit

import pytest


def _descriptor(payload, media_type):
    return {
        "digest": f"sha256:{hashlib.sha256(payload).hexdigest()}",
        "size": len(payload),
        "mediaType": media_type,
    }


@pytest.mark.skipif(shutil.which("skopeo") is None, reason="Skopeo is not installed")
def test_skopeo_override_selects_platform_from_local_registry_index(tmp_path):
    config = json.dumps(
        {"architecture": "amd64", "os": "linux", "rootfs": {"type": "layers", "diff_ids": []}}
    ).encode()
    layer = b"small disk-image fixture"
    config_desc = _descriptor(config, "application/vnd.oci.image.config.v1+json")
    layer_desc = _descriptor(layer, "application/vnd.oci.image.layer.v1.tar")
    manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": config_desc,
            "layers": [layer_desc],
        }
    ).encode()
    manifest_desc = _descriptor(manifest, "application/vnd.oci.image.manifest.v1+json")
    index = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [{**manifest_desc, "platform": {"architecture": "amd64", "os": "linux"}}],
        }
    ).encode()
    index_digest = f"sha256:{hashlib.sha256(index).hexdigest()}"
    responses = {
        "/v2/": (b"", "", ""),
        "/v2/test/manifests/latest": (index, "application/vnd.oci.image.index.v1+json", index_digest),
        f"/v2/test/manifests/{manifest_desc['digest']}": (
            manifest,
            "application/vnd.oci.image.manifest.v1+json",
            manifest_desc["digest"],
        ),
    }
    responses.update(
        {
            f"/v2/test/blobs/{desc['digest']}": (data, "application/octet-stream", desc["digest"])
            for desc, data in ((config_desc, config), (layer_desc, layer))
        }
    )

    class RegistryHandler(BaseHTTPRequestHandler):
        def _respond(self, send_body):
            response = responses.get(urlsplit(self.path).path)
            if response is None:
                self.send_error(404)
                return
            body, content_type, digest = response
            self.send_response(200)
            if content_type:
                self.send_header("Content-Type", content_type)
            if digest:
                self.send_header("Docker-Content-Digest", digest)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if send_body:
                self.wfile.write(body)

        def do_GET(self):
            self._respond(send_body=True)

        def do_HEAD(self):
            self._respond(send_body=False)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), RegistryHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    source = f"docker://localhost:{server.server_port}/test:latest"

    try:
        for arch, succeeds in (("arm64", False), ("amd64", True)):
            result = subprocess.run(
                ["skopeo", "copy", "--src-tls-verify=false", "--override-os", "linux",
                 "--override-arch", arch, source, f"dir:{tmp_path / arch}"],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            if succeeds:
                assert result.returncode == 0, result.stderr
            else:
                assert result.returncode != 0
                assert "no image found" in result.stderr
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
