"""Build pinned native WebRTC/RTP plugins against installed GStreamer 1.24.2.

Requires the documented host packages. Uses an isolated pinned Rust toolchain;
does not install system packages, alter shell startup or start any media source.
"""
# Copyright (c) 2026 Paul N. Beuchat. MIT License.
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import urllib.request

VERSION = "0.14.5"
RUST = "1.90.0"
RUST_HASH = "59f1883fcdd2d7243d2fd1ed19f22e05219251227abacfa3d656aebfbcc5e838"
CRATES = {
    "gst-plugin-webrtc": "81dcf7b73db298779b5216c02b1f862a010dcec9eac51799b10fbb89e447c0c9",
    "gst-plugin-rtp": "ea0b5775d49e2f58b43e2fab9154273a8848a467cbf0d9573ec97c4dc2d51401",
}


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download(url, checksum, cache):
    destination = cache / checksum
    if not destination.exists():
        temporary = cache / (checksum + ".part-" + str(os.getpid()))
        try:
            with urllib.request.urlopen(url, timeout=90) as source, temporary.open("xb") as output:
                shutil.copyfileobj(source, output)
            if sha(temporary) != checksum:
                raise ValueError("Downloaded source checksum mismatch: " + url)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    if sha(destination) != checksum:
        raise ValueError("Corrupt native build cache: " + str(destination))
    return destination


def run(command, *, env=None, capture=False):
    print("+", " ".join(map(str, command)), flush=True)
    return subprocess.run(list(map(str, command)), env=env, check=True, text=True,
                          stdout=subprocess.PIPE if capture else None).stdout


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True, help="New disposable build directory")
    parser.add_argument("--cache", type=Path, required=True, help="Hash-verified download cache")
    parser.add_argument("--destination", type=Path, required=True, help="New independent native prefix")
    args = parser.parse_args(argv)
    if sys.version_info[:2] != (3, 12) or os.uname().machine != "aarch64":
        parser.error("This recipe requires CPython 3.12 on aarch64")
    work, cache, destination = args.work.resolve(), args.cache.resolve(), args.destination.resolve()
    if work.exists() or destination.exists():
        parser.error("Work and destination must be absent; partial builds are never accepted as installed")
    versions = {}
    for package in ("gstreamer-1.0", "gstreamer-app-1.0", "gstreamer-video-1.0", "gstreamer-webrtc-1.0", "gstreamer-rtp-1.0"):
        versions[package] = run(["pkg-config", "--modversion", package], capture=True).strip()
        if versions[package] != "1.24.2":
            raise ValueError("This selected native recipe requires GStreamer 1.24.2: " + package)
    run(["gst-inspect-1.0", "nicesink"], capture=True)
    run([sys.executable, "-c", "import gi; gi.require_version('Gst', '1.0'); from gi.repository import Gst; Gst.init(None); import gi.overrides.Gst"], capture=True)
    work.mkdir(parents=True, mode=0o700)
    cache.mkdir(parents=True, exist_ok=True)
    rust_name = f"rust-{RUST}-aarch64-unknown-linux-gnu"
    rust_archive = download(f"https://static.rust-lang.org/dist/{rust_name}.tar.xz", RUST_HASH, cache)
    with tarfile.open(rust_archive) as archive:
        archive.extractall(work, filter="data")
    toolchain = work / "toolchain"
    run(["bash", work / rust_name / "install.sh", "--prefix=" + str(toolchain), "--without=rust-docs", "--disable-ldconfig"])
    env = dict(os.environ, CARGO_HOME=str(work / "cargo"), CARGO_TARGET_DIR=str(work / "target"),
        CARGO_PROFILE_RELEASE_DEBUG="0", CARGO_PROFILE_RELEASE_LTO="false", PATH=str(toolchain / "bin") + ":/usr/bin:/bin")
    rust_version = run([toolchain / "bin/rustc", "--version"], env=env, capture=True).strip()
    if not rust_version.startswith("rustc " + RUST + " "):
        raise ValueError("Unexpected isolated Rust compiler")
    locks = {}
    for name, checksum in CRATES.items():
        source_archive = download(f"https://static.crates.io/crates/{name}/{name}-{VERSION}.crate", checksum, cache)
        with tarfile.open(source_archive) as archive:
            archive.extractall(work, filter="data")
        source = work / (name + "-" + VERSION)
        locks[name] = sha(source / "Cargo.lock")
        run(["nice", "-n", "10", toolchain / "bin/cargo", "build", "--release", "--locked",
             "--no-default-features", "--jobs", "2", "--manifest-path", source / "Cargo.toml"], env=env)
        if locks[name] != sha(source / "Cargo.lock"):
            raise ValueError("Cargo lock changed during native build")
    run([sys.executable, Path(__file__).with_name("prepare-av-native.py"), "--runtime", work,
         "--system-root", "/", "--destination", destination])
    gst_env = dict(os.environ, GST_PLUGIN_PATH=str(destination / "gstreamer"), GST_REGISTRY=str(work / "gst-registry.bin"))
    for plugin in ("webrtcsink", "rtpgccbwe", "nicesink"):
        run(["gst-inspect-1.0", plugin], env=gst_env, capture=True)
    for library in (destination / "gstreamer").glob("*.so"):
        if "not found" in run(["ldd", library], capture=True):
            raise ValueError("Unresolved native dependency: " + str(library))
    provenance = dict(schema_version=1, gstreamer=versions, rust=rust_version, rust_archive_sha256=RUST_HASH,
                      crates=CRATES, cargo_locks=locks, factories=["webrtcsink", "rtpgccbwe", "nicesink"])
    (destination / "build.json").write_text(json.dumps(provenance, indent=2) + "\n")
    print(json.dumps(dict(native_prefix=str(destination), result="validated")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
