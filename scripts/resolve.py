#!/usr/bin/env python3
"""Map meta-server branches to builds and decide what to build or sync."""

import argparse
import base64
import configparser
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from tags import MIRROR_VARIANTS, VARIANTS, all_tags, fixed_tag

META_PATH = "holo/steamos/aarch64/vr"
USER_AGENT = os.environ.get("USER_AGENT", "holo-deckard")
MANIFEST_TYPES = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)
REGISTRY_HOSTS = {"docker.io": "registry-1.docker.io"}


def fetch(url, headers=None, retries=3):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.URLError:
            if attempt == retries - 1:
                raise
            time.sleep(5 * (attempt + 1))


def split_ref(ref):
    name, tag = ref.rsplit(":", 1)
    host, repo = name.split("/", 1)
    return REGISTRY_HOSTS.get(host, host), repo, tag


class Registry:
    """Digest lookups with HEAD requests, which Docker Hub does not count as pulls.

    Refs under `lenient` images treat 401/403 as missing, because Docker Hub
    answers 401 for repositories that do not exist yet.
    """

    def __init__(self, credentials, lenient=()):
        self.credentials = credentials
        self.lenient = set(lenient)
        self.auth = {}
        self.digests = {}

    def _authorize(self, host, challenge):
        params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
        realm = params.pop("realm")
        headers = {}
        if host in self.credentials:
            user, password = self.credentials[host]
            basic = base64.b64encode(f"{user}:{password}".encode()).decode()
            headers["Authorization"] = f"Basic {basic}"
        body = json.loads(fetch(f"{realm}?{urllib.parse.urlencode(params)}", headers))
        return f"Bearer {body['token']}"

    def digest(self, ref):
        if ref not in self.digests:
            self.digests[ref] = self._head(ref)
        return self.digests[ref]

    def _head(self, ref):
        host, repo, tag = split_ref(ref)
        url = f"https://{host}/v2/{repo}/manifests/{tag}"
        for attempt in range(2):
            headers = {"Accept": MANIFEST_TYPES, "User-Agent": USER_AGENT}
            if (host, repo) in self.auth:
                headers["Authorization"] = self.auth[host, repo]
            req = urllib.request.Request(url, headers=headers, method="HEAD")
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return r.headers["Docker-Content-Digest"]
            except urllib.error.HTTPError as e:
                challenge = e.headers.get("WWW-Authenticate") or ""
                if e.code == 401 and attempt == 0 and challenge.startswith("Bearer"):
                    self.auth[host, repo] = self._authorize(host, challenge)
                    continue
                lenient = ref.rsplit(":", 1)[0] in self.lenient
                if e.code == 404 or (lenient and e.code in (401, 403)):
                    return None
                raise


def parse_branches(remote_info):
    cp = configparser.ConfigParser()
    cp.read_string(remote_info)
    return [b.strip() for b in cp.get("Server", "Branches").split(";") if b.strip()]


def parse_candidate(raw):
    data = json.loads(raw or "{}")
    cands = data.get("minor", {}).get("candidates", [])
    if not cands:
        return None
    c = cands[0]
    img = c["image"]
    return {
        "buildid": img["buildid"],
        "version": img["version"],
        "image_branch": img["branch"],
        "update_path": c["update_path"],
        "chunks_store_path": c["chunks_store_path"],
    }


def group(candidates):
    """{branch: candidate|None} -> builds with every branch that points at them."""
    builds = {}
    for branch, cand in candidates.items():
        if cand is None:
            continue
        b = builds.setdefault(cand["buildid"], {**cand, "branches": []})
        if b["update_path"] != cand["update_path"]:
            raise ValueError(f"build {cand['buildid']} has conflicting update paths")
        b["branches"].append(branch)
    for b in builds.values():
        b["branches"].sort()
    return [builds[k] for k in sorted(builds)]


def plan(builds, image, digest, mirror=None, force=False, only_branch=None):
    """Builds missing from `image` are rebuilt; otherwise stale tags are synced from it.

    `mirror` receives the MIRROR_VARIANTS tags, fixed tags included.
    """
    targets = [(image, VARIANTS)] + ([(mirror, MIRROR_VARIANTS)] if mirror else [])
    to_build, to_sync = [], []
    for b in builds:
        if only_branch and only_branch not in b["branches"]:
            continue
        if force:
            to_build.append(b)
            continue
        sources = {v: digest(f"{image}:{fixed_tag(v, b['buildid'])}") for v in VARIANTS}
        if None in sources.values():
            to_build.append(b)
            continue
        for target, variants in targets:
            for v in variants:
                stale = [
                    f"{target}:{t}"
                    for t in all_tags(v, b["buildid"], b["branches"])
                    if digest(f"{target}:{t}") != sources[v]
                ]
                if stale:
                    to_sync.append(
                        {"source": f"{image}:{fixed_tag(v, b['buildid'])}", "tags": stale}
                    )
    return to_build, to_sync


def credentials_from_env():
    creds = {}
    if os.environ.get("GHCR_TOKEN"):
        creds["ghcr.io"] = (os.environ.get("GHCR_USER", "token"), os.environ["GHCR_TOKEN"])
    if os.environ.get("DOCKERHUB_TOKEN"):
        creds[REGISTRY_HOSTS["docker.io"]] = (
            os.environ.get("DOCKERHUB_USERNAME", ""),
            os.environ["DOCKERHUB_TOKEN"],
        )
    return creds


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--meta-url", required=True)
    p.add_argument("--image", required=True)
    p.add_argument("--mirror-image", default="")
    p.add_argument("--branch", default="")
    p.add_argument("--force", action="store_true")
    p.add_argument("--github-output")
    a = p.parse_args(argv)

    base = f"{a.meta_url.rstrip('/')}/{META_PATH}"
    branches = parse_branches(fetch(f"{base}/remote-info.conf").decode())
    candidates = {b: parse_candidate(fetch(f"{base}/{b}.json").decode()) for b in branches}
    for b, c in candidates.items():
        print(f"{b}: {c['buildid'] if c else '(none)'}", file=sys.stderr)

    mirror = a.mirror_image or None
    registry = Registry(credentials_from_env(), lenient=[mirror] if mirror else [])
    to_build, to_sync = plan(
        group(candidates),
        a.image,
        registry.digest,
        mirror=mirror,
        force=a.force,
        only_branch=a.branch or None,
    )
    out = {
        "build": json.dumps(to_build),
        "sync": json.dumps(to_sync),
        "has_build": str(bool(to_build)).lower(),
        "has_sync": str(bool(to_sync)).lower(),
    }
    print(json.dumps({"build": to_build, "sync": to_sync}, indent=2))
    if a.github_output:
        with open(a.github_output, "a") as f:
            for k, v in out.items():
                f.write(f"{k}={v}\n")


if __name__ == "__main__":
    sys.exit(main())
