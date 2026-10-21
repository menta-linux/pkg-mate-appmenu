#!/usr/bin/env python3
"""Plan a tagged Debian build, then prepare its source and changelog."""

import argparse
from datetime import datetime, timezone
from email.utils import format_datetime
from functools import cmp_to_key
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.request

NAME = "mate-appmenu"
MAINTAINER = "Kerem Soke <keremsoke@gmail.com>"


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def debian_upstream(tag):
    tag = tag.removeprefix("v")
    match = re.fullmatch(r"([0-9][0-9A-Za-z.+]*?)(?:[-.]?(alpha|beta|rc)([0-9]+))?", tag)
    if not match:
        raise ValueError(f"unsupported upstream tag: {tag}")
    base, stage, number = match.groups()
    return base + (f"~{stage}{number}" if stage else "")


def releases(repository, fixture):
    if fixture:
        return json.loads(Path(fixture).read_text())
    token = os.environ["GH_TOKEN"]
    all_releases = []
    for page in range(1, 100):
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repository}/releases?per_page=100&page={page}",
            headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req) as response:
            batch = json.load(response)
        all_releases.extend(batch)
        if len(batch) < 100:
            break
    return all_releases


def metadata(body):
    match = re.search(r"<!-- mate-appmenu-build: (\{[^\n]*\}) -->", body or "")
    return json.loads(match.group(1)) if match else {}


def latest_release(items, tag):
    candidates = []
    for item in items:
        match = re.fullmatch(rf"build/{re.escape(tag)}-(\d+)", item.get("tag_name", ""))
        if match and not item.get("draft") and any(
            asset.get("name", "").endswith(".deb") for asset in item.get("assets", [])
        ):
            candidates.append((int(match.group(1)), item))
    return max(candidates, default=(0, None), key=lambda pair: pair[0])


def version_compare(first, second):
    first_version, second_version = debian_upstream(first), debian_upstream(second)
    if subprocess.run(["dpkg", "--compare-versions", first_version, "gt", second_version]).returncode == 0:
        return 1
    if subprocess.run(["dpkg", "--compare-versions", first_version, "lt", second_version]).returncode == 0:
        return -1
    return 0


def version_tags(source):
    tags = git(source, "tag", "--list").splitlines()
    valid = []
    for tag in tags:
        try:
            debian_upstream(tag)
            valid.append(tag)
        except ValueError:
            continue
    return sorted(valid, key=cmp_to_key(version_compare), reverse=True)


def previous_tag(source, tag):
    for candidate in version_tags(source):
        if candidate == tag or version_compare(candidate, tag) >= 0:
            continue
        if subprocess.run(["git", "-C", str(source), "merge-base", "--is-ancestor", candidate, tag]).returncode == 0:
            return candidate
    return None


def commit_subjects(repo, old, new):
    if old == new:
        return []
    span = f"{old}..{new}" if old else new
    output = git(repo, "log", "--format=%s", "--no-merges", span)
    return [line.strip().replace("  ", " ") for line in output.splitlines() if line.strip()]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--packaging-dir", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--source-tag")
    parser.add_argument("--releases-file", help="offline release fixture for validation")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    source = args.source_dir.resolve()
    packaging = args.packaging_dir.resolve()
    tags = version_tags(source)
    tag = args.source_tag or (tags[0] if tags else None)
    if not tag or tag not in tags:
        raise ValueError(f"source tag was not found or is unsupported: {tag}")
    upstream = debian_upstream(tag)
    source_sha = git(source, "rev-parse", f"{tag}^{{commit}}")
    packaging_sha = git(packaging, "rev-parse", "HEAD")
    published = releases(args.repository, args.releases_file)
    revision, latest = latest_release(published, tag)
    prior = metadata(latest.get("body")) if latest else {}
    if prior.get("source_sha") == source_sha and prior.get("packaging_sha") == packaging_sha:
        print(f"Already published {tag}-{revision} for these two commits")
        return 0

    revision += 1
    version = f"{upstream}-{revision}"
    old_source = prior.get("source_sha") or previous_tag(source, tag)
    previous_builds = [item for item in published if item.get("tag_name", "").startswith("build/")
                       and item.get("assets") and not item.get("draft")]
    newest_build = max(previous_builds, key=lambda item: item.get("published_at") or "", default=None)
    old_packaging = prior.get("packaging_sha")
    if not old_packaging and newest_build:
        old_packaging = metadata(newest_build.get("body", "")).get("packaging_sha")
    source_commits = commit_subjects(source, old_source, source_sha)
    packaging_commits = commit_subjects(packaging, old_packaging, packaging_sha)
    out = args.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    tree = out / f"{NAME}-{upstream}"
    if tree.exists():
        shutil.rmtree(tree)
    archive = out / f"{NAME}_{upstream}.orig.tar.xz"
    with archive.open("wb") as destination:
        archive_process = subprocess.Popen(
            ["git", "-C", str(source), "archive", "--format=tar", f"--prefix={tree.name}/", tag],
            stdout=subprocess.PIPE,
        )
        compressor = subprocess.run(["xz", "-T0", "-9"], stdin=archive_process.stdout, stdout=destination)
        archive_process.stdout.close()
        if archive_process.wait() or compressor.returncode:
            raise RuntimeError("source archive creation failed")
    subprocess.run(["tar", "-xJf", str(archive), "-C", str(out)], check=True)
    shutil.copytree(packaging / "debian", tree / "debian")

    lines = [f"{NAME} ({version}) trixie; urgency=medium", ""]
    if source_commits:
        lines.append("  * Upstream changes:")
        lines.extend(f"    - {subject}" for subject in source_commits)
    if packaging_commits:
        lines.append("  * Packaging changes:")
        lines.extend(f"    - {subject}" for subject in packaging_commits)
    if not source_commits and not packaging_commits:
        lines.append("  * Rebuild the tagged source with updated packaging.")
    date = format_datetime(datetime.now(timezone.utc).astimezone())
    lines.extend(["", f" -- {MAINTAINER}  {date}"])
    (tree / "debian/changelog").write_text("\n".join(lines) + "\n")

    info = {"source_tag": tag, "source_sha": source_sha, "packaging_sha": packaging_sha,
            "version": version, "revision": revision, "tree": str(tree), "archive": str(archive)}
    (out / "build.json").write_text(json.dumps(info, indent=2) + "\n")
    release_info = {key: info[key] for key in ("source_tag", "source_sha", "packaging_sha", "version", "revision")}
    (out / "release-body.md").write_text(
        f"Debian Trixie build of `{tag}`. Package version: `{version}`.\n\n"
        f"<!-- mate-appmenu-build: {json.dumps(release_info, separators=(',', ':'))} -->\n"
    )
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as handle:
            handle.write(f"build=true\nversion={version}\nrelease_tag=build/{tag}-{revision}\n")
    print(json.dumps(info, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, KeyError) as error:
        sys.exit(str(error))
