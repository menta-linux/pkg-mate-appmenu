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
SUITE_SUFFIX = {"trixie": "~deb13", "forky": "~deb14", "unstable": ""}


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


def latest_release(items, channel, suite, source_key):
    candidates = []
    for item in items:
        match = re.fullmatch(
            rf"build/{channel}/{suite}/{re.escape(source_key)}-(\d+)",
            item.get("tag_name", ""),
        )
        if match and not item.get("draft") and any(
            asset.get("name", "").endswith(".deb") for asset in item.get("assets", [])
        ):
            candidates.append((int(match.group(1)), item))
    return max(candidates, default=(0, None), key=lambda pair: pair[0])


def newest_channel_release(items, channel, suite):
    prefix = f"build/{channel}/{suite}/"
    candidates = [item for item in items if item.get("tag_name", "").startswith(prefix)
                  and item.get("assets") and not item.get("draft")]
    return max(candidates, key=lambda item: item.get("published_at") or "", default=None)


def version_compare(first, second):
    first_version, second_version = debian_upstream(first), debian_upstream(second)
    if subprocess.run(["dpkg", "--compare-versions", first_version, "gt", second_version]).returncode == 0:
        return 1
    if subprocess.run(["dpkg", "--compare-versions", first_version, "lt", second_version]).returncode == 0:
        return -1
    return 0


def version_tags(source, ancestor_of=None):
    tags = git(source, "tag", "--list").splitlines()
    valid = []
    for tag in tags:
        try:
            debian_upstream(tag)
            if ancestor_of and subprocess.run(
                ["git", "-C", str(source), "merge-base", "--is-ancestor", tag, ancestor_of]
            ).returncode != 0:
                continue
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
    parser.add_argument("--channel", choices=("nightly", "testing", "stable"), required=True)
    parser.add_argument("--suite", choices=tuple(SUITE_SUFFIX), required=True)
    parser.add_argument("--source-tag")
    parser.add_argument("--releases-file", help="offline release fixture for validation")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    source = args.source_dir.resolve()
    packaging = args.packaging_dir.resolve()
    tags = version_tags(source)
    if args.channel == "stable":
        final_tags = [tag for tag in tags if not re.search(r"(?:alpha|beta|rc)[0-9]+$", tag)]
        tag = final_tags[0] if final_tags else None
    elif args.channel == "testing":
        tag = args.source_tag or (tags[0] if tags else None)
    else:
        nightly_tags = version_tags(source, "HEAD")
        tag = nightly_tags[0] if nightly_tags else None
    if not tag or tag not in tags:
        raise ValueError(f"source tag was not found or is unsupported: {tag}")

    if args.channel == "nightly":
        source_ref = "HEAD"
        source_sha = git(source, "rev-parse", "HEAD")
        commit_date = git(source, "show", "-s", "--format=%cd", "--date=format:%Y%m%d%H%M%S", "HEAD")
        source_key = f"git-{commit_date}-{source_sha[:7]}"
        upstream = f"{debian_upstream(tag)}+git{commit_date}.{source_sha[:7]}"
    else:
        source_ref = tag
        source_sha = git(source, "rev-parse", f"{tag}^{{commit}}")
        source_key = tag
        upstream = debian_upstream(tag)
    packaging_sha = git(packaging, "rev-parse", "HEAD")
    published = releases(args.repository, args.releases_file)
    revision, latest = latest_release(published, args.channel, args.suite, source_key)
    prior = metadata(latest.get("body")) if latest else {}
    if prior.get("source_sha") == source_sha and prior.get("packaging_sha") == packaging_sha:
        print(f"Already published {args.channel}/{args.suite}/{source_key}-{revision} for these two commits")
        return 0

    revision += 1
    version = f"{upstream}-{revision}{SUITE_SUFFIX[args.suite]}"
    newest_build = newest_channel_release(published, args.channel, args.suite)
    previous_info = metadata(newest_build.get("body", "")) if newest_build else {}
    old_source = prior.get("source_sha") or previous_info.get("source_sha") or previous_tag(source, tag)
    old_packaging = prior.get("packaging_sha") or previous_info.get("packaging_sha")
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
            ["git", "-C", str(source), "archive", "--format=tar", f"--prefix={tree.name}/", source_ref],
            stdout=subprocess.PIPE,
        )
        compressor = subprocess.run(["xz", "-T0", "-9"], stdin=archive_process.stdout, stdout=destination)
        archive_process.stdout.close()
        if archive_process.wait() or compressor.returncode:
            raise RuntimeError("source archive creation failed")
    subprocess.run(["tar", "-xJf", str(archive), "-C", str(out)], check=True)
    shutil.copytree(packaging / "debian", tree / "debian")

    lines = [f"{NAME} ({version}) {args.suite}; urgency=medium", ""]
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

    info = {"channel": args.channel, "suite": args.suite, "source_tag": tag,
            "source_ref": source_ref, "source_key": source_key, "source_sha": source_sha,
            "packaging_sha": packaging_sha, "version": version, "revision": revision,
            "tree": str(tree), "archive": str(archive)}
    (out / "build.json").write_text(json.dumps(info, indent=2) + "\n")
    release_info = {key: info[key] for key in ("channel", "suite", "source_tag", "source_ref",
                                                  "source_key", "source_sha", "packaging_sha", "version", "revision")}
    (out / "release-body.md").write_text(
        f"{args.channel.title()} build for Debian {args.suite} from `{source_ref}`. "
        f"Package version: `{version}`.\n\n"
        f"<!-- mate-appmenu-build: {json.dumps(release_info, separators=(',', ':'))} -->\n"
    )
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as handle:
            handle.write(f"build=true\nversion={version}\n"
                         f"release_tag=build/{args.channel}/{args.suite}/{source_key}-{revision}\n")
    print(json.dumps(info, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, KeyError) as error:
        sys.exit(str(error))
