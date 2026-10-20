"""The macOS packager verifies the exact update archive it signs, not only the bundle directory."""

import importlib.util
import platform
import plistlib
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(platform.system() != "Darwin", reason="codesign is macOS-only")


def packager():
    spec = importlib.util.spec_from_file_location("package_macos", ROOT / "scripts/package_macos.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # Import must not start a build or signing run.
    return module


def signed_bundle(directory, version="0.3.0", identifier="com.opendialer.desktop", sidecars=None):
    app = directory / "OpenDialer.app"
    (app / "Contents/MacOS").mkdir(parents=True)
    sidecars = ("opendialer-backend", "opendialer-trainer") if sidecars is None else sidecars
    for name in ("OpenDialer", *sidecars):
        executable = app / "Contents/MacOS" / name
        shutil.copyfile("/usr/bin/true", executable)
        executable.chmod(0o755)
    (app / "Contents/Info.plist").write_bytes(
        plistlib.dumps(
            {
                "CFBundleExecutable": "OpenDialer",
                "CFBundleIdentifier": identifier,
                "CFBundleName": "OpenDialer",
                "CFBundlePackageType": "APPL",
                "CFBundleShortVersionString": version,
                "CFBundleVersion": version,
            }
        )
    )
    sealed = {name: app / "Contents/MacOS" / name for name in sidecars}
    for path in sealed.values():
        subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(path)], check=True)  # noqa: S603
    subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(app)], check=True)  # noqa: S603
    return app, sealed


def archive_of(app, archive, extra=None):
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(app, arcname="OpenDialer.app")
        if extra:
            tar.add(extra, arcname=extra.name)
    return archive


def test_shipped_archive_is_extracted_and_verified(tmp_path):
    app, backend = signed_bundle(tmp_path / "bundle")
    archive = archive_of(app, tmp_path / "OpenDialer.app.tar.gz")
    packager().verify_archive(archive, "0.3.0", backend, tmp_path / "scratch")


def test_wrong_version_or_identity_in_the_archive_is_refused(tmp_path):
    module = packager()
    app, backend = signed_bundle(tmp_path / "bundle", version="0.2.1")
    with pytest.raises(SystemExit, match="version"):
        module.verify_archive(archive_of(app, tmp_path / "a.tar.gz"), "0.3.0", backend, tmp_path / "s1")
    other, other_backend = signed_bundle(tmp_path / "other", identifier="com.example.other")
    with pytest.raises(SystemExit, match="identifier"):
        module.verify_archive(
            archive_of(other, tmp_path / "b.tar.gz"), "0.3.0", other_backend, tmp_path / "s2"
        )


def test_archive_bytes_that_break_the_signature_or_differ_from_the_sealed_sidecar_are_refused(tmp_path):
    module = packager()
    app, backend = signed_bundle(tmp_path / "bundle")
    sealed = tmp_path / "sealed-backend"
    shutil.copyfile(backend["opendialer-backend"], sealed)
    (app / "Contents/Resources").mkdir()
    (app / "Contents/Resources/added-after-signing.txt").write_text("tamper")
    with pytest.raises(subprocess.CalledProcessError):
        module.verify_archive(
            archive_of(app, tmp_path / "a.tar.gz"),
            "0.3.0",
            {**backend, "opendialer-backend": sealed},
            tmp_path / "s1",
        )
    clean, clean_sealed = signed_bundle(tmp_path / "clean")
    different = tmp_path / "different-sidecar"
    different.write_bytes(b"not the sealed sidecar")
    for name in ("opendialer-backend", "opendialer-trainer"):
        with pytest.raises(SystemExit, match=name):
            module.verify_archive(
                archive_of(clean, tmp_path / f"{name}.tar.gz"),
                "0.3.0",
                {**clean_sealed, name: different},
                tmp_path / f"s-{name}",
            )
    assert all(path.exists() for path in clean_sealed.values())


def test_archive_without_the_trainer_is_refused(tmp_path):
    module = packager()
    app, sealed = signed_bundle(tmp_path / "bundle", sidecars=("opendialer-backend",))
    with pytest.raises(SystemExit, match="opendialer-trainer"):
        module.verify_archive(
            archive_of(app, tmp_path / "a.tar.gz"),
            "0.3.0",
            {**sealed, "opendialer-trainer": sealed["opendialer-backend"]},
            tmp_path / "s",
        )


def test_packager_seals_every_sidecar_with_the_scoped_entitlement():
    module = packager()
    assert module.SIDECARS == ("opendialer-backend", "opendialer-trainer")
    source = (ROOT / "scripts/package_macos.py").read_text()
    assert "BackendEntitlements.plist" in source and "--self-check" in source


def test_archive_entries_outside_the_bundle_are_refused(tmp_path):
    app, backend = signed_bundle(tmp_path / "bundle")
    stray = tmp_path / "stray.txt"
    stray.write_text("outside")
    with pytest.raises(SystemExit, match="outside"):
        packager().verify_archive(
            archive_of(app, tmp_path / "a.tar.gz", stray), "0.3.0", backend, tmp_path / "s"
        )
