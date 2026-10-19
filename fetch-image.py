#!/usr/bin/env python3
"""Fetch one platform of a digest-pinned container image into a `docker load` archive.

Build-time helper for the Voydar Beacon SD-card image. Python 3 stdlib only, no Docker needed.
Every byte is checked against the pinned digest chain:

    index digest (pinned)  ->  platform manifest digest  ->  config + layer digests

so the archive can only contain exactly what the pin names. The tar is written
deterministically (sorted, mtime 0, uid/gid 0) so the same pin gives the same archive bytes.

The config digest (the image ID with Docker's classic store) and the layer diff IDs are printed
here and written next to the archive (NAME.tar.json) so first boot can prove the loaded image is
the pinned one.

Usage:
  fetch-image.py --ref ghcr.io/OWNER/NAME:TAG@sha256:HEX --platform linux/arm64 --out x.tar
  fetch-image.py --ref ... --platform linux/arm64 --inspect      # verify + print, no layers
"""

import argparse
import hashlib
import io
import json
import os
import re
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request

UA = "Voydar-Beacon-Image-Build/1.0 (+https://www.voydar.com/contribute)"
INDEX_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
)
MANIFEST_TYPES = (
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)
# Plain, gzip and zstd layers. zstd needs Docker >= 23: Debian trixie's docker.io (26.x) has it,
# bookworm's (20.10.24) does NOT, which is why the image is built on trixie (see README).
OK_LAYER_TYPES = (
    "application/vnd.oci.image.layer.v1.tar",
    "application/vnd.oci.image.layer.v1.tar+gzip",
    "application/vnd.oci.image.layer.v1.tar+zstd",
    "application/vnd.docker.image.rootfs.diff.tar.gzip",
)
REF_RE = re.compile(r"^(?P<host>[a-z0-9.-]+)/(?P<repo>[a-z0-9._/-]+):(?P<tag>[A-Za-z0-9._-]+)@(?P<digest>sha256:[0-9a-f]{64})$")


