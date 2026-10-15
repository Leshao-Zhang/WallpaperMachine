#!/usr/bin/env python3
"""Write the update manifest the in-app updater reads instead of the GitHub API.

Anonymous GitHub API requests are limited to 60 an hour per public IP address, shared
by every app and device behind it, so a busy network can leave the updater locked out.
A release asset is not an API request: the app fetches
`https://github.com/<repository>/releases/latest/download/WallpaperMachine-update.json`,
which github.com redirects to this file on the release marked Latest.

The manifest is the release object in the REST API's shape (`tag_name`, `html_url`,
`prerelease`, `body`, `assets` with `browser_download_url`, `size` and a `sha256:`
`digest`), so the app parses both with `GitHubReleaseParser`. Everything in it is
derived from the tag, the notes file that becomes the release body and the image bytes;
the name is `AppUpdateConfiguration.manifestName` in the app.

    python3 scripts/update_manifest.py --tag v1.0.3 --repository owner/name --notes notes.md \\
        WallpaperMachine-1.0.3-arm64.dmg

Called by .github/workflows/build.yml before publishing; writes the manifest beside the
image and prints its path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

from lib.dmg import image_name
from lib.glyphs import markers

MARK = markers()
MANIFEST_NAME = "WallpaperMachine-update.json"
VERSION_TAG = re.compile(r"^v(\d+\.\d+\.\d+)$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ManifestError(Exception):
    """A condition that stops writing the manifest; the message is the whole report."""


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def accepted_image_names(version):
    """Disk image names the updater can install for `version`."""
    return [image_name(version, architecture) for architecture in ("arm64", "x86_64", "universal")]


def manifest(tag, repository, notes, image):
    """The manifest for one release, as a dict ready for JSON.

    `image` is one disk image or a sequence of them. Each must be named for this
    tag and for `arm64`, `x86_64` or `universal`, so an Apple silicon app and an
    Intel app can both select an asset from the same manifest.
    """
    match = VERSION_TAG.match(tag)
    if not match:
        raise ManifestError(f"{tag!r} is not a release tag like v1.2.3.")
    if not REPOSITORY.match(repository):
        raise ManifestError(f"{repository!r} is not an owner/name repository.")
    images = image if isinstance(image, (list, tuple)) else [image]
    expected = accepted_image_names(match.group(1))
    seen = set()
    assets = []
    base = f"https://github.com/{repository}/releases"
    for item in images:
        item = Path(item)
        if item.name not in expected:
            raise ManifestError(f"{item.name} is not one of {', '.join(expected)}, the images the updater installs for {tag}.")
        if item.name in seen:
            raise ManifestError(f"{item.name} is listed twice.")
        seen.add(item.name)
        assets.append({
            "name": item.name,
            "browser_download_url": f"{base}/download/{tag}/{item.name}",
            "size": item.stat().st_size,
            "digest": f"sha256:{sha256(item)}",
        })
    if not assets:
        raise ManifestError(f"{tag} has no disk image.")
    return {
        "tag_name": tag,
        "html_url": f"{base}/tag/{tag}",
        "prerelease": False,
        "body": Path(notes).read_text(encoding="utf-8"),
        "assets": assets,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tag", required=True, help="Version tag the release belongs to, like v1.2.3.")
    parser.add_argument("--repository", required=True, help="owner/name of the repository the release is in.")
    parser.add_argument("--notes", required=True, help="Markdown file that becomes the release body.")
    parser.add_argument("image", nargs="+", help="Disk images the release publishes, one per architecture.")
    args = parser.parse_args(argv)
    for path in (args.notes, *args.image):
        if not Path(path).is_file():
            raise ManifestError(f"Not a file: {path}")
    output = Path(args.image[0]).with_name(MANIFEST_NAME)
    content = manifest(args.tag, args.repository, args.notes, args.image)
    output.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output)
    print(f"{MARK.ok} {output.name} for {args.tag}: {content['assets'][0]['digest']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ManifestError as error:
        print(f"::error::{error}", file=sys.stderr)
        raise SystemExit(1)
