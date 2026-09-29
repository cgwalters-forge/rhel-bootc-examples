#!/usr/bin/env python3
"""Print the buildroot image to build bootc in.

Usage: bootc-git/resolve-buildroot.py IMAGE

IMAGE is normally the digest-pinned reference from bootc-git/buildroot. It is
used as is when it's in local storage or can be pulled. quay.io garbage-collects
the old digests of a tag that is rebuilt as often as CentOS Stream's, so when
the registry says the pinned digest doesn't exist, this falls back to the tag
with a loud warning (and a GitHub Actions annotation) instead of failing. Any
other pull error (network, rate limit, authentication) is fatal: it says nothing
about the pin.
"""

import os
import re
import subprocess
import sys

# How podman reports a digest the registry doesn't have
NOT_FOUND = re.compile(r"manifest unknown|not found", re.IGNORECASE)


def warn(msg):
    print(f"\n**********\nwarning: {msg}\n**********\n", file=sys.stderr)
    # stdout is the image name, so the annotation goes to stderr, which the
    # Actions runner parses too
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::warning title=Stale bootc-git buildroot pin::{msg}", file=sys.stderr)


def resolve(image):
    tag, sep, _ = image.partition("@")
    if not sep:
        return image
    if subprocess.run(["podman", "image", "exists", image]).returncode == 0:
        return image
    r = subprocess.run(["podman", "pull", "-q", image],
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if r.returncode == 0:
        return image
    error = r.stderr.strip()
    if not NOT_FOUND.search(error):
        sys.exit(f"error: pulling the bootc buildroot {image} failed:\n{error}")
    warn(f"the registry no longer has the pinned bootc buildroot {image} "
         f"({error.splitlines()[-1] if error else 'no error output'}); "
         f"falling back to {tag}, so this build isn't reproducible. "
         "Run 'just bootc-git-bump' to update the pin.")
    return tag


def main():
    if len(sys.argv) != 2:
        sys.exit(f"usage: {sys.argv[0]} IMAGE")
    print(resolve(sys.argv[1]))


if __name__ == "__main__":
    main()