class _NoAuthOnCrossHostRedirect(urllib.request.HTTPRedirectHandler):
    """Blob GETs redirect to a CDN; never forward the registry bearer token to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            new.headers.pop("Authorization", None)
            new.unredirected_hdrs.pop("Authorization", None)
        return new


_opener = urllib.request.build_opener(_NoAuthOnCrossHostRedirect)


def die(msg):
    print("fetch-image: ERROR: " + msg, file=sys.stderr)
    sys.exit(1)


def _get(url, token=None, accept=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    if accept:
        req.add_header("Accept", accept)
    return _opener.open(req, timeout=120)


def token_for(host, repo):
    # Anonymous pull token (ghcr.io and other Docker-distribution registries with bearer auth).
    try:
        with _get("https://%s/v2/" % host) as r:
            return None  # registry allows anonymous access without a token
    except urllib.error.HTTPError as e:
        if e.code != 401:
            raise
        chal = e.headers.get("WWW-Authenticate", "")
    params = dict(re.findall(r'(\w+)="([^"]*)"', chal))
    if "realm" not in params:
        die("unexpected auth challenge from %s" % host)
    q = {"scope": "repository:%s:pull" % repo}
    if "service" in params:
        q["service"] = params["service"]
    with _get(params["realm"] + "?" + urllib.parse.urlencode(q)) as r:
        body = json.load(r)
    return body.get("token") or body.get("access_token")


def get_verified(host, repo, digest, token, accept=None):
    with _get("https://%s/v2/%s/manifests/%s" % (host, repo, digest), token, accept) as r:
        data = r.read()
    got = "sha256:" + hashlib.sha256(data).hexdigest()
    if got != digest:
        die("manifest digest mismatch: pinned %s, registry returned %s" % (digest, got))
    return json.loads(data)


def blob_to(host, repo, digest, token, fh):
    h = hashlib.sha256()
    n = 0
    with _get("https://%s/v2/%s/blobs/%s" % (host, repo, digest), token) as r:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
            fh.write(chunk)
            n += len(chunk)
    got = "sha256:" + h.hexdigest()
    if got != digest:
        die("blob digest mismatch for %s (got %s)" % (digest, got))
    return n


def pick_platform(index, platform):
    os_, arch = platform.split("/")[:2]
    for m in index.get("manifests", []):
        p = m.get("platform") or {}
        if p.get("os") == os_ and p.get("architecture") == arch and p.get("variant") in (None, "v8"):
            return m
    die("no %s manifest in the index" % platform)


def _add(tar, name, data=None, fileobj=None, size=None):
    ti = tarfile.TarInfo(name)
    ti.mtime = 0
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    ti.mode = 0o644
    if data is not None:
        ti.size = len(data)
        tar.addfile(ti, io.BytesIO(data))
    else:
        ti.size = size
        tar.addfile(ti, fileobj)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ref", required=True, help="host/repo:tag@sha256:<index or manifest digest>")
    ap.add_argument("--platform", default="linux/arm64")
    ap.add_argument("--out", help="docker-archive tar to write")
    ap.add_argument("--inspect", action="store_true", help="verify the digest chain and print; fetch no layers")
    a = ap.parse_args()

    m = REF_RE.match(a.ref)
    if not m:
        die("ref must look like ghcr.io/owner/name:tag@sha256:<64 hex>")
    host, repo, tag, digest = m.group("host", "repo", "tag", "digest")
    token = token_for(host, repo)

    top = get_verified(host, repo, digest, token, ", ".join(INDEX_TYPES + MANIFEST_TYPES))
    if top.get("mediaType") in INDEX_TYPES or "manifests" in top:
        pm = pick_platform(top, a.platform)
        manifest_digest = pm["digest"]
        manifest = get_verified(host, repo, manifest_digest, token, ", ".join(MANIFEST_TYPES))
    else:
        manifest_digest, manifest = digest, top

    for layer in manifest["layers"]:
        if layer["mediaType"] not in OK_LAYER_TYPES:
            die("layer %s has media type %s, which docker load does not support" % (layer["digest"], layer["mediaType"]))

    summary = {
        "ref": a.ref,
        "platform": a.platform,
        "index_digest": digest,
        "manifest_digest": manifest_digest,
        # What `docker image inspect -f {{.Id}}` shows after a load with Docker's classic image store
        # (Debian's docker.io). The containerd image store reports a different ID; see voydar-preload.
        "image_id": manifest["config"]["digest"],
        "compressed_bytes": sum(l["size"] for l in manifest["layers"]),
    }
    if a.inspect or not a.out:
        print(json.dumps(summary, indent=2))
        return

    out_dir = os.path.dirname(os.path.abspath(a.out))
    with tempfile.TemporaryDirectory(dir=out_dir) as tmp:
        cfg = io.BytesIO()
        blob_to(host, repo, manifest["config"]["digest"], token, cfg)
        # Uncompressed layer digests, from the (digest-verified) config. Docker reports these as
        # .RootFS.Layers after a load with either image store, so first boot can check them.
        summary["diff_ids"] = json.loads(cfg.getvalue())["rootfs"]["diff_ids"]
        cfg_name = manifest["config"]["digest"].split(":")[1] + ".json"
        layer_files = []
        for layer in manifest["layers"]:
            hexd = layer["digest"].split(":")[1]
            path = os.path.join(tmp, hexd)
            with open(path, "wb") as fh:
                size = blob_to(host, repo, layer["digest"], token, fh)
            layer_files.append((hexd + "/layer.tar", path, size))
            print("  layer %s  %d bytes" % (layer["digest"], size), file=sys.stderr)

        repo_tag = "%s/%s:%s" % (host, repo, tag)
        docker_manifest = [{"Config": cfg_name, "RepoTags": [repo_tag], "Layers": [n for n, _, _ in layer_files]}]
        partial = a.out + ".partial"
        with tarfile.open(partial, "w", format=tarfile.USTAR_FORMAT) as tar:
            _add(tar, cfg_name, cfg.getvalue())
            for name, path, size in layer_files:
                with open(path, "rb") as fh:
                    _add(tar, name, fileobj=fh, size=size)
            _add(tar, "manifest.json", json.dumps(docker_manifest, sort_keys=True).encode())
            _add(tar, "repositories", json.dumps({"%s/%s" % (host, repo): {tag: manifest["config"]["digest"].split(":")[1]}}, sort_keys=True).encode())
        os.replace(partial, a.out)

    with open(a.out + ".json", "w") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True)
        fh.write("\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
