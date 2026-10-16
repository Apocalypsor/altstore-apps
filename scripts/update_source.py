#!/usr/bin/env python3
"""Generate a SideStore source from original GitHub release IPAs (macOS, stdlib)."""

import argparse
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import zipfile


CACHE_VERSION = 1


class SourceError(Exception):
    pass


def version_key(value):
    """Compare Apple's numeric versions/builds, including development suffixes.

    Preserve the original string in the source; this key is ONLY for ordering.
    2.7.10 > 2.7.9, 3.0 > 2.8.1, 1.0 == 1.0.0, 12 > 12fc2 > 12b9.
    """
    match = re.fullmatch(r"(\d+(?:\.\d+)*)(?:(d|a|b|fc)(\d+))?", value)
    if not match:
        raise SourceError(f"Unsupported IPA version/build number: {value!r}")
    numbers = [int(part) for part in match[1].split(".")]
    while len(numbers) > 1 and numbers[-1] == 0:
        numbers.pop()
    stage = {"d": 0, "a": 1, "b": 2, "fc": 3, None: 4}[match[2]]
    return tuple(numbers), stage, int(match[3] or 0)


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise SourceError(f"Release date has no timezone: {value!r}")
    return parsed.timestamp()


def request(url, *, token=None):
    headers = {"User-Agent": "altstore-apps-source/1", "Accept": "application/vnd.github+json"}
    # Never send the workflow token to release assets, redirects, or third parties.
    if token and urlsplit(url).hostname == "api.github.com":
        headers["Authorization"] = f"Bearer {token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    for attempt in range(3):
        try:
            return urlopen(Request(url, headers=headers), timeout=90)
        except HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise
        except (URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(2 ** attempt)


def list_releases(repository, token):
    releases = []
    page = 1
    while True:
        url = f"https://api.github.com/repos/{repository}/releases?per_page=100&page={page}"
        with request(url, token=token) as response:
            batch = json.load(response)
        if not isinstance(batch, list):
            raise SourceError(f"Unexpected releases response for {repository}")
        releases.extend(batch)
        if len(batch) < 100:
            return releases
        page += 1


def select_releases(releases, app, limit):
    candidates = []
    for release in releases:
        if release["draft"] or (release["prerelease"] and not app["include_prereleases"]):
            continue
        assets = [asset for asset in release["assets"]
                  if fnmatch.fnmatchcase(asset["name"], app["asset_pattern"])]
        if not assets:
            continue  # A release may not have uploaded its IPA yet.
        if len(assets) != 1:
            raise SourceError(f"{app['repository']} {release['tag_name']}: ambiguous IPA assets")
        if assets[0].get("state", "uploaded") != "uploaded":
            continue
        candidates.append((release, assets[0]))
    # API order/updated_at are not release order. Ignore edits of old releases.
    candidates.sort(key=lambda pair: (timestamp(pair[0]["published_at"]), pair[0]["id"]), reverse=True)
    if not candidates:
        raise SourceError(f"No eligible IPA releases for {app['repository']}")
    return candidates[:limit]


def signed_entitlements(executable, directory):
    """Use Apple's parser; never execute or install downloaded code."""
    binary = directory / "executable"
    binary.write_bytes(executable)
    result = subprocess.run(
        ["codesign", "--display", "--entitlements", "-", str(binary)],
        capture_output=True, check=False,
    )
    if result.returncode:
        if b"code object is not signed at all" in result.stderr:
            return []
        raise SourceError(f"Cannot inspect IPA entitlements: {result.stderr.decode(errors='replace')}")
    if not result.stdout.strip():
        return []
    entitlements = plistlib.loads(result.stdout)
    return sorted(entitlements)


def inspect_ipa(path):
    privacy = {}
    entitlements = set()
    with zipfile.ZipFile(path) as archive, tempfile.TemporaryDirectory() as directory:
        roots = [name for name in archive.namelist()
                 if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", name)]
        if len(roots) != 1:
            raise SourceError("IPA must contain exactly one main app Info.plist")
        root = roots[0]
        main = plistlib.loads(archive.read(root))
        metadata = {}
        for field in ("CFBundleIdentifier", "CFBundleShortVersionString", "CFBundleVersion", "MinimumOSVersion"):
            value = main.get(field)
            if not isinstance(value, str) or not value.strip():
                raise SourceError(f"IPA is missing a string {field}")
            metadata[field] = value
        version_key(metadata["CFBundleShortVersionString"])
        version_key(metadata["CFBundleVersion"])
        version_key(metadata["MinimumOSVersion"])
        extensions = sorted(name for name in archive.namelist()
                            if name.startswith(root.removesuffix("Info.plist") + "PlugIns/")
                            and re.fullmatch(r"Payload/[^/]+\.app/PlugIns/[^/]+\.appex/Info\.plist", name))
        for name in [root, *extensions]:
            info = plistlib.loads(archive.read(name))
            for key, value in sorted(info.items()):
                if re.fullmatch(r"NS.*UsageDescription", key) and isinstance(value, str):
                    privacy.setdefault(key, value)
            executable = info.get("CFBundleExecutable")
            if not isinstance(executable, str) or "/" in executable or executable in ("", ".", ".."):
                raise SourceError(f"Invalid CFBundleExecutable in {name}")
            binary = archive.read(name.removesuffix("Info.plist") + executable)
            entitlements.update(signed_entitlements(binary, Path(directory)))
        metadata["appPermissions"] = {"entitlements": sorted(entitlements), "privacy": privacy}
        return metadata


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def asset_metadata(asset, cache_dir):
    # A re-upload has a new ID; an in-place metadata/content change invalidates this key.
    identity = {key: asset.get(key) for key in
                ("id", "name", "size", "updated_at", "digest", "browser_download_url")}
    identity["cache_version"] = CACHE_VERSION
    cache_key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    cache_path = cache_dir / f"{cache_key}.json"
    if cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached.get("asset") == identity:
            return cached["metadata"]
    url = asset["browser_download_url"]
    if urlsplit(url).scheme != "https" or urlsplit(url).hostname != "github.com":
        raise SourceError(f"Unexpected release asset URL: {url}")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "app.ipa"
        sha256 = hashlib.sha256()
        size = 0
        with request(url) as response, path.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                sha256.update(chunk)
                size += len(chunk)
        if size != asset["size"] or size == 0:
            raise SourceError(f"IPA size mismatch: {asset['name']}")
        digest = sha256.hexdigest()
        if asset.get("digest") and asset["digest"] != f"sha256:{digest}":
            raise SourceError(f"IPA digest mismatch: {asset['name']}")
        metadata = inspect_ipa(path)
        metadata.update(size=size, sha256=digest)
    atomic_json(cache_path, {"asset": identity, "metadata": metadata})
    return metadata


def build_app(app, candidates, get_metadata):
    versions = []
    entitlements = set()
    privacy = {}
    for release, asset in candidates:
        metadata = get_metadata(asset)
        if metadata["CFBundleIdentifier"] != app["bundleIdentifier"]:
            raise SourceError(f"Unexpected bundle ID in {app['name']} {release['tag_name']}")
        version = metadata["CFBundleShortVersionString"]
        build = metadata["CFBundleVersion"]
        # Never add a 'v' prefix / beta suffix or copy the release tag into version.
        channel = "预发布" if release["prerelease"] else "正式版"
        notes = f"{channel} · GitHub {release['tag_name']}\n{release['html_url']}"
        if release.get("body"):
            notes += "\n\n" + release["body"].replace("\r\n", "\n").strip()
        versions.append({
            "version": version,
            "buildVersion": build,
            "date": release["published_at"],
            "localizedDescription": notes,
            "downloadURL": asset["browser_download_url"],
            "size": metadata["size"],
            "sha256": metadata["sha256"],
            "minOSVersion": metadata["MinimumOSVersion"],
        })
        entitlements.update(metadata["appPermissions"]["entitlements"])
        for key, value in metadata["appPermissions"]["privacy"].items():
            privacy.setdefault(key, value)
        print(f"{app['name']} {release['tag_name']} -> IPA {version} ({build}), iOS {metadata['MinimumOSVersion']}+")
    versions.sort(key=lambda v: (version_key(v["version"]), version_key(v["buildVersion"]), timestamp(v["date"])), reverse=True)
    # SideStore uses version + build as a unique key; keep the newest publication.
    unique = {}
    for version in versions:
        unique.setdefault((version["version"], version["buildVersion"]), version)
    result = {key: app[key] for key in (
        "name", "bundleIdentifier", "developerName", "subtitle", "localizedDescription", "iconURL", "tintColor"
    )}
    result["versions"] = list(unique.values())
    result["appPermissions"] = {"entitlements": sorted(entitlements), "privacy": dict(sorted(privacy.items()))}
    return result


def generate_source(config, repository, cache_dir, token):
    source = dict(config["source"])
    source["sourceURL"] = f"https://raw.githubusercontent.com/{repository}/release/source.json"
    source["apps"] = []
    limit = config["history_limit"]
    if not isinstance(limit, int) or limit < 1:
        raise SourceError("history_limit must be positive")
    for app in config["apps"]:
        candidates = select_releases(list_releases(app["repository"], token), app, limit)
        source["apps"].append(build_app(app, candidates, lambda asset: asset_metadata(asset, cache_dir)))
    source["news"] = []
    return source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("apps.json"))
    parser.add_argument("--output", type=Path, default=Path("dist/source.json"))
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache/ipa-metadata"))
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", "Apocalypsor/altstore-apps"))
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    source = generate_source(config, args.repository, args.cache_dir, token)
    # No partial source is written if any upstream, IPA, or version check fails.
    atomic_json(args.output, source)
    print(f"Wrote {args.output}: {len(source['apps'])} apps")


if __name__ == "__main__":
    try:
        main()
    except (SourceError, OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        print(f"Source update failed: {error}", file=sys.stderr)
        sys.exit(1)
