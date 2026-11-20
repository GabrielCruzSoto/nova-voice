"""Manifest-driven voice models downloader and importer (OP-3, AC-13)."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger("nova.models_downloader")

_CHUNK_SIZE = 64 * 1024  # 64 KiB chunks
_TIMEOUT_SECONDS = 60.0

ProgressCallback = Callable[[dict[str, Any]], None]


def default_user_models_dir() -> Path:
    xdg_data = os.environ.get("XDG_DATA_HOME")
    if xdg_data:
        return Path(xdg_data).expanduser() / "nova" / "models"
    return Path.home() / ".local" / "share" / "nova" / "models"


def _package_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _default_manifest_path() -> Path:
    return _package_root() / "config" / "models_manifest.json"


def load_manifest(manifest_path: Path | None = None) -> dict[str, Any]:
    path = manifest_path or _default_manifest_path()
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("Invalid models manifest format: expected JSON object")
    return data


def _sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK_SIZE):
            h.update(chunk)
    return h.hexdigest()


def _is_url_allowed(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "https":
        return True
    # Allow http for local test suites (localhost / 127.0.0.1)
    if parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost"):
        return True
    return False


def _download_file(
    url: str,
    dest_file: Path,
    expected_sha256: str,
    *,
    expected_size: int = 0,
    on_chunk: Callable[[int, int], None] | None = None,
) -> None:
    if not _is_url_allowed(url):
        raise ValueError(f"Insecure URL protocol '{url}'. Only HTTPS is permitted.")

    part_file = dest_file.with_name(f"{dest_file.name}.part")
    if part_file.exists():
        part_file.unlink(missing_ok=True)

    hasher = hashlib.sha256()
    req = urllib.request.Request(url, headers={"User-Agent": "nova-voice-downloader/1.0"})

    downloaded = 0
    try:
        with (
            urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as response,
            open(part_file, "wb") as out_f,
        ):
            content_len_hdr = response.headers.get("Content-Length")
            total_bytes = (
                int(content_len_hdr)
                if content_len_hdr and content_len_hdr.isdigit()
                else expected_size
            )
            while chunk := response.read(_CHUNK_SIZE):
                hasher.update(chunk)
                out_f.write(chunk)
                downloaded += len(chunk)
                if on_chunk is not None:
                    on_chunk(downloaded, total_bytes)
    except Exception as exc:
        if part_file.exists():
            part_file.unlink(missing_ok=True)
        raise RuntimeError(f"Download failed for {url}: {exc}") from exc

    calculated_hash = hasher.hexdigest()
    if calculated_hash.lower() != expected_sha256.lower():
        if part_file.exists():
            part_file.unlink(missing_ok=True)
        raise ValueError(
            f"SHA256 mismatch for {dest_file.name}: "
            f"expected {expected_sha256}, got {calculated_hash}"
        )

    # Atomic move
    os.replace(part_file, dest_file)


def _atomic_copy_verified(source_file: Path, target_file: Path, expected_sha256: str) -> None:
    calc_hash = _sha256_of_file(source_file)
    if calc_hash.lower() != expected_sha256.lower():
        raise ValueError(
            f"SHA256 mismatch for {target_file.name}: "
            f"expected {expected_sha256}, got {calc_hash}"
        )
    part_file = target_file.with_name(f"{target_file.name}.part")
    try:
        shutil.copyfile(source_file, part_file)
        os.replace(part_file, target_file)
    except Exception:
        if part_file.exists():
            part_file.unlink(missing_ok=True)
        raise


def download_models(
    dest: Path | str,
    *,
    import_dir: Path | str | None = None,
    import_wakeword: Path | str | None = None,
    manifest_data: dict[str, Any] | None = None,
    manifest_path: Path | None = None,
    progress_callback: ProgressCallback | None = None,
) -> int:
    """Download, import, and verify voice models atomically into `dest`."""
    dest_path = Path(dest).expanduser().resolve()
    dest_path.mkdir(parents=True, exist_ok=True)

    manifest = manifest_data if manifest_data is not None else load_manifest(manifest_path)
    models: list[dict[str, Any]] = manifest.get("models", [])
    total_models = len(models)

    def _emit(
        *,
        index: int,
        filename: str,
        status: str,
        bytes_downloaded: int = 0,
        total_bytes: int = 0,
        error: str | None = None,
    ) -> None:
        if progress_callback is not None:
            progress_callback(
                {
                    "index": index,
                    "total_models": total_models,
                    "filename": filename,
                    "status": status,
                    "bytes_downloaded": bytes_downloaded,
                    "total_bytes": total_bytes,
                    "error": error,
                }
            )

    for idx, entry in enumerate(models, start=1):
        filename: str = entry["filename"]
        source_type: str = entry["source_type"]
        expected_sha256: str = entry["sha256"]
        size_bytes: int = int(entry.get("size_bytes", 0))
        target_file = dest_path / filename

        # Idempotency check: if file exists and hash matches, skip
        if target_file.is_file():
            if _sha256_of_file(target_file).lower() == expected_sha256.lower():
                sys.stdout.write(f"Model '{filename}' already present and verified. Skipping.\n")
                _emit(
                    index=idx,
                    filename=filename,
                    status="skipped",
                    bytes_downloaded=size_bytes,
                    total_bytes=size_bytes,
                )
                continue

        # Local cache import shortcut for any source_type when --import dir has a verified copy
        if import_dir and source_type in ("https", "bundled"):
            cached = Path(import_dir).expanduser().resolve() / filename
            if cached.is_file() and _sha256_of_file(cached).lower() == expected_sha256.lower():
                _emit(
                    index=idx,
                    filename=filename,
                    status="importing",
                    bytes_downloaded=0,
                    total_bytes=size_bytes,
                )
                _atomic_copy_verified(cached, target_file, expected_sha256)
                sys.stdout.write(f"Imported verified cached '{filename}' from {cached}.\n")
                _emit(
                    index=idx,
                    filename=filename,
                    status="completed",
                    bytes_downloaded=size_bytes,
                    total_bytes=size_bytes,
                )
                continue

        if source_type == "https":
            url: str = entry["url"]
            sys.stdout.write(f"Downloading '{filename}' from {url}...\n")
            _emit(
                index=idx,
                filename=filename,
                status="downloading",
                bytes_downloaded=0,
                total_bytes=size_bytes,
            )
            def _on_chunk(
                dl: int,
                tot: int,
                _idx: int = idx,
                _fn: str = filename,
            ) -> None:
                _emit(
                    index=_idx,
                    filename=_fn,
                    status="downloading",
                    bytes_downloaded=dl,
                    total_bytes=tot,
                )

            try:
                _download_file(
                    url,
                    target_file,
                    expected_sha256,
                    expected_size=size_bytes,
                    on_chunk=_on_chunk,
                )
                sys.stdout.write(f"Successfully downloaded and verified '{filename}'.\n")
                _emit(
                    index=idx,
                    filename=filename,
                    status="completed",
                    bytes_downloaded=size_bytes,
                    total_bytes=size_bytes,
                )
            except Exception as exc:
                sys.stderr.write(f"Error downloading '{filename}': {exc}\n")
                _emit(
                    index=idx,
                    filename=filename,
                    status="error",
                    total_bytes=size_bytes,
                    error=str(exc),
                )
                return 1

        elif source_type == "bundled":
            source_file: Path | None = None
            if filename == "oye_nova.onnx" and import_wakeword:
                source_file = Path(import_wakeword).expanduser().resolve()
            else:
                asset_relpath = str(entry.get("asset_relpath", f"assets/models/{filename}"))
                candidate = _package_root() / asset_relpath
                if candidate.is_file():
                    source_file = candidate

            if source_file is None or not source_file.is_file():
                msg = f"Bundled model asset '{filename}' not found in package."
                sys.stderr.write(f"Error: {msg}\n")
                _emit(
                    index=idx,
                    filename=filename,
                    status="error",
                    total_bytes=size_bytes,
                    error=msg,
                )
                return 1

            _emit(
                index=idx,
                filename=filename,
                status="importing",
                bytes_downloaded=0,
                total_bytes=size_bytes,
            )
            try:
                _atomic_copy_verified(source_file, target_file, expected_sha256)
                sys.stdout.write(f"Successfully installed bundled model '{filename}'.\n")
                _emit(
                    index=idx,
                    filename=filename,
                    status="completed",
                    bytes_downloaded=size_bytes,
                    total_bytes=size_bytes,
                )
            except Exception as exc:
                sys.stderr.write(f"Error installing bundled '{filename}': {exc}\n")
                _emit(
                    index=idx,
                    filename=filename,
                    status="error",
                    total_bytes=size_bytes,
                    error=str(exc),
                )
                return 1

        elif source_type == "local-import":
            # Check if source path provided
            local_source: Path | None = None
            if filename == "oye_nova.onnx" and import_wakeword:
                local_source = Path(import_wakeword).expanduser().resolve()
            elif import_dir:
                candidate = Path(import_dir).expanduser().resolve() / filename
                if candidate.is_file():
                    local_source = candidate

            if local_source and local_source.is_file():
                sys.stdout.write(f"Importing '{filename}' from {local_source}...\n")
                _emit(
                    index=idx,
                    filename=filename,
                    status="importing",
                    bytes_downloaded=0,
                    total_bytes=size_bytes,
                )
                try:
                    _atomic_copy_verified(local_source, target_file, expected_sha256)
                    sys.stdout.write(f"Successfully imported and verified '{filename}'.\n")
                    _emit(
                        index=idx,
                        filename=filename,
                        status="completed",
                        bytes_downloaded=size_bytes,
                        total_bytes=size_bytes,
                    )
                except Exception as exc:
                    sys.stderr.write(f"Error importing '{filename}': {exc}\n")
                    _emit(
                        index=idx,
                        filename=filename,
                        status="error",
                        total_bytes=size_bytes,
                        error=str(exc),
                    )
                    return 1
            else:
                sys.stdout.write(
                    f"Notice: '{filename}' is a local-import model. "
                    "Provide --import <dir> or --import-wakeword <file> to install it.\n"
                )
                _emit(
                    index=idx,
                    filename=filename,
                    status="skipped",
                    total_bytes=size_bytes,
                )

    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nova-models-downloader",
        description="Download and verify Nova voice models.",
    )
    default_dest = Path.home() / ".local" / "share" / "nova" / "models"
    parser.add_argument(
        "--dest",
        default=str(default_dest),
        help="Destination directory for voice models.",
    )
    parser.add_argument(
        "--import",
        dest="import_dir",
        default=None,
        help="Directory containing local models to import.",
    )
    parser.add_argument(
        "--import-wakeword",
        default=None,
        help="Local wake-word model file path to import.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    return download_models(
        dest=args.dest,
        import_dir=args.import_dir,
        import_wakeword=args.import_wakeword,
    )


if __name__ == "__main__":
    raise SystemExit(main())
