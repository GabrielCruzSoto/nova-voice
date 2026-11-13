"""Tests for S4: Debian packaging, metadata, files, lifecycle, and reproducibility."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BUILD_SH = _REPO_ROOT / "packaging" / "deb" / "build.sh"


@pytest.fixture(scope="module")
def built_deb_packages(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    output_dir = tmp_path_factory.mktemp("deb_dist")
    epoch = "1700000000"

    # Build PR package
    r_pr = subprocess.run(
        [
            "bash",
            str(_BUILD_SH),
            "--mode",
            "pr",
            "--output-dir",
            str(output_dir),
            "--source-date-epoch",
            epoch,
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert r_pr.returncode == 0, f"PR build failed: {r_pr.stdout}\n{r_pr.stderr}"

    # Build Release package
    r_rel = subprocess.run(
        [
            "bash",
            str(_BUILD_SH),
            "--mode",
            "release",
            "--output-dir",
            str(output_dir),
            "--source-date-epoch",
            epoch,
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert r_rel.returncode == 0, f"Release build failed: {r_rel.stdout}\n{r_rel.stderr}"

    pr_debs = list(output_dir.glob("nova-voice_*+git*_amd64.deb"))
    rel_debs = [p for p in output_dir.glob("nova-voice_*_amd64.deb") if "+git" not in p.name]

    assert len(pr_debs) == 1, f"Expected 1 PR deb, found {pr_debs}"
    assert len(rel_debs) == 1, f"Expected 1 Release deb, found {rel_debs}"

    return {"pr": pr_debs[0], "release": rel_debs[0], "output_dir": output_dir}


def test_deb_control_fields_and_depends(built_deb_packages: dict[str, Path]) -> None:
    pr_deb = built_deb_packages["pr"]
    rel_deb = built_deb_packages["release"]

    # Check PR deb control
    r_pr = subprocess.run(
        ["dpkg-deb", "-f", str(pr_deb)], capture_output=True, text=True, check=True
    )
    ctrl_pr = r_pr.stdout
    assert "Package: nova-voice" in ctrl_pr
    assert "Architecture: amd64" in ctrl_pr
    assert re.search(r"Version: \d+\.\d+\.\d+\+git[0-9a-f]{7}", ctrl_pr)

    # Check Release deb control
    r_rel = subprocess.run(
        ["dpkg-deb", "-f", str(rel_deb)], capture_output=True, text=True, check=True
    )
    ctrl_rel = r_rel.stdout
    assert "Package: nova-voice" in ctrl_rel
    assert "Architecture: amd64" in ctrl_rel
    assert re.search(r"Version: \d+\.\d+\.\d+$", ctrl_rel, re.MULTILINE)

    # Validate exact dependencies
    required_deps = [
        "python3 (>= 3.12)",
        "python3 (<< 3.13)",
        "python3-venv",
        "python3-gi",
        "gir1.2-gtk-3.0",
        "pipewire-bin | pulseaudio-utils",
        "xdg-utils",
    ]
    for dep in required_deps:
        assert dep in ctrl_pr, f"Missing required dependency: {dep}"

    # Forbidden audio libraries
    forbidden = ["libportaudio2", "alsa-utils", "libasound2"]
    for forb in forbidden:
        assert forb not in ctrl_pr, f"Forbidden dependency present: {forb}"


def test_deb_contents_listing_no_models_no_pycache(built_deb_packages: dict[str, Path]) -> None:
    pr_deb = built_deb_packages["pr"]
    r = subprocess.run(["dpkg-deb", "-c", str(pr_deb)], capture_output=True, text=True, check=True)
    listing = r.stdout

    # No standalone downloaded models or models directory
    assert "opt/nova/models" not in listing
    assert "oye_nova.onnx" not in listing
    assert "es_ES-davefx-medium" not in listing
    assert not re.search(r"/opt/nova/[^/]*\.onnx", listing)

    # Internal library assets (faster-whisper VAD) must remain intact
    assert "faster_whisper/assets/silero_vad_v6.onnx" in listing

    # No __pycache__ or *.pyc
    assert "__pycache__" not in listing
    assert ".pyc" not in listing

    # Required files present
    assert "/usr/bin/nova-desktop" in listing
    assert "/usr/bin/nova" in listing
    assert "/usr/share/applications/nova-desktop.desktop" in listing
    assert "/usr/share/doc/nova-voice/changelog.Debian.gz" in listing
    assert "/usr/share/doc/nova-voice/copyright" in listing

    # debian/changelog must not be tracked in git
    r_git = subprocess.run(
        ["git", "ls-files", "debian/changelog"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert r_git.stdout.strip() == ""


def test_deb_extracted_desktop_entry_and_venv(
    built_deb_packages: dict[str, Path], tmp_path: Path
) -> None:
    pr_deb = built_deb_packages["pr"]
    extract_dir = tmp_path / "extracted"
    extract_dir.mkdir()

    subprocess.run(["dpkg-deb", "-x", str(pr_deb), str(extract_dir)], check=True)

    # Validate desktop file
    desktop_file = extract_dir / "usr" / "share" / "applications" / "nova-desktop.desktop"
    assert desktop_file.is_file()
    if shutil.which("desktop-file-validate"):
        r_val = subprocess.run(
            ["desktop-file-validate", str(desktop_file)], capture_output=True, text=True
        )
        assert r_val.returncode == 0, f"desktop-file-validate failed: {r_val.stderr}"

    # Validate pyvenv.cfg
    pyvenv_cfg = extract_dir / "opt" / "nova" / "venv" / "pyvenv.cfg"
    assert pyvenv_cfg.is_file()
    cfg_content = pyvenv_cfg.read_text()
    assert "include-system-site-packages = true" in cfg_content

    # Validate built UI asset app.js is present
    app_js_matches = list(extract_dir.glob("opt/nova/venv/**/ui/static/app.js"))
    assert len(app_js_matches) >= 1, "app.js must be present in packaged venv site-packages"

    # Check for build path leaks in text files under /opt/nova
    build_dir_str = str(_REPO_ROOT)
    r_grep = subprocess.run(
        ["grep", "-rI", build_dir_str, str(extract_dir / "opt" / "nova")],
        capture_output=True,
        text=True,
    )
    assert r_grep.returncode != 0, f"Host build directory leaked into package: {r_grep.stdout}"


def test_deb_size_budget_nfr1(built_deb_packages: dict[str, Path]) -> None:
    pr_deb = built_deb_packages["pr"]
    max_bytes = 300 * 1024 * 1024  # 300 MiB
    actual_size = pr_deb.stat().st_size
    assert actual_size <= max_bytes, f"Deb size {actual_size} exceeds budget of {max_bytes} bytes"


def test_deb_maintainer_scripts_lifecycle(
    built_deb_packages: dict[str, Path], tmp_path: Path
) -> None:
    pr_deb = built_deb_packages["pr"]
    control_dir = tmp_path / "control_extracted"
    control_dir.mkdir()

    subprocess.run(["dpkg-deb", "-e", str(pr_deb), str(control_dir)], check=True)

    postinst = control_dir / "postinst"
    prerm = control_dir / "prerm"
    postrm = control_dir / "postrm"

    assert postinst.is_file() and os.access(postinst, os.X_OK)
    assert prerm.is_file() and os.access(prerm, os.X_OK)
    assert postrm.is_file() and os.access(postrm, os.X_OK)

    # AC-11 verification: postrm purge must not touch ~/.local/share/nova/models
    fake_home = tmp_path / "user_home"
    models_dir = fake_home / ".local" / "share" / "nova" / "models"
    models_dir.mkdir(parents=True)
    sample_model = models_dir / "sample.onnx"
    sample_model.write_bytes(b"user model content")

    env = {**os.environ, "HOME": str(fake_home)}
    r_purge = subprocess.run(
        ["bash", str(postrm), "purge"], env=env, capture_output=True, text=True
    )
    assert r_purge.returncode == 0
    assert sample_model.is_file()
    assert sample_model.read_bytes() == b"user model content"


def test_deb_two_build_reproducibility_nfr3(tmp_path: Path) -> None:
    out1 = tmp_path / "out1"
    out2 = tmp_path / "out2"
    epoch = "1700000000"

    subprocess.run(
        [
            "bash",
            str(_BUILD_SH),
            "--mode",
            "release",
            "--output-dir",
            str(out1),
            "--source-date-epoch",
            epoch,
        ],
        cwd=_REPO_ROOT,
        check=True,
    )
    subprocess.run(
        [
            "bash",
            str(_BUILD_SH),
            "--mode",
            "release",
            "--output-dir",
            str(out2),
            "--source-date-epoch",
            epoch,
        ],
        cwd=_REPO_ROOT,
        check=True,
    )

    deb1 = list(out1.glob("*.deb"))[0]
    deb2 = list(out2.glob("*.deb"))[0]

    list1 = subprocess.run(
        ["dpkg-deb", "-c", str(deb1)], capture_output=True, text=True, check=True
    ).stdout
    list2 = subprocess.run(
        ["dpkg-deb", "-c", str(deb2)], capture_output=True, text=True, check=True
    ).stdout
    assert list1 == list2, "dpkg-deb -c listing must be byte-for-byte identical"

    ctrl1 = subprocess.run(
        ["dpkg-deb", "-f", str(deb1)], capture_output=True, text=True, check=True
    ).stdout
    ctrl2 = subprocess.run(
        ["dpkg-deb", "-f", str(deb2)], capture_output=True, text=True, check=True
    ).stdout
    assert ctrl1 == ctrl2, "DEBIAN/control must be identical across reproducible builds"
