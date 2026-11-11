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
derived from the tag, the notes file that becomes the release body, the changelog and
the image bytes;
the name is `AppUpdateConfiguration.manifestName` in the app.

`--changelog` adds one field the API does not have: `changelog`, the newest 20
(CHANGELOG_SECTIONS) `## ` version sections of CHANGELOG.md verbatim, in all their
languages, without the file's preamble. The app's update window shows the sections of
every version newer than the installed one from it, so an update across several
versions lists them all; a manifest without the field falls back to `body`.

    python3 scripts/update_manifest.py --tag v1.0.3 --repository owner/name --notes notes.md \\
        --changelog CHANGELOG.md WallpaperMachine-1.0.3-arm64.dmg

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
# How many versions an update window can list: an install this many releases behind
# still sees every change, and the manifest stays small.
CHANGELOG_SECTIONS = 20
# A release heading, `## [1.6.2](<compare link>) (2026-10-24)` or the older `## 1.6.2 — 2026-10-24`.
SECTION_HEADING = re.compile(r"^## \[?v?(\d+\.\d+\.\d+)(?:\]|[ \t]|$)", re.MULTILINE)


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


def version_key(text):
    parts = [int(part) for part in str(text).split(".")]
    return tuple(parts + [0] * (3 - len(parts)))


def changelog_sections(path, tag, limit=CHANGELOG_SECTIONS):
    """The newest `limit` version sections of a changelog, verbatim, without its preamble.

    The release's own version must be the newest section, so a manifest never describes
    notes the window cannot show: the app reads the history only when it starts there.
    """
    text = Path(path).read_text(encoding="utf-8")
    starts = [match.start() for match in re.finditer(r"^## ", text, re.MULTILINE)]
    if not starts:
        raise ManifestError(f"{path} has no ## version sections.")
    versions = SECTION_HEADING.findall(text)
    if not versions or versions[0] != tag.removeprefix("v"):
        raise ManifestError(f"{path} does not start with the section for {tag}.")
    end = starts[limit] if len(starts) > limit else len(text)
    return text[starts[0]:end].rstrip("\n") + "\n"


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def manifest(tag, repository, notes, image, changelog=None, metadata=None):
    """The manifest for one release, as a dict ready for JSON.

    `image` is one disk image or a sequence of them. Each must be named for this
    tag and for `arm64`, `x86_64` or `universal`, so an Apple silicon app and an
    Intel app can both select an asset from the same manifest. `changelog` adds
    its sections. `metadata` is one digest-bound sidecar, or one per image in the
    same order; the release then reports every architecture those images contain.
    """
    match = VERSION_TAG.match(tag)
    if not match:
        raise ManifestError(f"{tag!r} is not a release tag like v1.2.3.")
    if not REPOSITORY.match(repository):
        raise ManifestError(f"{repository!r} is not an owner/name repository.")
    images = [Path(item) for item in _as_list(image)]
    expected = accepted_image_names(match.group(1))
    seen = set()
    assets = []
    base = f"https://github.com/{repository}/releases"
    for item in images:
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
    content = {
        "tag_name": tag,
        "html_url": f"{base}/tag/{tag}",
        "prerelease": False,
        "body": Path(notes).read_text(encoding="utf-8"),
        "assets": assets,
    }
    if changelog is not None:
        content["changelog"] = changelog_sections(changelog, tag)
    metadatas = [Path(item) for item in _as_list(metadata)]
    if metadatas:
        if len(metadatas) != len(images):
            raise ManifestError("Each disk image needs its own build metadata file.")
        architectures = []
        minimums = []
        for item, meta, asset in zip(images, metadatas, assets):
            facts = json.loads(Path(meta).read_text(encoding="utf-8"))
            expected_facts = {"image": item.name, "version": match.group(1), "size": item.stat().st_size,
                              "sha256": asset["digest"].removeprefix("sha256:"),
                              "bundle_identifier": "app.wallpapermachine"}
            if any(facts.get(key) != value for key, value in expected_facts.items()):
                raise ManifestError("Build metadata does not match the verified disk image and tag.")
            minimum = facts.get("minimum_system_version")
            listed = facts.get("architectures")
            if not isinstance(minimum, str) or not re.fullmatch(r"\d+(?:\.\d+){0,2}", minimum):
                raise ManifestError("Build metadata has no valid minimum macOS version.")
            named = next((arch for arch in ("universal", "arm64", "x86_64") if item.name.endswith(f"-{arch}.dmg")), None)
            if not isinstance(listed, list) or not listed or any(
                    arch not in ("arm64", "arm64e", "x86_64") for arch in listed):
                raise ManifestError("Build metadata has no supported architecture list.")
            if named == "universal":
                if "arm64" not in listed or "x86_64" not in listed:
                    raise ManifestError("Build metadata for a universal image must list arm64 and x86_64.")
            elif named not in listed:
                raise ManifestError("Build metadata architecture does not match the disk image name.")
            for arch in listed:
                if arch not in architectures:
                    architectures.append(arch)
            minimums.append(minimum)
        content.update(minimum_system_version=max(minimums, key=version_key), architectures=architectures,
                       bundle_identifier="app.wallpapermachine")
    return content


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tag", required=True, help="Version tag the release belongs to, like v1.2.3.")
    parser.add_argument("--repository", required=True, help="owner/name of the repository the release is in.")
    parser.add_argument("--notes", required=True, help="Markdown file that becomes the release body.")
    parser.add_argument("--changelog", help=f"CHANGELOG.md whose newest {CHANGELOG_SECTIONS} sections the manifest carries.")
    parser.add_argument("--metadata", action="append", default=[],
                        help="Digest-bound metadata for one disk image, in the same order as the images. Repeat for each architecture.")
    parser.add_argument("image", nargs="+", help="Disk images the release publishes, one per architecture.")
    args = parser.parse_args(argv)
    for path in (args.notes, *args.image, *([args.changelog] if args.changelog else []), *args.metadata):
        if not Path(path).is_file():
            raise ManifestError(f"Not a file: {path}")
    output = Path(args.image[0]).with_name(MANIFEST_NAME)
    images = args.image if len(args.image) > 1 else args.image[0]
    metadata = None
    if args.metadata:
        metadata = args.metadata if len(args.metadata) > 1 else args.metadata[0]
    content = manifest(args.tag, args.repository, args.notes, images, args.changelog, metadata)
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
