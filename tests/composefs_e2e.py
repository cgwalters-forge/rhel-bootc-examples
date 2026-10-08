#!/usr/bin/env python3
"""End-to-end test of a composefs example.

Builds three versions of the example (bootc container lint runs as part of
that), turns the first into a qcow2 disk image with image-builder, and boots
it under QEMU/KVM, with Secure Boot and our keys enrolled for the sealed
variant. Checks that a composefs deployment booted, then switches to the
second build through a local registry, upgrades to the third, and checks
each after a reboot.

Usage: tests/composefs_e2e.py sealed|unsealed

Runs unprivileged. Needs podman, just, bcvk, QEMU with KVM, edk2 OVMF firmware
and, for sealed, openssl and virt-fw-vars (python3-virt-firmware).
Environment:
  BASE_IMAGE      base image (default: quay.io/centos-bootc/centos-bootc:stream10)
  BOOTC_RPMS      image with bootc RPMs from git (default: built by
                  `just bootc-rpms`; "none" uses the distribution's bootc)
  IB_IMAGE        image-builder container image (default: the released
                  ghcr.io/osbuild/image-builder-cli, unsealed only; see
                  SEALED_IB_REQUIREMENT)
  E2E_WORKDIR     where logs and disk images go (default: target/e2e-<variant>)
  E2E_REGISTRY_PORT, E2E_SSH_PORT  host ports to use (default: 5000, 2222)
  E2E_SUFFIX      appended to the image and registry container names, for
                  running several tests at once
  COMPOSEFS_BACKEND_CONFIG  passed on to composefs/Justfile (unsealed only)
"""

import argparse
import base64
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

TOPDIR = Path(__file__).resolve().parent.parent
EXAMPLES = {"sealed": "sealing", "unsealed": "composefs"}
DEFAULT_BASE_IMAGE = "quay.io/centos-bootc/centos-bootc:stream10"
DEFAULT_IB_IMAGE = "ghcr.io/osbuild/image-builder-cli:latest"
SEALED_IB_REQUIREMENT = (
    "the sealed variant needs an image-builder with "
    "https://github.com/osbuild/image-builder/pull/2719 (merged after v84, not released yet): "
    "without it the root partition gets a generic type and the UKI can't find it. "
    "Build one from image-builder's main (podman build -f Containerfile) and set IB_IMAGE."
)
REGISTRY_IMAGE = "docker.io/library/registry:2"
# The packages that the second and third builds add
BUILD_PACKAGES = {1: [], 2: ["strace"], 3: ["strace", "tmux"]}
# EFI global variable GUID, also used as the owner of the enrolled certificates
EFI_GLOBAL_GUID = "8be4df61-93ca-11d2-aa0d-00e098032b8c"
# Discoverable Partitions Specification root type for x86-64, the only
# architecture the QEMU setup below supports
DPS_ROOT_X86_64 = "4f68bce3-e8cd-4db1-96e7-fbcaf984b709"
# Where distributions install OVMF, and the (code, vars) file names they use
OVMF_DIRS = ["/usr/share/edk2/ovmf", "/usr/share/OVMF", "/usr/share/edk2/x64"]
OVMF_SECURE = [("OVMF_CODE.secboot.fd", "OVMF_VARS.secboot.fd"),
               ("OVMF_CODE_4M.secboot.fd", "OVMF_VARS_4M.fd"),
               ("OVMF_CODE.secboot.4m.fd", "OVMF_VARS.4m.fd")]
OVMF_PLAIN = [("OVMF_CODE.fd", "OVMF_VARS.fd"),
              ("OVMF_CODE_4M.fd", "OVMF_VARS_4M.fd"),
              ("OVMF_CODE.4m.fd", "OVMF_VARS.4m.fd")]
SSH_TIMEOUT = 600
BOOT_TIMEOUT = 600


class E2EError(Exception):
    pass


def log(msg):
    print(f"==> {msg}", file=sys.stderr, flush=True)


def run(cmd, **kwargs):
    """Run a command, failing on a nonzero exit."""
    return subprocess.run(cmd, check=True, **kwargs)


def output(cmd, **kwargs):
    return run(cmd, stdout=subprocess.PIPE, text=True, **kwargs).stdout.strip()


