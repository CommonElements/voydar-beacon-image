#!/usr/bin/env python3
"""Load the digest-pinned container images shipped on the Beacon card into Docker (first boot).

For each /opt/voydar/images/NAME.tar (+ NAME.tar.json written at build time by fetch-image.py):
  1. `docker load` it;
  2. check the loaded layer diff IDs equal the pinned ones (content-addressed, so this proves the
     bytes are the pinned image) and delete it if not; with Docker's classic store (Debian's
     docker.io) the image ID must also equal the pinned config digest;
  3. record the pinned reference `repo@sha256:<index digest>` in Docker's reference store, which
     `docker load` does not do, so `docker run repo:tag@sha256:...` (as the installer runs it)
     finds the image locally instead of downloading it again.

Anything that fails is logged and skipped: the installer then simply pulls the pinned image.
Writes /var/lib/voydar/preload.done when finished (the setup page waits for it).
"""

import json
import os
import subprocess
import sys
import time

IMAGES = "/opt/voydar/images"
DONE = "/var/lib/voydar/preload.done"
REPO_STORE = "/var/lib/docker/image/overlay2/repositories.json"


def log(msg):
    print("voydar-preload: " + msg, flush=True)


def sh(*cmd, timeout=1800):
    return subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)


def wait_docker():
    for _ in range(60):
        if sh("docker", "info", timeout=30).returncode == 0:
            return True
        time.sleep(2)
    return False


def main():
    os.makedirs(os.path.dirname(DONE), exist_ok=True)
    loaded = []
    if not wait_docker():
        log("docker is not running; skipping preload (the installer will pull)")
    else:
        for name in sorted(os.listdir(IMAGES)):
            if not name.endswith(".tar"):
                continue
            tar = os.path.join(IMAGES, name)
            try:
                with open(tar + ".json") as fh:
                    meta = json.load(fh)
            except (OSError, ValueError):
                log("%s has no metadata; skipping" % name)
                continue
            repo_tag = meta["ref"].split("@")[0]
            repo = repo_tag.rsplit(":", 1)[0]
            log("loading %s" % repo_tag)
            p = sh("docker", "load", "-i", tar)
            if p.returncode != 0:
                log("docker load failed: %s" % p.stderr.strip()[:300])
                continue
            p = sh("docker", "image", "inspect", "-f", "{{.Id}} {{json .RootFS.Layers}}", repo_tag, timeout=60)
            got_id, _, layers = p.stdout.strip().partition(" ")
            try:
                got_layers = json.loads(layers or "null")
            except ValueError:
                got_layers = None
            if got_layers != meta.get("diff_ids"):
                log("loaded layers of %s do not match the pin; removing it" % repo_tag)
                sh("docker", "image", "rm", "-f", repo_tag, timeout=120)
                continue
            os.remove(tar)  # free the space; the image now lives in Docker
            if got_id != meta["image_id"]:
                # containerd image store: IDs differ and the reference store below does not apply.
                log("%s loaded (containerd image store; the installer may re-check it online)" % repo_tag)
                continue
            loaded.append((repo, repo_tag, meta["index_digest"], meta["image_id"]))

        if loaded and os.path.exists(REPO_STORE):
            # Docker must be stopped while its reference store is edited.
            sh("systemctl", "stop", "docker.socket", "docker.service", timeout=120)
            try:
                with open(REPO_STORE) as fh:
                    store = json.load(fh)
                repos = store.setdefault("Repositories", {})
                for repo, _tag, digest, image_id in loaded:
                    repos.setdefault(repo, {})["%s@%s" % (repo, digest)] = image_id
                with open(REPO_STORE + ".tmp", "w") as fh:
                    json.dump(store, fh)
                os.replace(REPO_STORE + ".tmp", REPO_STORE)
            finally:
                sh("systemctl", "start", "docker.service", timeout=120)
            wait_docker()
            for repo, _tag, digest, image_id in loaded:
                ref = "%s@%s" % (repo, digest)
                ok = sh("docker", "image", "inspect", "-f", "{{.Id}}", ref, timeout=60).stdout.strip() == image_id
                log("%s %s" % (ref, "ready (no download needed)" if ok else "not resolvable locally; the installer will pull it"))
        elif loaded:
            log("docker uses a different image store; the images are loaded by tag only")

    with open(DONE, "w") as fh:
        fh.write(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + " loaded=%d\n" % len(loaded))


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:  # noqa: BLE001 - never block setup on the preload
        log("failed: %s" % ex)
        os.makedirs(os.path.dirname(DONE), exist_ok=True)
        with open(DONE, "w") as fh:
            fh.write("failed\n")
        sys.exit(0)
