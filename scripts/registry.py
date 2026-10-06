#!/usr/bin/env python3
"""Registry digest lookups with HEAD requests, which registries do not count as pulls."""

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

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
    """Refs under `lenient` images treat 401/403 as missing, because Docker Hub
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
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("digest").add_argument("ref")
    a = p.parse_args(argv)

    digest = Registry(credentials_from_env()).digest(a.ref)
    if digest is None:
        sys.exit(f"{a.ref} not found")
    print(digest)


if __name__ == "__main__":
    sys.exit(main())
