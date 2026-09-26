# composefs backend (unsealed)

This example builds a bootc image for bootc's
[composefs backend](https://bootc.dev/bootc/experimental-composefs.html),
which stores deployments in a composefs repository instead of ostree. It
keeps the regular boot chain (shim, GRUB and a kernel plus initramfs); see
[sealing](../sealing) for the variant where a signed UKI binds the root
filesystem to Secure Boot.

The root filesystem is ext4, because composefs relies on fs-verity.

Like the other examples it builds from `rhel10/rhel-bootc:10.2`, which needs
a subscribed host and a login to registry.redhat.io; `just base=centos-stream
build` builds from CentOS Stream 10 without one.
It has only been tested on CentOS Stream 10, with bootc from git (below):
the composefs backend is Tech Preview in RHEL 10.2's bootc.

## Build and boot

```sh
just build
just bcvk-test
```

`bcvk-test` installs the image with `bootc install to-disk --composefs-backend`
in a VM, checks that `/` is a composefs mount and that `bootc status` shows a
composefs deployment.

## Disk images

bootc-image-builder and Anaconda call `bootc install` without
`--composefs-backend`, so for them the image itself has to ask for the
composefs backend. Build with `COMPOSEFS_BACKEND_CONFIG=1` to add
`composefs-backend = true` to its install configuration.

That doesn't work with any released or merged code yet. It needs:
- bootc support for the `composefs-backend` key (not merged yet; bootc
  without it rejects the image's install configuration);
- image-builder support for installing such an image with the composefs
  backend (not written upstream yet; today it installs it with ostree);
- a bootc fix for upgrades on disks with a separate `/boot` partition, as
  image-builder makes them (not merged yet).

## bootc from git

To try unreleased bootc changes, build bootc RPMs from git first (see
[bootc-git](../bootc-git/Containerfile)) and pass them in:

```sh
BOOTC_RPMS=$(just ../bootc-rpms) just build
```
