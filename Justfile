# What to build the examples from: "rhel" (the default) or "centos-stream",
# e.g. `just base=centos-stream build sealing`. Only examples with their own
# Justfile support another base; the others are RHEL only.
base := "rhel"

# Build one example.
# If the example has its own Justfile, delegate to its `build` recipe
# (which may do custom setup e.g. secrets). Otherwise run podman build.
build example:
    #!/bin/bash
    set -euo pipefail
    if [ -f "{{example}}/Justfile" ]; then
        just --justfile "{{example}}/Justfile" --working-directory "{{example}}" base="{{base}}" build
    elif [ "{{base}}" != rhel ]; then
        echo "error: {{example}} can only be built from RHEL, not base={{base}}" >&2
        exit 1
    else
        podman build -t "localhost/{{example}}:latest" "{{example}}"
    fi

# Build every example that contains a Containerfile. bootc-git isn't an
# example (it compiles bootc, which takes a while); see `just bootc-rpms`.
# With a base other than rhel, those that can only be built from RHEL are
# skipped.
build-all:
    #!/bin/bash
    set -euo pipefail
    for d in */; do
        [ -f "$d/Containerfile" ] || continue
        [ "${d%/}" = bootc-git ] && continue
        if [ "{{base}}" != rhel ] && [ ! -f "$d/Justfile" ]; then
            echo "Skipping ${d%/}: it can only be built from RHEL"
            continue
        fi
        just base="{{base}}" build "${d%/}"
    done

# Build bootc RPMs from git into an image and print its name (for BOOTC_RPMS)
bootc-rpms:
    @just --justfile bootc-git/Justfile --working-directory bootc-git build

# Pin bootc-git to the current head of bootc and the current buildroot image.
bootc-git-bump:
    @just --justfile bootc-git/Justfile --working-directory bootc-git bump