def just(directory, recipe, env=None):
    return output(["just", "--justfile", str(directory / "Justfile"),
                   "--working-directory", str(directory), recipe],
                  env={**os.environ, **(env or {})})


def find_qemu():
    for name in ["qemu-kvm", "qemu-system-x86_64"]:
        if path := shutil.which(name):
            return path
    # RHEL and CentOS install it outside of PATH
    if os.access("/usr/libexec/qemu-kvm", os.X_OK):
        return "/usr/libexec/qemu-kvm"
    raise E2EError("QEMU not found: install qemu-kvm or qemu-system-x86_64")


def find_ovmf(secure):
    for d in OVMF_DIRS:
        for code, vars_ in OVMF_SECURE if secure else OVMF_PLAIN:
            if (Path(d) / code).exists() and (Path(d) / vars_).exists():
                return Path(d) / code, Path(d) / vars_
    kind = "Secure Boot capable " if secure else ""
    raise E2EError(f"no {kind}OVMF firmware found in {', '.join(OVMF_DIRS)}: install edk2-ovmf")


class Test:
    def __init__(self, variant):
        self.variant = variant
        self.sealed = variant == "sealed"
        self.example = TOPDIR / EXAMPLES[variant]
        # Distinguishes the images and registry of concurrent runs
        suffix = os.environ.get("E2E_SUFFIX", "")
        self.image = f"localhost/{EXAMPLES[variant]}-e2e{suffix}"
        self.workdir = Path(os.environ.get("E2E_WORKDIR", TOPDIR / "target" / f"e2e-{variant}"))
        self.base_image = os.environ.get("BASE_IMAGE", DEFAULT_BASE_IMAGE)
        self.ib_image = os.environ.get("IB_IMAGE")
        if not self.ib_image:
            if self.sealed:
                raise E2EError(SEALED_IB_REQUIREMENT)
            self.ib_image = DEFAULT_IB_IMAGE
        self.registry_port = int(os.environ.get("E2E_REGISTRY_PORT", "5000"))
        self.registry_name = f"composefs-e2e-registry-{variant}{suffix}"
        # How the guest reaches the host with QEMU's user networking
        self.registry = f"10.0.2.2:{self.registry_port}"
        self.target_ref = f"{self.registry}/e2e:latest"
        self.ssh_port = int(os.environ.get("E2E_SSH_PORT", "2222"))
        self.ssh_key = self.workdir / "id_e2e"
        self.qemu = None
        self.bootc_rpms = None

    # --- building

    def build(self, n):
        packages = BUILD_PACKAGES[n]
        tag = f"{self.image}:{n}"
        log(f"Building {tag} (extra packages: {' '.join(packages) or 'none'})")
        env = {"HOST_IMAGE": tag, "EXTRA_PACKAGES": " ".join(packages),
               "BASE_IMAGE": self.base_image}
        if self.bootc_rpms:
            env["BOOTC_RPMS"] = self.bootc_rpms
        run(["just", "--justfile", str(self.example / "Justfile"),
             "--working-directory", str(self.example), "build"],
            env={**os.environ, **env})
        log(f"Linting the final image {tag}")
        run(["just", "--justfile", str(self.example / "Justfile"),
             "--working-directory", str(self.example), "lint"],
            env={**os.environ, "HOST_IMAGE": tag})

    def check_bootc_version(self):
        """Guard against stale build caches: the image must have the bootc we built."""
        have = output(["podman", "run", "--rm", f"{self.image}:1", "rpm", "-q", "bootc"])
        (self.workdir / "bootc-version.txt").write_text(have + "\n")
        log(f"bootc in the image: {have}")
        if not self.bootc_rpms:
            return
        want = output(["podman", "run", "--rm",
                       "--mount", f"type=image,src={self.bootc_rpms},dst=/run/bootc-rpms",
                       f"{self.image}:1", "sh", "-c",
                       "rpm -qp --qf '%{NVRA}\\n' /run/bootc-rpms/bootc-[0-9]*.rpm"])
        if have != want:
            raise E2EError(f"the image has {have}, not {want} from {self.bootc_rpms}")

    def make_disk(self):
        """Build a qcow2 of build 1 with image-builder.

        image-builder (osbuild) needs root, and its --in-vm doesn't cover the
        pipeline that sets up the bootc build root, so run it as root in an
        ephemeral VM, with the images passed in as archives and a scratch
        disk for its container storage.
        """
        log(f"Building a qcow2 disk image from {self.image}:1 with {self.ib_image}")
        ibdir = self.workdir / "ib"
        shutil.rmtree(ibdir, ignore_errors=True)
        (ibdir / "out").mkdir(parents=True)
        if subprocess.run(["podman", "pull", "-q", self.ib_image],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode != 0:
            run(["podman", "image", "exists", self.ib_image])
        run(["podman", "save", "-q", "--format", "oci-archive",
             "-o", str(ibdir / "image-builder.ociarchive"), self.ib_image])
        run(["podman", "save", "-q", "--format", "oci-archive",
             "-o", str(ibdir / "source.ociarchive"), f"{self.image}:1"])
        with open(ibdir / "scratch.raw", "wb") as f:
            f.truncate(60 << 30)
        w = "/run/virtiofs-mnt-work"
        store = "/var/lib/containers"
        script = f"""set -xeuo pipefail
mkfs.xfs -f -q /dev/disk/by-id/virtio-scratch
mkdir -p {store} && mount /dev/disk/by-id/virtio-scratch {store}
mkdir -p {store}/tmp {store}/out
export TMPDIR={store}/tmp
podman load -q -i {w}/image-builder.ociarchive
podman load -q -i {w}/source.ociarchive
podman run --rm --privileged --security-opt label=type:unconfined_t \\
    -v {store}/out:/output -v {store}/storage:{store}/storage \\
    {shlex.quote(self.ib_image)} build --bootc-ref {shlex.quote(self.image + ':1')} \\
    --bootc-default-fs ext4 --output-dir /output qcow2
find {store}/out -name '*.qcow2' -exec cp --sparse=always {{}} {w}/out/ \\;
"""
        (ibdir / "run.sh").write_text(script)
        logfile = self.workdir / "image-builder.log"
        with open(logfile, "w") as f:
            r = subprocess.run(["bcvk", "ephemeral", "run-ssh", "--memory", "8G", "--vcpus", "4",
                                "--bind", f"{ibdir}:work",
                                "--mount-disk-file", f"{ibdir}/scratch.raw:scratch",
                                self.base_image, "--", "bash", f"{w}/run.sh"],
                               stdout=f, stderr=subprocess.STDOUT, check=False)
        disks = sorted((ibdir / "out").glob("*.qcow2"))
        if r.returncode != 0 or len(disks) != 1:
            tail = "".join(logfile.read_text(errors="replace").splitlines(keepends=True)[-30:])
            raise E2EError(f"image-builder produced {len(disks)} qcow2 images "
                           f"(exit status {r.returncode}); see {logfile}:\n{tail}")
        disk = self.workdir / "disk.qcow2"
        disks[0].rename(disk)
        shutil.rmtree(ibdir)
        return disk

    # --- registry

    def start_registry(self):
        log("Starting a local registry for the upgrade images")
        self.stop_registry()
        run(["podman", "run", "-d", "--name", self.registry_name,
             "-p", f"{self.registry_port}:5000", REGISTRY_IMAGE], stdout=subprocess.DEVNULL)

    def stop_registry(self):
        subprocess.run(["podman", "rm", "-f", self.registry_name],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

    def push(self, n):
        """Push build n as the target ref, returning its manifest digest."""
        digestfile = self.workdir / f"digest-{n}"
        run(["podman", "push", "-q", "--tls-verify=false", "--digestfile", str(digestfile),
             f"{self.image}:{n}", f"docker://localhost:{self.registry_port}/e2e:latest"])
        return digestfile.read_text().strip()

    # --- the VM

    def firmware(self):
        code, vars_template = find_ovmf(self.sealed)
        vars_ = self.workdir / "vars.fd"
        if self.sealed:
            keys = TOPDIR / "sealing" / "target" / "keys"
            run(["virt-fw-vars", "--input", str(vars_template), "--output", str(vars_),
                 "--set-pk", EFI_GLOBAL_GUID, str(keys / "sb-PK.crt"),
                 "--add-kek", EFI_GLOBAL_GUID, str(keys / "sb-KEK.crt"),
                 "--add-db", EFI_GLOBAL_GUID, str(keys / "sb-db.crt"),
                 "--secure-boot", "--no-microsoft"])
        else:
            shutil.copyfile(vars_template, vars_)
        return code, vars_

    def boot(self, disk):
        log(f"Booting {disk}")
        for p in [self.ssh_key, self.ssh_key.with_suffix(".pub")]:
            p.unlink(missing_ok=True)
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.ssh_key)])
        pubkey = base64.b64encode(self.ssh_key.with_suffix(".pub").read_bytes()).decode()
        code, vars_ = self.firmware()
        self.qemu = subprocess.Popen([
            find_qemu(), "-machine", "q35,smm=on,accel=kvm", "-cpu", "host",
            "-m", "4096", "-smp", "2", "-nographic",
            "-global", "driver=cfi.pflash01,property=secure,value=on",
            "-drive", f"if=pflash,format=raw,unit=0,readonly=on,file={code}",
            "-drive", f"if=pflash,format=raw,unit=1,file={vars_}",
            "-drive", f"file={disk},if=virtio,format=qcow2",
            "-netdev", f"user,id=n0,hostfwd=tcp:127.0.0.1:{self.ssh_port}-:22",
            "-device", "virtio-net-pci,netdev=n0",
            # image-builder can't customize images with a UKI, so the SSH
            # key goes in as a systemd credential over SMBIOS.
            "-smbios", f"type=11,value=io.systemd.credential.binary:ssh.authorized_keys.root={pubkey}",
            "-serial", f"file:{self.workdir / 'firmware.log'}",
            "-device", "virtio-serial-pci",
            "-chardev", f"file,id=hvc0,path={self.workdir / 'console.log'}",
            "-device", "virtconsole,chardev=hvc0",
            "-monitor", "none", "-display", "none",
        ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)

    def stop_vm(self):
        if self.qemu and self.qemu.poll() is None:
            self.qemu.terminate()
            self.qemu.wait()

    def ssh(self, command, check=True, capture=False, timeout=None):
        cmd = ["ssh", "-i", str(self.ssh_key), "-p", str(self.ssh_port),
               "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
               "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
               "root@127.0.0.1", command]
        r = subprocess.run(cmd, stdout=subprocess.PIPE if capture else None, text=True,
                           timeout=timeout, check=False)
        if check and r.returncode != 0:
            raise E2EError(f"in the guest, {command!r} failed with exit status {r.returncode}")
        return r

    def boot_id(self):
        """The guest's boot ID, or None while it isn't reachable."""
        if self.qemu.poll() is not None:
            raise E2EError(f"QEMU exited; see {self.workdir / 'console.log'}")
        try:
            r = self.ssh("cat /proc/sys/kernel/random/boot_id", check=False, capture=True,
                         timeout=30)
        except subprocess.TimeoutExpired:
            return None
        return r.stdout.strip() if r.returncode == 0 else None

    def wait_for_boot(self, previous=None):
        """Wait until the guest is up with a boot ID other than previous."""
        deadline = time.monotonic() + SSH_TIMEOUT
        while time.monotonic() < deadline:
            boot_id = self.boot_id()
            if boot_id and boot_id != previous:
                return boot_id
            time.sleep(5)
        raise E2EError(f"timed out waiting for the guest to boot; see {self.workdir / 'console.log'}")

    def reboot(self, previous):
        try:
            self.ssh("systemctl reboot", check=False, timeout=60)
        except subprocess.TimeoutExpired:
            pass
        return self.wait_for_boot(previous)

    def check_boot(self, expected_ref, expected_digest, packages):
        """Check what's booted: the image reference, the digest if known, and packages."""
        r = self.ssh(f"timeout {BOOT_TIMEOUT} systemctl is-system-running --wait", check=False,
                     capture=True)
        if r.stdout.strip() != "running":
            self.ssh("systemctl --failed", check=False)
            raise E2EError(f"the system is {r.stdout.strip() or 'unreachable'}, not running")
        self.ssh("systemctl is-active multi-user.target")
        self.ssh("cat /proc/cmdline; findmnt /; bootc status")
        status = json.loads(self.ssh("bootc status --json", capture=True).stdout)
        (self.workdir / "status.json").write_text(json.dumps(status, indent=2))
        booted = status["status"]["booted"]
        if not booted.get("composefs"):
            raise E2EError(f"the booted deployment isn't composefs: {booted}")
        image = booted["image"]["image"]["image"]
        if image != expected_ref:
            raise E2EError(f"booted {image}, expected {expected_ref}")
        digest = booted["image"]["imageDigest"]
        if expected_digest and digest != expected_digest:
            raise E2EError(f"booted digest {digest}, expected {expected_digest}")
        opts = self.ssh("findmnt -n -o OPTIONS /", capture=True).stdout
        if "verity=require" not in opts:
            raise E2EError(f"/ isn't a verity-checked composefs mount: {opts.strip()}")
        self.ssh("lsblk -o NAME,SIZE,PARTTYPE,PARTTYPENAME,FSTYPE,MOUNTPOINTS")
        parttype = self.ssh("lsblk -n -o PARTTYPE \"$(findmnt -n -o SOURCE -T /sysroot)\"",
                            capture=True).stdout.strip().lower()
        log(f"root partition type: {parttype}")
        # image-builder gives only UKI images the DPS root type (which gpt-auto needs
        # without root=); a BLS image's command line has root=.
        if self.sealed and parttype != DPS_ROOT_X86_64:
            raise E2EError(f"the root partition type is {parttype}, not DPS {DPS_ROOT_X86_64}")
        if self.sealed:
            sb = self.ssh(f"od -An -t u1 -j4 -N1 /sys/firmware/efi/efivars/SecureBoot-{EFI_GLOBAL_GUID}",
                          capture=True).stdout.split()
            if sb != ["1"]:
                raise E2EError("Secure Boot isn't enabled")
        if packages:
            self.ssh(f"rpm -q {' '.join(packages)}")
        log(f"OK: booted a composefs deployment of {image} ({digest})")

    # --- the whole thing

    def run(self):
        self.workdir.mkdir(parents=True, exist_ok=True)
        rpms = os.environ.get("BOOTC_RPMS", "")
        if not rpms:
            log("Building bootc RPMs from git")
            rpms = just(TOPDIR, "bootc-rpms")
        if rpms != "none":
            self.bootc_rpms = rpms
            log(f"Using bootc RPMs from {rpms}")

        if self.sealed and not (TOPDIR / "sealing/target/keys/sb-PK.crt").exists():
            log("Generating Secure Boot keys")
            run(["just", "--justfile", str(self.example / "Justfile"),
                 "--working-directory", str(self.example), "keygen"])

        for n in BUILD_PACKAGES:
            self.build(n)
        self.check_bootc_version()
        disk = self.make_disk()

        try:
            self.start_registry()
            digest2 = self.push(2)
            self.boot(disk)
            log("Checking the first boot")
            boot_id = self.wait_for_boot()
            # image-builder installed from local storage; its digest isn't comparable
            self.check_boot(f"{self.image}:1", None, BUILD_PACKAGES[1])

            log(f"Switching to {self.target_ref} (build 2)")
            self.ssh("mkdir -p /etc/containers/registries.conf.d && cat > "
                     "/etc/containers/registries.conf.d/50-e2e.conf <<'EOF'\n"
                     f"[[registry]]\nlocation = \"{self.registry}\"\ninsecure = true\nEOF")
            self.ssh(f"bootc switch {self.target_ref}")
            boot_id = self.reboot(boot_id)
            self.check_boot(self.target_ref, digest2, BUILD_PACKAGES[2])

            log("Upgrading to build 3")
            digest3 = self.push(3)
            self.ssh("bootc upgrade")
            boot_id = self.reboot(boot_id)
            self.check_boot(self.target_ref, digest3, BUILD_PACKAGES[3])
        finally:
            self.stop_vm()
            self.stop_registry()
        log(f"PASS: {self.variant} composefs end-to-end test")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("variant", choices=sorted(EXAMPLES))
    args = parser.parse_args()
    try:
        Test(args.variant).run()
    except (E2EError, subprocess.CalledProcessError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
