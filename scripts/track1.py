#!/usr/bin/env python3
"""Minimal, auditable Track 1 workflow.

The public code never embeds phenotype terms, sample identifiers, target genes,
or patient variants. Protected and large outputs are created only below the
configured external-volume roots.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import datetime as dt
import gzip
import hashlib
import itertools
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from typing import Any, Iterable, Iterator, Mapping, Sequence


class PipelineError(RuntimeError):
    """A deliberate fail-closed pipeline error."""


HPO_RE = re.compile(r"HP:\d{7}")
CONTIG_RE = re.compile(r"^##contig=<ID=([^,>]+)(?:,length=([0-9]+))?")
FORMAT_RE = re.compile(r"^##FORMAT=<ID=([^,>]+)")
PASS_FILTERS = {"PASS", ".", ""}
IMPACT_ORDER = {"HIGH": 4, "MODERATE": 3, "LOW": 2, "MODIFIER": 1, "": 0, ".": 0}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def run(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    capture: bool = False,
    stdout: Any = None,
    stderr: Any = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        [str(item) for item in command],
        cwd=str(cwd) if cwd else None,
        env=merged_env,
        text=True,
        capture_output=capture,
        stdout=stdout,
        stderr=stderr,
        check=check,
    )


def command_version(command: Sequence[str]) -> str:
    try:
        result = run(command, capture=True)
    except (OSError, subprocess.CalledProcessError):
        return "unavailable"
    output = (result.stdout or result.stderr or "").strip().splitlines()
    return output[0] if output else "unknown"


def require_executable(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise PipelineError(f"required executable is unavailable: {name}")
    return path


def is_below(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def resolved_without_escape(path: Path, allowed_root: Path) -> Path:
    resolved_root = allowed_root.resolve(strict=False)
    resolved = path.resolve(strict=False)
    if resolved != resolved_root and not is_below(resolved, resolved_root):
        raise PipelineError(f"path escapes external project root: {path} -> {resolved}")
    return resolved


def nearest_existing(path: Path) -> Path:
    candidate = path
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            raise PipelineError(f"no existing ancestor for path: {path}")
        candidate = parent
    return candidate


def validate_storage(
    config: Mapping[str, Any],
    *,
    phase: str,
    create: bool = False,
    require_docker: bool = False,
    minimum_free_gib: int | None = None,
) -> dict[str, Any]:
    storage = config["storage"]
    volume = Path(storage["volume_root"])
    project_root = Path(storage["project_root"])
    if not volume.is_dir():
        raise PipelineError(f"required external volume is not mounted: {volume}")
    volume_device = volume.stat().st_dev
    paths = {
        key: Path(storage[key])
        for key in (
            "project_root",
            "reference_root",
            "private_run_root",
            "reference_tmp",
            "run_tmp",
            "docker_disk_root",
        )
    }
    if project_root.is_symlink():
        raise PipelineError(f"external project root may not be a symlink: {project_root}")
    for name, path in paths.items():
        resolved = resolved_without_escape(path, project_root)
        existing = nearest_existing(path)
        if existing.stat().st_dev != volume_device:
            raise PipelineError(f"{name} is not on the required external filesystem: {resolved}")
    free_bytes = shutil.disk_usage(volume).free
    required_gib = minimum_free_gib if minimum_free_gib is not None else storage[
        "minimum_setup_free_gib" if phase == "setup" else "minimum_run_free_gib"
    ]
    if free_bytes < int(required_gib) * 1024**3:
        raise PipelineError(
            f"external free space is {free_bytes / 1024**3:.1f} GiB; {required_gib} GiB required"
        )
    if create:
        for path in paths.values():
            path.mkdir(parents=True, exist_ok=True)
            if path.stat().st_dev != volume_device:
                raise PipelineError(f"created path is not on the external filesystem: {path}")
    if require_docker:
        validate_docker_storage(Path(storage["docker_disk_root"]))
    return {
        "volume": str(volume.resolve()),
        "volume_device": volume_device,
        "free_gib": round(free_bytes / 1024**3, 2),
        "required_gib": required_gib,
        "paths": {name: str(path.resolve(strict=False)) for name, path in paths.items()},
    }


def _find_string_values(value: Any, key_path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], str]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _find_string_values(child, key_path + (str(key),))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _find_string_values(child, key_path + (str(index),))
    elif isinstance(value, str):
        yield key_path, value


def validate_docker_storage(expected_root: Path) -> dict[str, Any]:
    require_executable("docker")
    expected = expected_root.resolve(strict=False)
    settings_paths = [
        Path.home() / "Library/Group Containers/group.com.docker/settings-store.json",
        Path.home() / "Library/Group Containers/group.com.docker/settings.json",
    ]
    configured_locations: list[str] = []
    for settings_path in settings_paths:
        if not settings_path.is_file():
            continue
        try:
            settings = load_json(settings_path)
        except (OSError, json.JSONDecodeError):
            continue
        for key_path, value in _find_string_values(settings):
            key = ".".join(key_path).lower()
            if "diskimage" in key or "datafolder" in key:
                configured_locations.append(value)
    settings_ok = any(
        Path(value).expanduser().resolve(strict=False) == expected
        or is_below(Path(value).expanduser().resolve(strict=False), expected)
        for value in configured_locations
        if value
    )
    actual_images: list[str] = []
    if expected_root.exists():
        for pattern in ("Docker.raw", "Docker.qcow2", "*.raw", "*.qcow2"):
            actual_images.extend(str(path.resolve()) for path in expected_root.rglob(pattern))
    actual_ok = bool(actual_images) and all(is_below(Path(item), expected) for item in actual_images)
    if not settings_ok and not actual_ok:
        raise PipelineError(
            "Docker Desktop disk-image location is not verified below "
            f"{expected_root}. Configure Settings > Resources > Advanced > Disk image location first."
        )
    info = run(["docker", "info", "--format", "{{json .ServerVersion}}"], capture=True, check=False)
    if info.returncode != 0:
        raise PipelineError("Docker Desktop is installed but its engine is not running")
    return {
        "expected_root": str(expected),
        "configured_locations": configured_locations,
        "actual_images": actual_images,
        "server_version": info.stdout.strip(),
    }


def config_path(repo: Path) -> Path:
    return repo / "config/track1.json"


def load_config(repo: Path) -> dict[str, Any]:
    config = load_json(config_path(repo))
    if config.get("schema_version") != 1:
        raise PipelineError("unsupported config schema version")
    unbiased = config.get("unbiased", {})
    if unbiased.get("target_gene_prior") is not None or unbiased.get("gene_panel") is not None:
        raise PipelineError("unbiased configuration contains a target-gene prior")
    if unbiased.get("added_hpo_terms"):
        raise PipelineError("unbiased configuration contains added phenotype terms")
    return config


def load_panel(path: Path, *, expected_assembly: str = "GRCh38") -> dict[str, Any]:
    """Load the YAML-compatible JSON disease knowledge layer without PyYAML."""
    try:
        panel = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PipelineError(f"disease panel is not valid YAML-compatible JSON: {path}") from error
    if panel.get("schema_version") != 1 or panel.get("assembly") != expected_assembly:
        raise PipelineError("disease panel schema or assembly is unsupported")
    genes = panel.get("genes")
    if not isinstance(genes, list) or not genes:
        raise PipelineError("disease panel must contain at least one gene")
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for item in genes:
        if not isinstance(item, dict):
            raise PipelineError("disease panel gene entries must be objects")
        symbol = str(item.get("symbol", "")).strip().upper()
        aliases = [str(value).strip().upper() for value in item.get("aliases", []) if str(value).strip()]
        inheritance = [str(value).strip().upper() for value in item.get("inheritance", []) if str(value).strip()]
        if not symbol or symbol in seen or not inheritance or not item.get("citations"):
            raise PipelineError(f"invalid or duplicate disease panel entry: {item}")
        if any(value not in {"AR", "AD", "X_LINKED", "MITOCHONDRIAL"} for value in inheritance):
            raise PipelineError(f"unsupported inheritance mode for {symbol}")
        seen.add(symbol)
        normalized.append(
            {
                **item,
                "symbol": symbol,
                "aliases": aliases,
                "inheritance": inheritance,
                "tier": str(item.get("tier", "mva_like")),
            }
        )
    panel["genes"] = normalized
    panel["gene_lookup"] = {
        alias: item
        for item in normalized
        for alias in [item["symbol"], *item["aliases"]]
    }
    return panel


def external_env(tmp_path: Path) -> dict[str, str]:
    # Return a complete environment because several streaming subprocesses are
    # launched directly with ``env=`` (rather than through run(), which merges
    # environments itself).
    return {**os.environ, "TMPDIR": str(tmp_path), "TMP": str(tmp_path), "TEMP": str(tmp_path)}


def download_asset(asset: Mapping[str, Any], downloads: Path, tmp_path: Path) -> dict[str, Any]:
    require_executable("curl")
    destination = downloads / str(asset["name"])
    partial = downloads / (str(asset["name"]) + ".part")
    expected_bytes = asset.get("expected_bytes")
    expected_sha = asset.get("sha256")
    if destination.exists():
        if expected_bytes is not None and destination.stat().st_size != int(expected_bytes):
            raise PipelineError(f"existing download has unexpected size: {destination}")
        digest = sha256_file(destination)
        if expected_sha and digest != expected_sha:
            raise PipelineError(f"existing download checksum failed: {destination}")
        return asset_record(asset, destination, digest, reused=True)
    if expected_bytes is not None and int(expected_bytes) >= 1024**3:
        parts_dir = parallel_download(
            url=str(asset["url"]),
            destination=partial,
            expected_bytes=int(expected_bytes),
            tmp_path=tmp_path,
        )
    else:
        parts_dir = None
        command = [
            "curl",
            "--fail",
            "--location",
            "--retry",
            "5",
            "--retry-all-errors",
            "--continue-at",
            "-",
            "--output",
            str(partial),
            str(asset["url"]),
        ]
        run(command, env=external_env(tmp_path))
    if expected_bytes is not None and partial.stat().st_size != int(expected_bytes):
        raise PipelineError(
            f"download size mismatch for {asset['name']}: {partial.stat().st_size} != {expected_bytes}"
        )
    digest = sha256_file(partial)
    if expected_sha and digest != expected_sha:
        raise PipelineError(f"download checksum failed for {asset['name']}")
    partial.replace(destination)
    if parts_dir is not None and parts_dir.is_dir():
        shutil.rmtree(parts_dir)
    return asset_record(asset, destination, digest, reused=False)


def parallel_download(
    *,
    url: str,
    destination: Path,
    expected_bytes: int,
    tmp_path: Path,
    segment_size: int = 256 * 1024 * 1024,
    workers: int = 16,
) -> Path:
    """Download a known-size range-capable asset into bounded external parts."""
    if expected_bytes <= 0 or segment_size <= 0:
        raise PipelineError("parallel download requires positive sizes")
    parts_dir = destination.parent / f".{destination.name}.parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    ranges = []
    for index, start in enumerate(range(0, expected_bytes, segment_size)):
        end = min(start + segment_size, expected_bytes) - 1
        ranges.append((index, start, end, parts_dir / f"part-{index:05d}"))
    if destination.exists():
        first_expected = ranges[0][2] - ranges[0][1] + 1
        if destination.stat().st_size <= first_expected and not ranges[0][3].exists():
            destination.replace(ranges[0][3])
        elif destination.stat().st_size != expected_bytes:
            raise PipelineError(
                f"cannot seed range download from unexpected partial size: {destination.stat().st_size}"
            )
        elif destination.stat().st_size == expected_bytes:
            return parts_dir

    def fetch(item: tuple[int, int, int, Path]) -> tuple[int, Path]:
        index, start, end, path = item
        target_size = end - start + 1
        existing = path.stat().st_size if path.exists() else 0
        if existing > target_size:
            raise PipelineError(f"range part is oversized: {path}")
        if existing == target_size:
            return index, path
        append_path = path.with_name(path.name + ".append")
        if append_path.exists():
            append_path.unlink()
        request_start = start + existing
        command = [
            "curl",
            "--fail",
            "--location",
            "--silent",
            "--show-error",
            "--retry",
            "5",
            "--retry-all-errors",
            "--range",
            f"{request_start}-{end}",
            "--output",
            str(append_path),
            url,
        ]
        run(command, env=external_env(tmp_path))
        expected_append = target_size - existing
        if not append_path.is_file() or append_path.stat().st_size != expected_append:
            actual = append_path.stat().st_size if append_path.exists() else 0
            raise PipelineError(
                f"range response size mismatch at part {index}: {actual} != {expected_append}"
            )
        with path.open("ab") as output, append_path.open("rb") as source:
            shutil.copyfileobj(source, output, length=8 * 1024 * 1024)
        append_path.unlink()
        if path.stat().st_size != target_size:
            raise PipelineError(f"assembled range part has unexpected size: {path}")
        return index, path

    with ThreadPoolExecutor(max_workers=min(workers, len(ranges))) as executor:
        futures = [executor.submit(fetch, item) for item in ranges]
        for future in as_completed(futures):
            future.result()
    assembling = destination.with_name(destination.name + ".assembling")
    with assembling.open("wb") as output:
        for _, _, _, part in ranges:
            with part.open("rb") as source:
                shutil.copyfileobj(source, output, length=8 * 1024 * 1024)
    if assembling.stat().st_size != expected_bytes:
        raise PipelineError(
            f"parallel assembly size mismatch: {assembling.stat().st_size} != {expected_bytes}"
        )
    assembling.replace(destination)
    return parts_dir


def asset_record(asset: Mapping[str, Any], path: Path, digest: str, *, reused: bool) -> dict[str, Any]:
    return {
        "name": asset["name"],
        "url": asset["url"],
        "expected_bytes": asset.get("expected_bytes"),
        "bytes": path.stat().st_size,
        "upstream_sha256": asset.get("sha256"),
        "sha256": digest,
        "path": str(path.resolve()),
        "reused": reused,
    }


def copy_reference_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if source.stat().st_size != destination.stat().st_size or sha256_file(source) != sha256_file(destination):
            raise PipelineError(f"reference destination differs from retained download: {destination}")
        return
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def extract_archive(archive: Path, destination: Path, required: Sequence[str], tmp_path: Path) -> dict[str, Any]:
    marker_name = re.sub(r"[^A-Za-z0-9_.-]", "_", archive.name)
    marker = destination / f".track1-extraction.{marker_name}.json"
    if marker.exists() and all((destination / item).exists() for item in required):
        return load_json(marker)
    destination.mkdir(parents=True, exist_ok=True)
    if archive.name.endswith(".zip"):
        require_executable("unzip")
        command = ["unzip", "-q", "-o", str(archive), "-d", str(destination)]
    elif archive.name.endswith(".tar.gz"):
        require_executable("tar")
        command = ["tar", "-xzf", str(archive), "-C", str(destination)]
    else:
        raise PipelineError(f"unsupported archive type: {archive}")
    run(command, env=external_env(tmp_path))
    missing = [item for item in required if not (destination / item).exists()]
    if missing:
        raise PipelineError(f"archive extraction missing required paths: {', '.join(missing)}")
    record = {
        "archive": str(archive.resolve()),
        "archive_sha256": sha256_file(archive),
        "destination": str(destination.resolve()),
        "required_paths": list(required),
        "completed_at": utc_now(),
    }
    write_json(marker, record)
    return record


def docker_pull_and_inspect(image: str) -> dict[str, Any]:
    run(["docker", "pull", image])
    result = run(
        ["docker", "image", "inspect", image, "--format", "{{json .RepoDigests}}|{{.Architecture}}|{{.Os}}"],
        capture=True,
    )
    digest_text, architecture, os_name = result.stdout.strip().split("|", 2)
    return {
        "image": image,
        "repo_digests": json.loads(digest_text),
        "architecture": architecture,
        "os": os_name,
    }


def write_download_checkpoint(reference_root: Path, record: Mapping[str, Any]) -> Path:
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", str(record["name"]))
    checkpoint = reference_root / f"download-checkpoint-{safe_name}.json"
    write_json(
        checkpoint,
        {
            "schema_version": 1,
            "completed_at": utc_now(),
            "stopped_after": record["name"],
            "asset": dict(record),
            "size_verified": record.get("expected_bytes") is None
            or record["bytes"] == record["expected_bytes"],
            "checksum_verified": True,
            "downstream_started": False,
        },
    )
    return checkpoint


def setup_references(
    repo: Path,
    config: Mapping[str, Any],
    *,
    dry_run: bool,
    stop_after: str | None = None,
) -> None:
    # A verified mid-download checkpoint does not use Docker. Full setup still
    # requires a live engine and externally located Docker disk before pulling.
    require_live_docker = stop_after is None
    storage = validate_storage(
        config,
        phase="setup",
        create=False,
        require_docker=require_live_docker,
    )
    references = config["references"]
    expected = []
    for section_name in ("genome", "exomiser", "vep"):
        section = references[section_name]
        for asset in section.get("files", section.get("archives", [])):
            expected.append({"section": section_name, **asset})
    asset_names = {item["name"] for item in expected}
    if stop_after is not None and stop_after not in asset_names:
        raise PipelineError(f"unknown setup checkpoint asset: {stop_after}")
    if dry_run:
        print(json.dumps({
            "status": "dry-run-ok",
            "storage": storage,
            "downloads": expected,
            "containers": list(config["containers"].values()),
            "stop_after": stop_after,
        }, indent=2))
        return
    validate_storage(
        config,
        phase="setup",
        create=True,
        require_docker=require_live_docker,
    )
    reference_root = Path(config["storage"]["reference_root"])
    downloads = reference_root / "downloads"
    tmp_path = Path(config["storage"]["reference_tmp"])
    downloads.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    for item in expected:
        record = download_asset(item, downloads, tmp_path)
        record["section"] = item["section"]
        records.append(record)
        if item["name"] == stop_after:
            checkpoint = write_download_checkpoint(reference_root, record)
            print(f"verified download checkpoint complete: {checkpoint}")
            return
    genome_destination = reference_root / references["genome"]["directory"]
    for asset in references["genome"]["files"]:
        copy_reference_file(downloads / asset["name"], genome_destination / asset["name"])
    extractions = []
    for section_name in ("exomiser", "vep"):
        section = references[section_name]
        destination = reference_root / section["directory"]
        for asset in section["archives"]:
            extractions.append(
                extract_archive(
                    downloads / asset["name"],
                    destination,
                    [asset["required_directory"]],
                    tmp_path,
                )
            )
        missing = [item for item in section["required_directories"] if not (destination / item).exists()]
        if missing:
            raise PipelineError(f"{section_name} extraction is incomplete: {', '.join(missing)}")
    docker_storage = validate_docker_storage(Path(config["storage"]["docker_disk_root"]))
    images = [docker_pull_and_inspect(image) for image in config["containers"].values()]
    manifest = {
        "schema_version": 1,
        "created_at": utc_now(),
        "storage": storage,
        "assets": records,
        "extractions": extractions,
        "docker_storage": docker_storage,
        "container_images": images,
        "releases": {name: value["release"] for name, value in references.items()},
    }
    write_json(reference_root / "reference-manifest.json", manifest)
    print(f"reference setup complete: {reference_root / 'reference-manifest.json'}")


def setup_light(repo: Path, config: Mapping[str, Any], *, dry_run: bool) -> None:
    lightweight = config.get("lightweight", {})
    panel_path = repo / str(lightweight.get("panel", ""))
    load_panel(panel_path, expected_assembly=str(config["inputs"]["assembly"]))
    storage = validate_storage(
        config,
        phase="setup",
        create=False,
        require_docker=False,
        minimum_free_gib=int(lightweight.get("minimum_setup_free_gib", 2)),
    )
    annotation = config["references"]["lightweight_annotation"]
    expected = [{"section": "lightweight_annotation", **item} for item in annotation["files"]]
    if dry_run:
        print(json.dumps({"status": "dry-run-ok", "storage": storage, "downloads": expected}, indent=2))
        return
    validate_storage(
        config,
        phase="setup",
        create=True,
        require_docker=False,
        minimum_free_gib=int(lightweight.get("minimum_setup_free_gib", 2)),
    )
    reference_root = Path(config["storage"]["reference_root"])
    downloads = reference_root / "downloads"
    tmp_path = Path(config["storage"]["reference_tmp"])
    downloads.mkdir(parents=True, exist_ok=True)
    records = []
    for item in expected:
        record = download_asset(item, downloads, tmp_path)
        record["section"] = item["section"]
        records.append(record)
    gff_record = next(record for record in records if record["name"].endswith(".gff3.gz"))
    checksum_record = next(record for record in records if record["name"] == "CHECKSUMS")
    checksum_lines = Path(checksum_record["path"]).read_text(encoding="utf-8").splitlines()
    checksum_line = next(
        (line for line in checksum_lines if line.endswith(gff_record["name"])), None
    )
    if not checksum_line:
        raise PipelineError("Ensembl CHECKSUMS does not contain the downloaded GFF3")
    fields = checksum_line.split()
    if len(fields) != 3:
        raise PipelineError("unexpected Ensembl CHECKSUMS format")
    upstream_sum, upstream_blocks, _ = fields
    # Ensembl's two-field checksum is the portable BSD ``sum`` output
    # (checksum and 512-byte block count); macOS does not accept GNU's -r flag.
    local_sum = run(["sum", gff_record["path"]], capture=True).stdout.split()
    if (
        len(local_sum) < 2
        or int(local_sum[0]) != int(upstream_sum)
        or int(local_sum[1]) != int(upstream_blocks)
    ):
        raise PipelineError("Ensembl GFF3 upstream checksum manifest verification failed")
    annotation_destination = reference_root / annotation["directory"]
    for record in records:
        copy_reference_file(Path(record["path"]), annotation_destination / record["name"])
    manifest = {
        "schema_version": 1,
        "created_at": utc_now(),
        "release": annotation["release"],
        "assets": records,
        "installed_directory": str(annotation_destination.resolve()),
        "upstream_checksum": {
            "manifest": checksum_record["path"],
            "gff3_line": checksum_line,
            "algorithm": "Ensembl BSD sum",
            "verified": True,
        },
    }
    path = reference_root / "light-reference-manifest.json"
    write_json(path, manifest)
    print(f"light reference setup complete: {path}")


def verify_light_manifest(config: Mapping[str, Any]) -> tuple[Path, dict[str, Any]]:
    """Verify the small Ensembl annotation setup before reading patient records."""
    reference_root = Path(config["storage"]["reference_root"])
    manifest_path = reference_root / "light-reference-manifest.json"
    if not manifest_path.is_file():
        raise PipelineError("light reference manifest is missing; run setup-light first")
    manifest = load_json(manifest_path)
    expected_release = config["references"]["lightweight_annotation"]["release"]
    if manifest.get("release") != expected_release:
        raise PipelineError("light reference release does not match configuration")
    expected_assets = {
        str(item["name"]): item
        for item in config["references"]["lightweight_annotation"]["files"]
    }
    observed = {str(item.get("name")): item for item in manifest.get("assets", [])}
    if set(observed) != set(expected_assets):
        raise PipelineError("light reference manifest assets do not match configuration")
    for name, expected in expected_assets.items():
        record = observed[name]
        path = Path(record.get("path", ""))
        if not path.is_file() or path.stat().st_size != int(expected["expected_bytes"]):
            raise PipelineError(f"light reference asset is missing or changed in size: {path}")
        digest = sha256_file(path)
        if digest != record.get("sha256"):
            raise PipelineError(f"light reference asset checksum changed: {path}")
    if not manifest.get("upstream_checksum", {}).get("verified"):
        raise PipelineError("light reference upstream checksum was not verified")
    return manifest_path, manifest


def extract_hpo_from_text(text: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for identifier in HPO_RE.findall(text):
        if identifier not in seen:
            seen.add(identifier)
            result.append(identifier)
    return result


def extract_hpo_docx(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml").decode("utf-8", errors="replace")
    return extract_hpo_from_text(xml)


def write_tsv(path: Path, rows: Iterable[Mapping[str, Any]], fields: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    open_fn = gzip.open if path.name.endswith(".gz") else open
    with open_fn(path, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def read_tsv(path: Path) -> list[dict[str, str]]:
    open_fn = gzip.open if path.name.endswith(".gz") else open
    with open_fn(path, "rt", encoding="utf-8", newline="") as handle:
        lines = (line for line in handle if line.strip() and not line.startswith("##"))
        return list(csv.DictReader(lines, delimiter="\t"))


def parse_vcf_header(header: str) -> dict[str, Any]:
    contigs: dict[str, int | None] = {}
    formats: set[str] = set()
    samples: list[str] = []
    for line in header.splitlines():
        if match := CONTIG_RE.match(line):
            contigs[match.group(1)] = int(match.group(2)) if match.group(2) else None
        elif match := FORMAT_RE.match(line):
            formats.add(match.group(1))
        elif line.startswith("#CHROM"):
            columns = line.split("\t")
            samples = columns[9:]
    return {"contigs": contigs, "formats": formats, "samples": samples}


def read_fai(path: Path) -> dict[str, int]:
    contigs: dict[str, int] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            fields = line.rstrip("\n").split("\t")
            if len(fields) >= 2:
                contigs[fields[0]] = int(fields[1])
    return contigs


def create_contig_maps(
    vcf_contigs: Mapping[str, int | None], fasta_contigs: Mapping[str, int]
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    forward: list[tuple[str, str]] = []
    reverse: list[tuple[str, str]] = []
    used_targets: set[str] = set()
    for source, length in vcf_contigs.items():
        candidates = [source]
        if not source.startswith("chr"):
            candidates.append("chrM" if source in {"M", "MT"} else f"chr{source}")
        target = next((item for item in candidates if item in fasta_contigs), None)
        if target is None:
            raise PipelineError(f"VCF contig is absent from reference FASTA: {source}")
        if length is not None and fasta_contigs[target] != length:
            raise PipelineError(
                f"contig length mismatch for {source}: VCF={length}, FASTA={fasta_contigs[target]}"
            )
        if target in used_targets:
            raise PipelineError(f"contig map is not one-to-one at reference contig: {target}")
        used_targets.add(target)
        forward.append((source, target))
        reverse.append((target, source))
    return forward, reverse


def write_contig_map(path: Path, pairs: Sequence[tuple[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for source, target in pairs:
            handle.write(f"{source}\t{target}\n")


def gff_attributes(value: str) -> dict[str, str]:
    attributes: dict[str, str] = {}
    for item in value.rstrip(";").split(";"):
        if "=" not in item:
            continue
        key, raw = item.split("=", 1)
        attributes[key] = raw.replace("%3B", ";").replace("%2C", ",")
    return attributes


def gff_id(value: str) -> str:
    return value.strip().removeprefix("gene:").removeprefix("transcript:")


def resolve_panel_gene(
    value: Any,
    panel_gene_ids: Mapping[str, str],
    panel_gene_lookup: Mapping[str, Mapping[str, Any]] | None = None,
) -> str | None:
    """Resolve an Ensembl ID, canonical symbol, or panel alias to one symbol."""
    normalized = gff_id(normalize_missing(value)).upper()
    if not normalized:
        return None
    for symbol, stable_id in panel_gene_ids.items():
        if gff_id(str(stable_id)).upper() == normalized:
            return str(symbol).upper()
    if panel_gene_lookup:
        item = panel_gene_lookup.get(normalized)
        if item is not None:
            return str(item["symbol"]).upper()
    return None


def canonical_panel_gene(value: Any, panel: Mapping[str, Any]) -> str | None:
    """Resolve a classifier gene field through the panel symbol/alias lookup."""
    normalized = gff_id(normalize_missing(value)).upper()
    if not normalized:
        return None
    item = panel.get("gene_lookup", {}).get(normalized)
    if item is None:
        return None
    return str(item["symbol"]).upper()


def gff_contig_to_chr(value: str) -> str:
    if value.startswith("chr"):
        return value
    return "chrM" if value in {"M", "MT"} else f"chr{value}"


def panel_gff_and_intervals(
    *,
    gff_path: Path,
    panel: Mapping[str, Any],
    vcf_contigs: Mapping[str, int | None],
    destination_gff: Path,
    destination_bed: Path,
    padding_bp: int,
) -> dict[str, Any]:
    """Select panel transcripts from Ensembl GFF3 and emit matching chr-style files."""
    selected: dict[str, dict[str, Any]] = {}
    selected_gene_ids: dict[str, str] = {}
    selected_ids: set[str] = set()
    # Keep one interval per canonical panel gene.  The previous implementation
    # keyed by contig and spanned every same-contig gene between the minimum and
    # maximum coordinates, which inflated targeted extraction dramatically.
    intervals: dict[str, tuple[str, int, int]] = {}
    with gzip.open(gff_path, "rt", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if line.startswith("#") or not line.strip():
                continue
            fields = line.rstrip("\n").split("\t")
            if len(fields) != 9 or fields[2] != "gene":
                continue
            attributes = gff_attributes(fields[8])
            names = {
                str(attributes.get(key, "")).upper()
                for key in ("Name", "gene_name", "gene")
                if attributes.get(key)
            }
            match = next((panel["gene_lookup"].get(name) for name in names if name in panel["gene_lookup"]), None)
            if match is None:
                continue
            symbol = str(match["symbol"])
            gene_id = attributes.get("ID") or attributes.get("gene_id")
            if not gene_id:
                continue
            selected[symbol] = match
            selected_ids.update({gff_id(gene_id), gene_id})
            selected_gene_ids[symbol] = gff_id(gene_id)
            start, end = int(fields[3]), int(fields[4])
            source_contig = fields[0]
            if source_contig not in vcf_contigs:
                source_contig = next(
                    (candidate for candidate in vcf_contigs if gff_contig_to_chr(candidate) == gff_contig_to_chr(fields[0])),
                    source_contig,
                )
            if source_contig in vcf_contigs:
                padded_start = max(1, start - padding_bp)
                padded_end = end + padding_bp
                current = intervals.get(symbol)
                if current is not None and current[0] != source_contig:
                    raise PipelineError(
                        f"panel gene {symbol} resolves to multiple VCF contigs: "
                        f"{current[0]} and {source_contig}"
                    )
                if current is None:
                    intervals[symbol] = (source_contig, padded_start, padded_end)
                else:
                    intervals[symbol] = (
                        source_contig,
                        min(padded_start, current[1]),
                        max(padded_end, current[2]),
                    )
    if len(selected) != len(panel["genes"]):
        missing = sorted(set(panel["gene_lookup"][item["symbol"]]["symbol"] for item in panel["genes"]) - set(selected))
        raise PipelineError(f"panel genes missing from Ensembl GFF3: {', '.join(missing)}")
    missing_intervals = sorted(set(selected) - set(intervals))
    if missing_intervals:
        raise PipelineError(
            "panel genes do not overlap any VCF contig: " + ", ".join(missing_intervals)
        )

    destination_gff.parent.mkdir(parents=True, exist_ok=True)
    relevant_ids = set(selected_ids)
    with gzip.open(gff_path, "rt", encoding="utf-8", errors="replace") as source, gzip.open(
        destination_gff, "wt", encoding="utf-8", newline=""
    ) as output:
        output.write("##gff-version 3\n")
        for line in source:
            if line.startswith("#") or not line.strip():
                continue
            fields = line.rstrip("\n").split("\t")
            if len(fields) != 9:
                continue
            attributes = gff_attributes(fields[8])
            identifiers = {gff_id(attributes[key]) for key in ("ID", "gene_id") if attributes.get(key)}
            parents = {
                gff_id(parent)
                for parent in attributes.get("Parent", "").split(",")
                if parent
            }
            direct = bool(identifiers & selected_ids) or bool(parents & selected_ids)
            inherited = bool(parents & relevant_ids)
            if not (direct or inherited):
                continue
            relevant_ids.update(identifiers)
            fields[0] = gff_contig_to_chr(fields[0])
            output.write("\t".join(fields) + "\n")
    with destination_bed.open("w", encoding="utf-8") as output:
        for symbol in sorted(intervals):
            contig, start, end = intervals[symbol]
            output.write(f"{contig}\t{start - 1}\t{end}\n")
    return {
        "genes": sorted(selected),
        "gene_ids": selected_gene_ids,
        "interval_count": len(intervals),
        "intervals_by_gene": {
            symbol: {"contig": contig, "start": start, "end": end}
            for symbol, (contig, start, end) in sorted(intervals.items())
        },
        "padding_bp": padding_bp,
        "gff_sha256": sha256_file(destination_gff),
        "gff": str(destination_gff),
        "intervals": str(destination_bed),
    }


def append_command_log(path: Path, stage: str, command: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"time": utc_now(), "stage": stage, "argv": list(command)}) + "\n")


def run_logged(
    command: Sequence[str],
    *,
    stage: str,
    command_log: Path,
    output_log: Path | None = None,
    env: Mapping[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    append_command_log(command_log, stage, command)
    if output_log is None:
        return run(command, env=env, check=check)
    output_log.parent.mkdir(parents=True, exist_ok=True)
    with output_log.open("a", encoding="utf-8") as handle:
        return run(command, env=env, stdout=handle, stderr=subprocess.STDOUT, check=check)


def first_record(command: Sequence[str]) -> str | None:
    process = subprocess.Popen([str(item) for item in command], stdout=subprocess.PIPE, text=True)
    assert process.stdout is not None
    line = process.stdout.readline()
    process.stdout.close()
    if line:
        process.terminate()
    process.wait()
    return line.rstrip("\n") if line else None


def normalize_vcf(
    *,
    original: Path,
    fasta: Path,
    fai: Path,
    run_dir: Path,
    tmp_path: Path,
    expected_samples: int,
) -> dict[str, Any]:
    require_executable("bcftools")
    command_log = run_dir / "commands.log"
    input_dir = run_dir / "input"
    working = run_dir / "working"
    input_dir.mkdir(parents=True, exist_ok=True)
    working.mkdir(parents=True, exist_ok=True)
    original_hash = sha256_file(original)
    (input_dir / "original.sha256").write_text(f"{original_hash}  {original.name}\n", encoding="utf-8")
    header_result = run(["bcftools", "view", "-h", str(original)], capture=True)
    source_header = parse_vcf_header(header_result.stdout)
    if len(source_header["samples"]) != expected_samples:
        raise PipelineError(
            f"unexpected sample count: {len(source_header['samples'])}; expected {expected_samples}"
        )
    forward, reverse = create_contig_maps(source_header["contigs"], read_fai(fai))
    to_reference = input_dir / "contig-map-to-reference.tsv"
    from_reference = input_dir / "contig-map-from-reference.tsv"
    write_contig_map(to_reference, forward)
    write_contig_map(from_reference, reverse)
    canonical = working / "canonical.grch38.biallelic.vcf.gz"
    commands = [
        ["bcftools", "annotate", "--rename-chrs", str(to_reference), "-Ou", str(original)],
        [
            "bcftools",
            "norm",
            "--check-ref",
            "e",
            "--fasta-ref",
            str(fasta),
            "-m",
            "-any",
        ],
        [
            "bcftools",
            "annotate",
            "--rename-chrs",
            str(from_reference),
            "-Oz",
            "-o",
            str(canonical),
        ],
    ]
    if "AD" in source_header["formats"]:
        commands[1].extend(["--keep-sum", "AD"])
    commands[1].append("-Ou")
    for command in commands:
        append_command_log(command_log, "normalize", command)
    env = os.environ.copy()
    env.update(external_env(tmp_path))
    first = subprocess.Popen(commands[0], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    assert first.stdout is not None
    second = subprocess.Popen(
        commands[1], stdin=first.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
    )
    first.stdout.close()
    assert second.stdout is not None
    third = subprocess.Popen(commands[2], stdin=second.stdout, stderr=subprocess.PIPE, env=env)
    second.stdout.close()
    third_stderr = third.communicate()[1]
    second_stderr = second.communicate()[1]
    first_stderr = first.communicate()[1]
    failures = [
        ("rename-to-reference", first.returncode, first_stderr),
        ("normalize", second.returncode, second_stderr),
        ("rename-from-reference", third.returncode, third_stderr),
    ]
    failed = [(name, code, message) for name, code, message in failures if code]
    if failed:
        details = "; ".join(
            f"{name}={code}: {(message or b'').decode(errors='replace')[-500:]}"
            for name, code, message in failed
        )
        raise PipelineError(f"normalization failed: {details}")
    run_logged(
        ["bcftools", "index", "--csi", "--force", str(canonical)],
        stage="normalize-index",
        command_log=command_log,
        env=external_env(tmp_path),
    )
    canonical_header_text = run(["bcftools", "view", "-h", str(canonical)], capture=True).stdout
    canonical_header = parse_vcf_header(canonical_header_text)
    if len(canonical_header["samples"]) != expected_samples:
        raise PipelineError("sample count changed during normalization")
    if set(source_header["formats"]) != set(canonical_header["formats"]):
        raise PipelineError("declared FORMAT fields changed during normalization")
    if any(contig.startswith("chr") for contig in canonical_header["contigs"]):
        raise PipelineError("canonical VCF did not restore unprefixed contig style")
    multiallelic = first_record(["bcftools", "view", "-m3", "-H", str(canonical)])
    if multiallelic:
        raise PipelineError("canonical VCF still contains a multiallelic record")
    if sha256_file(original) != original_hash:
        raise PipelineError("source VCF changed during normalization")
    original_records = int(run(["bcftools", "index", "-n", str(original)], capture=True).stdout.strip())
    canonical_records = int(run(["bcftools", "index", "-n", str(canonical)], capture=True).stdout.strip())
    summary = {
        "assembly": "GRCh38",
        "source_sha256": original_hash,
        "canonical_sha256": sha256_file(canonical),
        "source_records": original_records,
        "canonical_biallelic_records": canonical_records,
        "source_contigs": len(source_header["contigs"]),
        "sample_count": len(canonical_header["samples"]),
        "sample_identifiers_emitted": False,
        "contig_style": "unprefixed",
        "format_fields_preserved": sorted(source_header["formats"]),
        "ad_sum_preservation_requested": "AD" in source_header["formats"],
        "index": str(canonical) + ".csi",
        "validated_at": utc_now(),
    }
    write_json(working / "normalization.json", summary)
    return {**summary, "sample_id": canonical_header["samples"][0], "canonical": canonical}


def normalize_missing(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text in {".", "-"} else text


def parse_number(value: Any) -> float | None:
    text = normalize_missing(value)
    if not text:
        return None
    numbers: list[float] = []
    for token in re.split(r"[,&|]", text):
        token = token.strip()
        if not token or token == ".":
            continue
        try:
            numbers.append(float(token))
        except ValueError:
            continue
    return max(numbers) if numbers else None


def genotype_state(gt: str) -> str:
    alleles = re.split(r"[/|]", normalize_missing(gt))
    called = [item for item in alleles if item and item != "."]
    if not called or all(item == "0" for item in called):
        return "reference_or_missing"
    if len(called) >= 2 and all(item != "0" for item in called):
        return "hom_alt"
    return "het"


def clinvar_flags(value: str) -> tuple[bool, bool]:
    normalized = normalize_missing(value).lower().replace(" ", "_")
    conflict = "conflict" in normalized
    pathogenic = "pathogenic" in normalized or "likely_pathogenic" in normalized
    return pathogenic, conflict


def biological_relevance(impact: str, consequence: str, clinvar_plp: bool) -> bool:
    consequence_lower = normalize_missing(consequence).lower()
    return (
        normalize_missing(impact).upper() in {"HIGH", "MODERATE"}
        or "splice_region_variant" in consequence_lower
        or "splice_acceptor_variant" in consequence_lower
        or "splice_donor_variant" in consequence_lower
        or clinvar_plp
    )


def phase_relationship(first: Mapping[str, Any], second: Mapping[str, Any]) -> str:
    gt1 = normalize_missing(first.get("GT"))
    gt2 = normalize_missing(second.get("GT"))
    ps1 = normalize_missing(first.get("PS"))
    ps2 = normalize_missing(second.get("PS"))
    if "|" not in gt1 or "|" not in gt2 or not ps1 or ps1 != ps2:
        return "phase_unknown"
    a1 = gt1.split("|")
    a2 = gt2.split("|")
    if len(a1) != 2 or len(a2) != 2:
        return "phase_unknown"
    sides1 = {index for index, allele in enumerate(a1) if allele not in {"0", "."}}
    sides2 = {index for index, allele in enumerate(a2) if allele not in {"0", "."}}
    if not sides1 or not sides2:
        return "phase_unknown"
    return "in_cis" if sides1 & sides2 else "in_trans_supported"


def row_priority(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        1 if row.get("clinvar_plp") else 0,
        IMPACT_ORDER.get(normalize_missing(row.get("IMPACT")).upper(), 0),
        1 if "splice" in normalize_missing(row.get("Consequence")).lower() else 0,
        -(row.get("max_af") if row.get("max_af") is not None else 1.0),
    )


ALLELE_FIELDS = [
    "variant_key",
    "CHROM",
    "POS",
    "REF",
    "ALT",
    "gene",
    "gene_id",
    "GT",
    "zygosity",
    "PS",
    "FILTER",
    "DP",
    "GQ",
    "Consequence",
    "IMPACT",
    "MANE_SELECT",
    "CANONICAL",
    "max_af",
    "af_missing",
    "CLIN_SIG",
    "clinvar_plp",
    "clinvar_conflict",
    "quality_status",
    "quality_reasons",
    "biological_relevance",
    "primary_eligible",
    "dominant_eligible",
    "rescue_eligible",
    "exclusion_reasons",
]

ARCHITECTURE_FIELDS = [
    "gene",
    "architecture",
    "variant_1",
    "variant_2",
    "phase_status",
    "rescue_status",
    "clinvar_plp_count",
    "worst_impact",
    "max_population_af",
    "rank_key",
]


def classify_vep_rows(
    rows: Sequence[Mapping[str, str]], thresholds: Mapping[str, Any]
) -> dict[str, list[dict[str, Any]]]:
    collapsed: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in rows:
        chrom = normalize_missing(raw.get("CHROM"))
        pos = normalize_missing(raw.get("POS"))
        ref = normalize_missing(raw.get("REF"))
        alt = normalize_missing(raw.get("ALT"))
        gene = normalize_missing(raw.get("SYMBOL")) or normalize_missing(raw.get("Gene"))
        if not all((chrom, pos, ref, alt, gene)):
            continue
        clinvar_plp, clinvar_conflict = clinvar_flags(raw.get("CLIN_SIG", ""))
        af_values = [
            parse_number(raw.get("MAX_AF")),
            parse_number(raw.get("gnomADe_AF")),
            parse_number(raw.get("gnomADg_AF")),
        ]
        observed_af = [value for value in af_values if value is not None]
        record: dict[str, Any] = {
            "variant_key": f"{chrom}-{pos}-{ref}-{alt}",
            "CHROM": chrom,
            "POS": pos,
            "REF": ref,
            "ALT": alt,
            "gene": gene,
            "gene_id": normalize_missing(raw.get("Gene")),
            "GT": normalize_missing(raw.get("GT")),
            "PS": normalize_missing(raw.get("PS")),
            "FILTER": normalize_missing(raw.get("FILTER")) or ".",
            "DP": normalize_missing(raw.get("DP")),
            "GQ": normalize_missing(raw.get("GQ")),
            "Consequence": normalize_missing(raw.get("Consequence")),
            "IMPACT": normalize_missing(raw.get("IMPACT")).upper(),
            "MANE_SELECT": normalize_missing(raw.get("MANE_SELECT")),
            "CANONICAL": normalize_missing(raw.get("CANONICAL")),
            "max_af": max(observed_af) if observed_af else None,
            "af_missing": not observed_af,
            "CLIN_SIG": normalize_missing(raw.get("CLIN_SIG")),
            "clinvar_plp": clinvar_plp,
            "clinvar_conflict": clinvar_conflict,
        }
        key = (record["variant_key"], gene)
        previous = collapsed.get(key)
        if previous is None or row_priority(record) > row_priority(previous):
            collapsed[key] = record
    candidates: list[dict[str, Any]] = []
    filter_rows: list[dict[str, Any]] = []
    for record in collapsed.values():
        record["zygosity"] = genotype_state(record["GT"])
        non_reference = record["zygosity"] != "reference_or_missing"
        quality_reasons: list[str] = []
        if record["FILTER"] not in PASS_FILTERS:
            quality_reasons.append(f"FILTER={record['FILTER']}")
        dp = parse_number(record["DP"])
        gq = parse_number(record["GQ"])
        if dp is not None and dp < float(thresholds["minimum_dp"]):
            quality_reasons.append(f"DP<{thresholds['minimum_dp']}")
        if gq is not None and gq < float(thresholds["minimum_gq"]):
            quality_reasons.append(f"GQ<{thresholds['minimum_gq']}")
        record["quality_status"] = (
            "fail"
            if quality_reasons
            else "not_evaluable"
            if dp is None or gq is None
            else "pass"
        )
        record["quality_reasons"] = ";".join(quality_reasons)
        relevant = biological_relevance(record["IMPACT"], record["Consequence"], record["clinvar_plp"])
        record["biological_relevance"] = relevant
        af = record["max_af"]
        comphet_frequency = af is None or af <= float(thresholds["compound_het_max_af"])
        strict_frequency = af is None or af <= float(thresholds["single_or_hom_max_af"])
        if record["clinvar_plp"]:
            comphet_frequency = True
            strict_frequency = True
        primary_eligible = (
            non_reference
            and not quality_reasons
            and relevant
            and (
                (record["zygosity"] == "het" and comphet_frequency)
                or (record["zygosity"] == "hom_alt" and strict_frequency)
            )
        )
        record["primary_eligible"] = primary_eligible
        record["dominant_eligible"] = (
            primary_eligible and record["zygosity"] == "het" and strict_frequency
        )
        record["rescue_eligible"] = (
            non_reference
            and bool(quality_reasons)
            and relevant
            and (
                record["clinvar_plp"]
                or (record["zygosity"] == "het" and comphet_frequency)
                or (record["zygosity"] == "hom_alt" and strict_frequency)
            )
        )
        exclusions: list[str] = []
        if not non_reference:
            exclusions.append("reference_or_missing_genotype")
        if quality_reasons:
            exclusions.append("quality")
        if not relevant:
            exclusions.append("consequence")
        if not record["clinvar_plp"] and not (
            (record["zygosity"] == "het" and comphet_frequency)
            or (record["zygosity"] == "hom_alt" and strict_frequency)
        ):
            exclusions.append("frequency")
        record["exclusion_reasons"] = ";".join(exclusions)
        if non_reference:
            candidates.append(record)
            filter_rows.append(record)
    primary = [row for row in candidates if row["primary_eligible"]]
    rescue = [row for row in candidates if row["rescue_eligible"]]
    primary.sort(key=lambda row: (row["gene"], row["CHROM"], int(row["POS"]), row["REF"], row["ALT"]))
    rescue.sort(key=lambda row: (row["gene"], row["CHROM"], int(row["POS"]), row["REF"], row["ALT"]))
    return {"all_nonref": candidates, "primary": primary, "rescue": rescue, "filter_rows": filter_rows}


CSQ_HIGH = {
    "transcript_ablation",
    "splice_acceptor_variant",
    "splice_donor_variant",
    "stop_gained",
    "frameshift_variant",
    "start_lost",
    "stop_lost",
}
CSQ_MODERATE = {"missense_variant", "inframe_insertion", "inframe_deletion"}
CSQ_LOW = {"synonymous_variant", "stop_retained_variant"}
CSQ_ALIASES = {
    "missense": "missense_variant",
    "synonymous": "synonymous_variant",
    "frameshift": "frameshift_variant",
    "inframe": "inframe_insertion",
    "splice": "splice_region_variant",
    # BCFtools/csq uses compact Sequence Ontology names, while the shared
    # classifier historically consumed VEP-style *_variant names.
    "splice_acceptor": "splice_acceptor_variant",
    "splice_donor": "splice_donor_variant",
    "splice_region": "splice_region_variant",
    "start_lost": "start_lost",
    "stop_lost": "stop_lost",
    "stop_gained": "stop_gained",
}


def normalize_csq_consequence(value: str) -> str:
    terms = []
    for item in value.split("&"):
        term = item.strip().lower()
        terms.append(CSQ_ALIASES.get(term, term))
    return "&".join(term for term in terms if term)


def consequence_impact(consequence: str) -> str:
    consequences = {item.strip().lower() for item in consequence.split("&") if item.strip()}
    if consequences & CSQ_HIGH:
        return "HIGH"
    if consequences & CSQ_MODERATE:
        return "MODERATE"
    if consequences & CSQ_LOW:
        return "LOW"
    return "MODIFIER"


def parse_csquery(
    vcf: Path,
    *,
    panel_gene_ids: Mapping[str, str],
    panel_gene_lookup: Mapping[str, Mapping[str, Any]] | None = None,
    command_log: Path,
    tmp_path: Path,
) -> list[dict[str, str]]:
    """Read only local BCFtools/csq output and convert it to the common classifier shape."""
    header = run(["bcftools", "view", "-h", str(vcf)], capture=True).stdout
    parsed = parse_vcf_header(header)
    sample_fields = [field for field in ("GT", "DP", "GQ", "PS", "PID", "PGT") if field in parsed["formats"]]
    query = "%CHROM\t%POS\t%REF\t%ALT\t%FILTER"
    if sample_fields:
        query += "[" + "".join(f"\t%{field}" for field in sample_fields) + "]"
    query += "\t%INFO/BCSQ\n"
    command = ["bcftools", "query", "-f", query, str(vcf)]
    append_command_log(command_log, "csq-query", command)
    result = run(command, capture=True, env=external_env(tmp_path))
    output: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        base = fields[:5]
        if len(base) != 5:
            continue
        cursor = 5
        values = {field: "" for field in sample_fields}
        for field in sample_fields:
            if cursor >= len(fields):
                break
            values[field] = fields[cursor]
            cursor += 1
        bcsq = fields[cursor] if cursor < len(fields) else "."
        if not bcsq or bcsq == ".":
            continue
        for annotation in bcsq.split(","):
            annotation = annotation.lstrip("*@")
            parts = annotation.split("|")
            consequence = normalize_csq_consequence(normalize_missing(parts[0] if parts else ""))
            gene_id = normalize_missing(parts[1] if len(parts) > 1 else "")
            if not consequence or not gene_id:
                continue
            gene = resolve_panel_gene(gene_id, panel_gene_ids, panel_gene_lookup)
            if gene is None:
                raise PipelineError(
                    "bcftools csq annotation gene cannot be resolved to the disease panel: "
                    f"{gene_id}"
                )
            row = {
                "CHROM": base[0],
                "POS": base[1],
                "REF": base[2],
                "ALT": base[3],
                "FILTER": base[4],
                "SYMBOL": gene,
                "Gene": gene_id,
                "Consequence": consequence,
                "IMPACT": consequence_impact(consequence),
                "MANE_SELECT": "",
                "CANONICAL": "",
                "MAX_AF": "",
                "gnomADe_AF": "",
                "gnomADg_AF": "",
                "CLIN_SIG": "",
                **values,
            }
            output.append(row)
    return output


def architecture_rank_key(rows: Sequence[Mapping[str, Any]], architecture: str) -> str:
    clinvar_count = sum(bool(row.get("clinvar_plp")) for row in rows)
    impact = max(IMPACT_ORDER.get(str(row.get("IMPACT", "")).upper(), 0) for row in rows)
    af_values = [row.get("max_af") for row in rows if row.get("max_af") is not None]
    max_af = max(af_values) if af_values else 1.0
    completeness = 3 if architecture in {"compound_heterozygous", "homozygous_alt"} else 2
    return f"{completeness}:{clinvar_count}:{impact}:{1.0 - max_af:.12f}"


def make_architecture(
    gene: str,
    architecture: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    phase_status: str = "not_applicable",
    rescue_status: str = "primary",
) -> dict[str, Any]:
    af_values = [row.get("max_af") for row in rows if row.get("max_af") is not None]
    worst_impact = max(
        rows,
        key=lambda row: IMPACT_ORDER.get(str(row.get("IMPACT", "")).upper(), 0),
    ).get("IMPACT", "")
    return {
        "gene": gene,
        "architecture": architecture,
        "variant_1": rows[0]["variant_key"],
        "variant_2": rows[1]["variant_key"] if len(rows) > 1 else "",
        "phase_status": phase_status,
        "rescue_status": rescue_status,
        "clinvar_plp_count": sum(bool(row.get("clinvar_plp")) for row in rows),
        "worst_impact": worst_impact,
        "max_population_af": max(af_values) if af_values else "missing",
        "rank_key": architecture_rank_key(rows, architecture),
    }


def build_primary_architectures(primary: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_gene: dict[str, list[Mapping[str, Any]]] = {}
    for row in primary:
        by_gene.setdefault(str(row["gene"]), []).append(row)
    architectures: list[dict[str, Any]] = []
    for gene, rows in by_gene.items():
        for row in rows:
            if row["zygosity"] == "hom_alt":
                architectures.append(make_architecture(gene, "homozygous_alt", [row]))
            elif row.get("dominant_eligible"):
                architectures.append(make_architecture(gene, "single_heterozygous", [row]))
        heterozygotes = [row for row in rows if row["zygosity"] == "het"]
        for first, second in itertools.combinations(heterozygotes, 2):
            if first["CHROM"] == second["CHROM"] and first["POS"] == second["POS"]:
                continue
            phase = phase_relationship(first, second)
            architectures.append(
                make_architecture(
                    gene,
                    "compound_heterozygous",
                    [first, second],
                    phase_status=phase,
                )
            )
    architectures.sort(key=lambda row: (row["gene"], row["architecture"], row["variant_1"], row["variant_2"]))
    return architectures


def build_rescue_architectures(
    primary: Sequence[Mapping[str, Any]], rescue: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    primary_by_gene: dict[str, list[Mapping[str, Any]]] = {}
    rescue_by_gene: dict[str, list[Mapping[str, Any]]] = {}
    for row in primary:
        primary_by_gene.setdefault(str(row["gene"]), []).append(row)
    for row in rescue:
        rescue_by_gene.setdefault(str(row["gene"]), []).append(row)
    architectures: list[dict[str, Any]] = []
    for gene, rescue_rows in rescue_by_gene.items():
        for row in rescue_rows:
            if row["zygosity"] == "hom_alt":
                architectures.append(
                    make_architecture(gene, "homozygous_alt", [row], rescue_status="rescue_only")
                )
        primary_hets = [row for row in primary_by_gene.get(gene, []) if row["zygosity"] == "het"]
        rescue_hets = [row for row in rescue_rows if row["zygosity"] == "het"]
        for first in primary_hets:
            for second in rescue_hets:
                if first["CHROM"] == second["CHROM"] and first["POS"] == second["POS"]:
                    continue
                architectures.append(
                    make_architecture(
                        gene,
                        "compound_heterozygous",
                        [first, second],
                        phase_status=phase_relationship(first, second),
                        rescue_status="primary_plus_rescue",
                    )
                )
        for first, second in itertools.combinations(rescue_hets, 2):
            if first["CHROM"] == second["CHROM"] and first["POS"] == second["POS"]:
                continue
            architectures.append(
                make_architecture(
                    gene,
                    "compound_heterozygous",
                    [first, second],
                    phase_status=phase_relationship(first, second),
                    rescue_status="rescue_only",
                )
            )
    architectures.sort(key=lambda row: (row["gene"], row["architecture"], row["variant_1"], row["variant_2"]))
    return architectures


DISEASE_ARCHITECTURE_FIELDS = [
    "gene",
    "disease_tier",
    "inheritance",
    "architecture",
    "variant_1",
    "variant_2",
    "consequence_1",
    "consequence_2",
    "impact_1",
    "impact_2",
    "quality_1",
    "quality_2",
    "phase_status",
    "rescue_status",
    "rank_key",
]


DISEASE_TIER_ORDER = {"canonical_mva": 0, "mva5_mva6": 1, "mva_like": 2}
CONSEQUENCE_ORDER = {
    "transcript_ablation": 7,
    "splice_acceptor_variant": 7,
    "splice_donor_variant": 7,
    "stop_gained": 7,
    "frameshift_variant": 7,
    "start_lost": 6,
    "stop_lost": 5,
    "missense_variant": 4,
    "inframe_insertion": 3,
    "inframe_deletion": 3,
    "splice_region_variant": 2,
    "synonymous_variant": 1,
}


def consequence_score(row: Mapping[str, Any]) -> int:
    return max(
        (
            CONSEQUENCE_ORDER.get(
                CSQ_ALIASES.get(item.strip().lower(), item.strip().lower()), 0
            )
            for item in str(row.get("Consequence", "")).split("&")
        ),
        default=0,
    )


def disease_architecture_rank_key(
    architecture: Mapping[str, Any], panel_item: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]
) -> tuple[Any, ...]:
    scores = sorted((consequence_score(row) for row in rows), reverse=True)
    complete = 2 if architecture["architecture"] in {"compound_heterozygous", "homozygous_alt"} else 1
    phase = {"in_trans_supported": 2, "phase_unknown": 1, "not_applicable": 1, "in_cis": 0}.get(
        str(architecture.get("phase_status")), 0
    )
    return (
        DISEASE_TIER_ORDER.get(str(panel_item.get("tier")), 99),
        -complete,
        -(scores[0] if scores else 0),
        -(scores[1] if len(scores) > 1 else scores[0] if scores else 0),
        -phase,
        str(architecture.get("gene", "")),
        str(architecture.get("variant_1", "")),
        str(architecture.get("variant_2", "")),
    )


def disease_architecture(
    gene: str,
    panel_item: Mapping[str, Any],
    architecture: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    phase_status: str = "not_applicable",
    rescue_status: str = "primary",
) -> dict[str, Any]:
    base = make_architecture(
        gene,
        architecture,
        rows,
        phase_status=phase_status,
        rescue_status=rescue_status,
    )
    row1 = rows[0]
    row2 = rows[1] if len(rows) > 1 else {}
    base.update(
        {
            "disease_tier": panel_item.get("tier", "mva_like"),
            "inheritance": ",".join(panel_item.get("inheritance", [])),
            "consequence_1": row1.get("Consequence", ""),
            "consequence_2": row2.get("Consequence", ""),
            "impact_1": row1.get("IMPACT", ""),
            "impact_2": row2.get("IMPACT", ""),
            "quality_1": row1.get("quality_status", ""),
            "quality_2": row2.get("quality_status", ""),
        }
    )
    base["rank_key"] = disease_architecture_rank_key(base, panel_item, rows)
    base["_rows"] = list(rows)
    return base


def build_disease_architectures(
    primary: Sequence[Mapping[str, Any]], panel: Mapping[str, Any]
) -> list[dict[str, Any]]:
    by_gene: dict[str, list[Mapping[str, Any]]] = {}
    for row in primary:
        canonical = canonical_panel_gene(row.get("gene") or row.get("SYMBOL") or row.get("Gene"), panel)
        if canonical is None:
            raise PipelineError(
                "primary/relevant allele cannot be resolved to the disease panel: "
                f"{row.get('gene') or row.get('SYMBOL') or row.get('Gene')}"
            )
        normalized = dict(row)
        normalized["gene"] = canonical
        by_gene.setdefault(canonical, []).append(normalized)
    output: list[dict[str, Any]] = []
    for item in panel["genes"]:
        gene = str(item["symbol"]).upper()
        rows = by_gene.get(gene, [])
        if not rows:
            continue
        if "AR" in item["inheritance"]:
            for row in rows:
                if row["zygosity"] == "hom_alt":
                    output.append(disease_architecture(gene, item, "homozygous_alt", [row]))
            heterozygotes = [row for row in rows if row["zygosity"] == "het"]
            for first, second in itertools.combinations(heterozygotes, 2):
                if first["variant_key"] == second["variant_key"]:
                    continue
                output.append(
                    disease_architecture(
                        gene,
                        item,
                        "compound_heterozygous",
                        [first, second],
                        phase_status=phase_relationship(first, second),
                    )
                )
        if "AD" in item["inheritance"]:
            for row in rows:
                if row["zygosity"] == "het":
                    output.append(disease_architecture(gene, item, "single_heterozygous", [row]))
    return sorted(output, key=lambda row: row["rank_key"])


def build_disease_rescue_architectures(
    primary: Sequence[Mapping[str, Any]], rescue: Sequence[Mapping[str, Any]], panel: Mapping[str, Any]
) -> list[dict[str, Any]]:
    primary_by_gene: dict[str, list[Mapping[str, Any]]] = {}
    rescue_by_gene: dict[str, list[Mapping[str, Any]]] = {}
    for row in primary:
        canonical = canonical_panel_gene(row.get("gene") or row.get("SYMBOL") or row.get("Gene"), panel)
        if canonical is None:
            raise PipelineError(
                "primary/relevant allele cannot be resolved to the disease panel: "
                f"{row.get('gene') or row.get('SYMBOL') or row.get('Gene')}"
            )
        normalized = dict(row)
        normalized["gene"] = canonical
        primary_by_gene.setdefault(canonical, []).append(normalized)
    for row in rescue:
        canonical = canonical_panel_gene(row.get("gene") or row.get("SYMBOL") or row.get("Gene"), panel)
        if canonical is None:
            raise PipelineError(
                "rescue/relevant allele cannot be resolved to the disease panel: "
                f"{row.get('gene') or row.get('SYMBOL') or row.get('Gene')}"
            )
        normalized = dict(row)
        normalized["gene"] = canonical
        rescue_by_gene.setdefault(canonical, []).append(normalized)
    output: list[dict[str, Any]] = []
    for item in panel["genes"]:
        gene = str(item["symbol"]).upper()
        primary_rows = primary_by_gene.get(gene, [])
        rescue_rows = rescue_by_gene.get(gene, [])
        if "AR" in item["inheritance"]:
            for row in rescue_rows:
                if row["zygosity"] == "hom_alt":
                    output.append(
                        disease_architecture(gene, item, "homozygous_alt", [row], rescue_status="rescue_only")
                    )
            for first in [row for row in primary_rows if row["zygosity"] == "het"]:
                for second in [row for row in rescue_rows if row["zygosity"] == "het"]:
                    if first["variant_key"] == second["variant_key"]:
                        continue
                    output.append(
                        disease_architecture(
                            gene,
                            item,
                            "compound_heterozygous",
                            [first, second],
                            phase_status=phase_relationship(first, second),
                            rescue_status="primary_plus_rescue",
                        )
                    )
            for first, second in itertools.combinations(
                [row for row in rescue_rows if row["zygosity"] == "het"], 2
            ):
                output.append(
                    disease_architecture(
                        gene,
                        item,
                        "compound_heterozygous",
                        [first, second],
                        phase_status=phase_relationship(first, second),
                        rescue_status="rescue_only",
                    )
                )
        if "AD" in item["inheritance"]:
            for row in rescue_rows:
                if row["zygosity"] == "het":
                    output.append(
                        disease_architecture(gene, item, "single_heterozygous", [row], rescue_status="rescue_only")
                    )
    return sorted(output, key=lambda row: row["rank_key"])


def disease_architecture_is_causal_eligible(
    architecture: Mapping[str, Any],
    panel_lookup: Mapping[str, Mapping[str, Any]],
) -> bool:
    """Return whether an architecture meets the predeclared first-submission gate."""
    if str(architecture.get("rescue_status", "primary")) != "primary":
        return False
    if str(architecture.get("phase_status", "")) == "in_cis":
        return False
    gene = str(architecture.get("gene", "")).upper()
    item = panel_lookup.get(gene)
    if item is None:
        return False
    mode = set(item.get("inheritance", []))
    kind = str(architecture.get("architecture", ""))
    if kind == "compound_heterozygous" and "AR" not in mode:
        return False
    if kind == "homozygous_alt" and "AR" not in mode:
        return False
    if kind == "single_heterozygous" and "AD" not in mode:
        return False
    if kind not in {"compound_heterozygous", "homozygous_alt", "single_heterozygous"}:
        return False
    rows = architecture.get("_rows", [])
    if not rows or any(str(row.get("quality_status", "fail")) == "fail" for row in rows):
        return False
    if kind == "compound_heterozygous" and len(rows) != 2:
        return False
    # A first submission requires a protein-altering or essential splice allele;
    # splice-region-only and noncoding observations remain visible but are not
    # promoted without additional evidence.
    if any(consequence_score(row) < 4 for row in rows):
        return False
    return True


def submission_clarity(
    architectures: Sequence[Mapping[str, Any]], panel: Mapping[str, Any]
) -> tuple[bool, str]:
    """Apply the non-negotiable clarity gate before spending a submission."""
    panel_lookup = {str(item["symbol"]).upper(): item for item in panel["genes"]}
    eligible = [
        row for row in architectures if disease_architecture_is_causal_eligible(row, panel_lookup)
    ]
    if not eligible:
        return False, "no_primary_coding_architecture"
    top = eligible[0]
    # Ignore gene/coordinate tie-breakers when deciding whether the evidence is
    # genuinely unique; equal disease-tier, inheritance, consequence, and phase
    # evidence must be escalated rather than spent as a blind submission.
    top_signature = tuple(top["rank_key"][:5])
    equivalent = [row for row in eligible if tuple(row["rank_key"][:5]) == top_signature]
    if len(equivalent) != 1:
        return False, "equivalent_top_architectures"
    return True, "unique_primary_coding_architecture"


def rank_disease_architectures(
    architectures: Sequence[Mapping[str, Any]], panel: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Add stable ranks and submission eligibility without learned scores."""
    panel_lookup = {str(item["symbol"]).upper(): item for item in panel["genes"]}
    ranked: list[dict[str, Any]] = []
    for index, architecture in enumerate(architectures, start=1):
        row = dict(architecture)
        row["rank"] = index
        row["submission_eligible"] = disease_architecture_is_causal_eligible(row, panel_lookup)
        row["variant_1_consequence"] = row.get("consequence_1", "")
        row["variant_2_consequence"] = row.get("consequence_2", "")
        ranked.append(row)
    return ranked


OFFICIAL_SUBMISSION_FIELDS = [
    "proband_id",
    "chrom_1",
    "pos_1",
    "ref_1",
    "alt_1",
    "chrom_2",
    "pos_2",
    "ref_2",
    "alt_2",
    "epcr",
    "finding_type",
    "notes",
]


def architecture_submission_row(
    architecture: Mapping[str, Any], sample_id: str, *, rank_index: int = 0
) -> dict[str, Any]:
    rows = list(architecture.get("_rows", []))
    if not rows:
        raise PipelineError("cannot export an architecture without variant rows")
    first = rows[0]
    second = rows[1] if len(rows) > 1 else {}
    epcr = max(0.01, 1.0 - (rank_index * 0.01))
    notes = (
        f"{architecture.get('gene','')}; {architecture.get('architecture','')}; "
        f"tier={architecture.get('disease_tier','')}; "
        f"consequences={architecture.get('consequence_1','')}"
    )
    if second:
        notes += f"+{architecture.get('consequence_2','')}"
    notes += f"; phase={architecture.get('phase_status','not_applicable')}"
    return {
        "proband_id": sample_id,
        "chrom_1": first.get("CHROM", ""),
        "pos_1": first.get("POS", ""),
        "ref_1": first.get("REF", ""),
        "alt_1": first.get("ALT", ""),
        "chrom_2": second.get("CHROM", "") if second else "",
        "pos_2": second.get("POS", "") if second else "",
        "ref_2": second.get("REF", "") if second else "",
        "alt_2": second.get("ALT", "") if second else "",
        "epcr": f"{epcr:.2f}",
        "finding_type": "primary",
        "notes": notes,
    }


def write_submission_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    if len(rows) > 10:
        raise PipelineError("submission contains more than the official ten-row limit")
    seen: set[tuple[str, ...]] = set()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OFFICIAL_SUBMISSION_FIELDS)
        writer.writeheader()
        for row in rows:
            if not (0 < float(row["epcr"]) <= 1):
                raise PipelineError("submission EPCR must be in (0,1]")
            key = tuple(str(row.get(field, "")) for field in OFFICIAL_SUBMISSION_FIELDS[:9])
            if key in seen:
                raise PipelineError("submission contains duplicate normalized variants")
            seen.add(key)
            if not str(row.get("chrom_1", "")).startswith("chr"):
                raise PipelineError("submission chromosome must use GRCh38 chr style")
            writer.writerow({field: row.get(field, "") for field in OFFICIAL_SUBMISSION_FIELDS})


def rename_normalized_to_chr(
    normalization: Mapping[str, Any],
    run_dir: Path,
    *,
    command_log: Path,
    tmp_path: Path,
    expected_samples: int,
) -> tuple[Path, dict[str, Any]]:
    """Create the official chr-prefixed working representation from the shared norm."""
    source = Path(normalization["canonical"])
    output = run_dir / "panel.normalized.vcf.gz"
    reverse_map = run_dir / "input/contig-map-to-reference.tsv"
    run_logged(
        [
            "bcftools",
            "annotate",
            "--rename-chrs",
            str(reverse_map),
            "-Oz",
            "-o",
            str(output),
            str(source),
        ],
        stage="panel-rename-to-chr",
        command_log=command_log,
        env=external_env(tmp_path),
    )
    run_logged(
        ["bcftools", "index", "--csi", "--force", str(output)],
        stage="panel-normalized-index",
        command_log=command_log,
        env=external_env(tmp_path),
    )
    header = parse_vcf_header(run(["bcftools", "view", "-h", str(output)], capture=True).stdout)
    if len(header["samples"]) != expected_samples:
        raise PipelineError("sample count changed while creating chr-prefixed VCF")
    if not header["contigs"] or not all(name.startswith("chr") for name in header["contigs"]):
        raise PipelineError("targeted normalized VCF is not chr-prefixed GRCh38")
    if first_record(["bcftools", "view", "-m3", "-H", str(output)]):
        raise PipelineError("targeted normalized VCF is not biallelic")
    return output, {
        "path": str(output),
        "sha256": sha256_file(output),
        "index": str(output) + ".csi",
        "sample_count": len(header["samples"]),
        "contig_style": "chr-prefixed",
    }


def disease_funnel(
    *,
    source_records: int,
    panel_records: int,
    canonical_alleles: int,
    nonref: int,
    all_nonref: Sequence[Mapping[str, Any]],
    classified: Mapping[str, Sequence[Mapping[str, Any]]],
    primary_architectures: Sequence[Mapping[str, Any]],
    rescue_architectures: Sequence[Mapping[str, Any]],
    ranked: Sequence[Mapping[str, Any]],
    final_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    relevant = [row for row in all_nonref if row.get("biological_relevance")]
    quality_pass = [
        row for row in relevant if row.get("quality_status") in {"pass", "not_evaluable"}
    ]
    primary_genes = {str(row.get("gene", "")).upper() for row in classified["primary"]}
    rows = [
        {"path": "shared", "stage_order": 1, "stage_id": "source_records", "unit": "record", "count": source_records, "parent_stage": "", "rule": "all indexed records in supplied VCF"},
        {"path": "shared", "stage_order": 2, "stage_id": "panel_interval_records", "unit": "record", "count": panel_records, "parent_stage": "source_records", "rule": "records intersecting panel gene intervals plus 2 kb"},
        {"path": "shared", "stage_order": 3, "stage_id": "canonical_biallelic_alleles", "unit": "allele", "count": canonical_alleles, "parent_stage": "", "rule": "GRCh38 normalize and split multiallelic records"},
        {"path": "transparent", "stage_order": 4, "stage_id": "nonreference_sample_alleles", "unit": "allele", "count": nonref, "parent_stage": "canonical_biallelic_alleles", "rule": "sample genotype contains an alternate allele"},
        {"path": "transparent", "stage_order": 5, "stage_id": "coding_splice_alleles", "unit": "gene_allele", "count": len(relevant), "parent_stage": "", "rule": "bcftools csq HIGH/MODERATE, splice, or ClinVar P/LP"},
        {"path": "transparent", "stage_order": 6, "stage_id": "primary_quality_alleles", "unit": "gene_allele", "count": len(quality_pass), "parent_stage": "coding_splice_alleles", "rule": "PASS/.; DP>=10 and GQ>=20 when present"},
        {"path": "transparent", "stage_order": 7, "stage_id": "primary_alleles", "unit": "gene_allele", "count": len(classified["primary"]), "parent_stage": "primary_quality_alleles", "rule": "frequency_not_evaluated; local CSQ provided no population AF/ClinVar fields"},
        {"path": "transparent", "stage_order": 8, "stage_id": "candidate_genes", "unit": "gene", "count": len(primary_genes), "parent_stage": "", "rule": "panel genes with primary qualifying alleles"},
        {"path": "transparent", "stage_order": 9, "stage_id": "inheritance_architectures", "unit": "architecture", "count": len(primary_architectures), "parent_stage": "", "rule": "AR hom/compound-het or AD single-het"},
        {"path": "rescue", "stage_order": 6, "stage_id": "quality_rescue_alleles", "unit": "gene_allele", "count": len(classified["rescue"]), "parent_stage": "coding_splice_alleles", "rule": "biologically plausible allele excluded only by FILTER/DP/GQ"},
        {"path": "rescue", "stage_order": 9, "stage_id": "quality_rescue_architectures", "unit": "architecture", "count": len(rescue_architectures), "parent_stage": "", "rule": "primary+rescue or rescue-only; separate branch"},
        {"path": "ranked", "stage_order": 10, "stage_id": "ranked_architectures", "unit": "candidate", "count": len(ranked), "parent_stage": "", "rule": "literature tier, inheritance, consequence, quality, phase"},
        {"path": "ranked", "stage_order": 11, "stage_id": "submission_rows", "unit": "row", "count": len(final_rows), "parent_stage": "", "rule": "official CSV rows, max ten"},
    ]
    validate_funnel(rows)
    return rows


def disease_bias_guard(repo: Path, panel_path: Path, command_log: Path) -> dict[str, Any]:
    """Guard against case-specific or public-clue targeting while allowing the panel."""
    panel = load_panel(panel_path)
    symbols = {str(item["symbol"]).upper() for item in panel["genes"]}
    canonical = {str(item["symbol"]).upper() for item in panel["genes"] if item.get("tier") == "canonical_mva"}
    if len(canonical) < 4 or len(symbols) < 8:
        raise PipelineError("disease panel is too narrow for the established MVA knowledge layer")
    command_text = command_log.read_text(encoding="utf-8", errors="replace")
    if re.search(r"(?:leaderboard|target[_ -]?gene|--gene(?:=|\s))", command_text, re.IGNORECASE):
        raise PipelineError("disease-informed command log contains a case-specific target clue")
    panel_text = panel_path.read_text(encoding="utf-8", errors="replace")
    if re.search(r"leaderboard|target[_ -]?gene|override", panel_text, re.IGNORECASE):
        raise PipelineError("disease panel contains a target-gene override or public clue")
    return {
        "passed": True,
        "panel_path": str(panel_path),
        "panel_sha256": sha256_file(panel_path),
        "gene_count": len(symbols),
        "canonical_tier_count": len(canonical),
        "target_gene_override": None,
        "public_clue_detected": False,
        "checked_repo": str(repo),
    }


def annotate_rescue_alerts(
    rescue_architectures: Sequence[Mapping[str, Any]],
    primary_architectures: Sequence[Mapping[str, Any]],
    exomiser_top_genes: set[str],
) -> list[dict[str, Any]]:
    primary_genes = {str(row["gene"]) for row in primary_architectures}
    output: list[dict[str, Any]] = []
    for architecture in rescue_architectures:
        row = dict(architecture)
        reasons: list[str] = []
        if not primary_genes:
            reasons.append("no_primary_architecture")
        if row["gene"] in exomiser_top_genes:
            reasons.append("exomiser_top20_gene")
        row["rescue_alert"] = ";".join(reasons)
        output.append(row)
    return output


def generate_exomiser_job(
    repo: Path,
    destination: Path,
    *,
    sample_id: str,
    hpo_ids: Sequence[str],
) -> None:
    template = (repo / "config/exomiser-unbiased.yml").read_text(encoding="utf-8")
    if re.search(r"genePanelFilter|target[_ -]?gene", template, flags=re.IGNORECASE):
        raise PipelineError("Exomiser template contains a gene-targeting construct")
    quoted_hpo = ", ".join(f"'{item}'" for item in hpo_ids)
    safe_sample_id = sample_id.replace("'", "''")
    sample = (
        "---\n"
        "sample:\n"
        f"  proband: '{safe_sample_id}'\n"
        f"  hpoIds: [{quoted_hpo}]\n"
        "  vcf: /input/canonical.grch38.biallelic.vcf.gz\n"
        "  genomeAssembly: hg38\n"
    )
    body = template.removeprefix("---\n")
    destination.write_text(sample + body, encoding="utf-8")


def run_exomiser(
    config: Mapping[str, Any], run_dir: Path, *, command_log: Path, tmp_path: Path
) -> tuple[Path, Path, Path]:
    reference_root = Path(config["storage"]["reference_root"])
    data_dir = reference_root / config["references"]["exomiser"]["directory"]
    canonical_dir = run_dir / "working"
    job_dir = run_dir / "hpo"
    output_dir = run_dir / "exomiser"
    output_dir.mkdir(parents=True, exist_ok=True)
    image = config["containers"]["exomiser"]
    command = [
        "docker",
        "run",
        "--rm",
        "--memory",
        "12g",
        "--cpus",
        "6",
        "-e",
        "JAVA_TOOL_OPTIONS=-Xmx10g",
        "-e",
        "EXOMISER_HG38_DATA_VERSION=2602",
        "-e",
        "EXOMISER_PHENOTYPE_DATA_VERSION=2602",
        "-v",
        f"{data_dir}:/exomiser-data:ro",
        "-v",
        f"{canonical_dir}:/input:ro",
        "-v",
        f"{job_dir}:/run:ro",
        "-v",
        f"{output_dir}:/output:rw",
        image,
        "analyse",
        "--analysis",
        "/run/exomiser-job.yml",
        "--output-directory",
        "/output",
    ]
    result = run_logged(
        command,
        stage="exomiser",
        command_log=command_log,
        output_log=output_dir / "run.log",
        env=external_env(tmp_path),
        check=False,
    )
    if result.returncode != 0:
        log_tail = (output_dir / "run.log").read_text(encoding="utf-8", errors="replace")[-10000:]
        if not re.search(r"outofmemory|java heap space|cannot allocate memory", log_tail, re.IGNORECASE):
            raise PipelineError("Exomiser failed; see external exomiser/run.log")
        retry = command.copy()
        retry[retry.index("12g")] = "14g"
        retry[retry.index("JAVA_TOOL_OPTIONS=-Xmx10g")] = "JAVA_TOOL_OPTIONS=-Xmx12g"
        retry_result = run_logged(
            retry,
            stage="exomiser-oom-retry",
            command_log=command_log,
            output_log=output_dir / "run.log",
            env=external_env(tmp_path),
            check=False,
        )
        if retry_result.returncode != 0:
            raise PipelineError("Exomiser failed after the single permitted OOM retry")
    expected: dict[str, Path] = {}
    suffixes = {"genes": ("genes.tsv", "gene.tsv"), "variants": ("variants.tsv", "variant.tsv"), "html": (".html",)}
    for kind, endings in suffixes.items():
        matches = [
            path
            for path in output_dir.iterdir()
            if path.is_file() and any(path.name.lower().endswith(ending) for ending in endings)
        ]
        if not matches:
            raise PipelineError(f"Exomiser did not create required {kind} output")
        source = sorted(matches)[0]
        canonical_name = {"genes": "genes.tsv", "variants": "variants.tsv", "html": "report.html"}[kind]
        destination = output_dir / canonical_name
        if source != destination:
            source.replace(destination)
        expected[kind] = destination
    return expected["genes"], expected["variants"], expected["html"]


def run_vep(
    config: Mapping[str, Any], run_dir: Path, *, command_log: Path, tmp_path: Path
) -> tuple[Path, Path]:
    reference_root = Path(config["storage"]["reference_root"])
    cache_dir = reference_root / config["references"]["vep"]["directory"]
    working = run_dir / "working"
    output_dir = run_dir / "vep"
    output_dir.mkdir(parents=True, exist_ok=True)
    annotated = output_dir / "annotated.vcf.gz"
    image = config["containers"]["vep"]
    command = [
        "docker",
        "run",
        "--rm",
        "--memory",
        "8g",
        "--cpus",
        "6",
        "-v",
        f"{cache_dir}:/opt/vep/.vep:ro",
        "-v",
        f"{working}:/input:ro",
        "-v",
        f"{output_dir}:/output:rw",
        image,
        "vep",
        "--input_file",
        "/input/canonical.grch38.biallelic.vcf.gz",
        "--output_file",
        "/output/annotated.vcf.gz",
        "--format",
        "vcf",
        "--vcf",
        "--compress_output",
        "bgzip",
        "--cache",
        "--offline",
        "--dir_cache",
        "/opt/vep/.vep",
        "--cache_version",
        "116",
        "--assembly",
        "GRCh38",
        "--species",
        "homo_sapiens",
        "--pick_allele_gene",
        "--symbol",
        "--mane",
        "--canonical",
        "--variant_class",
        "--sift",
        "b",
        "--polyphen",
        "b",
        "--af_gnomade",
        "--af_gnomadg",
        "--max_af",
        "--check_existing",
        "--clin_sig_allele",
        "1",
        "--fork",
        "4",
        "--buffer_size",
        "5000",
        "--force_overwrite",
        "--no_stats",
    ]
    result = run_logged(
        command,
        stage="vep",
        command_log=command_log,
        output_log=output_dir / "run.log",
        env=external_env(tmp_path),
        check=False,
    )
    if result.returncode != 0 or not annotated.is_file():
        raise PipelineError("VEP failed; see external vep/run.log")
    run_logged(
        ["bcftools", "index", "--csi", "--force", str(annotated)],
        stage="vep-index",
        command_log=command_log,
        env=external_env(tmp_path),
    )
    alleles = output_dir / "alleles.tsv.gz"
    split_vep_to_tsv(annotated, alleles, command_log=command_log, tmp_path=tmp_path)
    return annotated, alleles


def split_vep_to_tsv(
    annotated: Path, destination: Path, *, command_log: Path, tmp_path: Path
) -> None:
    header = run(["bcftools", "view", "-h", str(annotated)], capture=True).stdout
    parsed = parse_vcf_header(header)
    sample_fields = [field for field in ("GT", "DP", "GQ", "PS") if field in parsed["formats"]]
    base_fields = ["CHROM", "POS", "REF", "ALT", "FILTER"]
    vep_fields = [
        "SYMBOL",
        "Gene",
        "Consequence",
        "IMPACT",
        "MANE_SELECT",
        "CANONICAL",
        "MAX_AF",
        "gnomADe_AF",
        "gnomADg_AF",
        "CLIN_SIG",
        "Existing_variation",
    ]
    query = "%CHROM\t%POS\t%REF\t%ALT\t%FILTER"
    if sample_fields:
        query += "[" + "".join(f"\t%{field}" for field in sample_fields) + "]"
    query += "".join(f"\t%{field}" for field in vep_fields) + "\n"
    include = 'IMPACT="HIGH" || IMPACT="MODERATE" || Consequence~"splice" || CLIN_SIG~"pathogenic"'
    command = [
        "bcftools",
        "+split-vep",
        str(annotated),
        "-d",
        "-A",
        "tab",
        "-i",
        include,
        "-f",
        query,
    ]
    append_command_log(command_log, "vep-split", command)
    env = os.environ.copy()
    env.update(external_env(tmp_path))
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    assert process.stdout is not None
    with gzip.open(destination, "wt", encoding="utf-8", newline="") as handle:
        handle.write("\t".join(base_fields + sample_fields + vep_fields) + "\n")
        for line in process.stdout:
            handle.write(line)
    process.stdout.close()
    stderr = process.stderr.read() if process.stderr else ""
    code = process.wait()
    if code:
        raise PipelineError(f"bcftools +split-vep failed: {stderr[-1000:]}")


def find_gene_column(fieldnames: Sequence[str]) -> str:
    normalized = {re.sub(r"[^a-z]", "", field.lower()): field for field in fieldnames}
    for candidate in ("genesymbol", "symbol", "gene"):
        if candidate in normalized:
            return normalized[candidate]
    raise PipelineError(f"cannot identify gene column in fields: {', '.join(fieldnames)}")


def read_exomiser_genes(path: Path, limit: int) -> list[dict[str, Any]]:
    rows = read_tsv(path)
    if not rows:
        raise PipelineError("Exomiser gene output is empty")
    gene_column = find_gene_column(list(rows[0]))
    output = []
    seen: set[str] = set()
    for row in rows:
        gene = normalize_missing(row.get(gene_column))
        if not gene or gene in seen:
            continue
        seen.add(gene)
        output.append({"exomiser_rank": len(output) + 1, "gene": gene, "exomiser_row": row})
        if len(output) >= limit:
            break
    return output


def reconcile_candidates(
    exomiser_genes: Sequence[Mapping[str, Any]],
    primary_architectures: Sequence[Mapping[str, Any]],
    shortlist_limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rank_by_gene = {str(row["gene"]): int(row["exomiser_rank"]) for row in exomiser_genes}
    architectures_by_gene: dict[str, list[Mapping[str, Any]]] = {}
    for row in primary_architectures:
        architectures_by_gene.setdefault(str(row["gene"]), []).append(row)
    reconciliation: list[dict[str, Any]] = []
    for gene, rank in rank_by_gene.items():
        architectures = architectures_by_gene.get(gene, [])
        if architectures:
            for architecture in architectures:
                reconciliation.append(
                    {
                        "tier": 1,
                        "gene": gene,
                        "exomiser_rank": rank,
                        "architecture": architecture["architecture"],
                        "variant_1": architecture["variant_1"],
                        "variant_2": architecture["variant_2"],
                        "phase_status": architecture["phase_status"],
                        "rank_key": architecture["rank_key"],
                    }
                )
        else:
            reconciliation.append(
                {
                    "tier": 2,
                    "gene": gene,
                    "exomiser_rank": rank,
                    "architecture": "none_in_transparent_primary",
                    "variant_1": "",
                    "variant_2": "",
                    "phase_status": "not_applicable",
                    "rank_key": "",
                }
            )
    for gene, architectures in architectures_by_gene.items():
        if gene in rank_by_gene:
            continue
        for architecture in architectures:
            reconciliation.append(
                {
                    "tier": 3,
                    "gene": gene,
                    "exomiser_rank": "",
                    "architecture": architecture["architecture"],
                    "variant_1": architecture["variant_1"],
                    "variant_2": architecture["variant_2"],
                    "phase_status": architecture["phase_status"],
                    "rank_key": architecture["rank_key"],
                }
            )
    reconciliation.sort(
        key=lambda row: (
            int(row["tier"]),
            int(row["exomiser_rank"]) if row["exomiser_rank"] != "" else 10**9,
            str(row["rank_key"]),
            str(row["gene"]),
            str(row["variant_1"]),
        )
    )
    shortlist: list[dict[str, Any]] = []
    seen_genes: set[str] = set()
    for row in reconciliation:
        gene = str(row["gene"])
        if gene in seen_genes:
            continue
        seen_genes.add(gene)
        shortlist.append({"unbiased_rank": len(shortlist) + 1, **row})
        if len(shortlist) >= shortlist_limit:
            break
    return reconciliation, shortlist


FUNNEL_FIELDS = ["path", "stage_order", "stage_id", "unit", "count", "parent_stage", "rule"]
RECONCILIATION_FIELDS = [
    "tier",
    "gene",
    "exomiser_rank",
    "architecture",
    "variant_1",
    "variant_2",
    "phase_status",
    "rank_key",
]
SHORTLIST_FIELDS = ["unbiased_rank", *RECONCILIATION_FIELDS]


def count_nonreference_genotypes(vcf: Path) -> int:
    command = ["bcftools", "query", "-f", "[%GT\n]", str(vcf)]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert process.stdout is not None
    count = sum(1 for line in process.stdout if genotype_state(line.strip()) != "reference_or_missing")
    process.stdout.close()
    stderr = process.stderr.read() if process.stderr else ""
    code = process.wait()
    if code:
        raise PipelineError(f"failed to count non-reference genotypes: {stderr[-1000:]}")
    return count


def make_funnel(
    normalization: Mapping[str, Any],
    classified: Mapping[str, Sequence[Mapping[str, Any]]],
    primary_architectures: Sequence[Mapping[str, Any]],
    rescue_architectures: Sequence[Mapping[str, Any]],
    exomiser_genes: Sequence[Mapping[str, Any]],
    reconciliation: Sequence[Mapping[str, Any]],
    shortlist: Sequence[Mapping[str, Any]],
    nonref_count: int,
) -> list[dict[str, Any]]:
    all_nonref = classified["all_nonref"]
    quality_pass = [row for row in all_nonref if row["quality_status"] == "pass"]
    relevant = [row for row in all_nonref if row["biological_relevance"]]
    primary = classified["primary"]
    primary_genes = {str(row["gene"]) for row in primary}
    rescue = classified["rescue"]
    rows = [
        {"path": "shared", "stage_order": 1, "stage_id": "source_records", "unit": "record", "count": normalization["source_records"], "parent_stage": "", "rule": "supplied indexed VCF"},
        {"path": "shared", "stage_order": 2, "stage_id": "canonical_alleles", "unit": "allele", "count": normalization["canonical_biallelic_records"], "parent_stage": "", "rule": "GRCh38 normalize and split multiallelics"},
        {"path": "transparent", "stage_order": 3, "stage_id": "nonreference_alleles", "unit": "allele", "count": nonref_count, "parent_stage": "canonical_alleles", "rule": "sample GT contains alternate allele"},
        {"path": "transparent", "stage_order": 4, "stage_id": "relevant_annotated_alleles", "unit": "gene_allele", "count": len(relevant), "parent_stage": "", "rule": "HIGH/MODERATE, splice, or ClinVar P/LP"},
        {"path": "transparent", "stage_order": 5, "stage_id": "relevant_quality_pass", "unit": "gene_allele", "count": len(quality_pass), "parent_stage": "relevant_annotated_alleles", "rule": "FILTER PASS/.; DP>=10 and GQ>=20 when present"},
        {"path": "transparent", "stage_order": 6, "stage_id": "primary_frequency_compatible", "unit": "gene_allele", "count": len(primary), "parent_stage": "relevant_quality_pass", "rule": "AF thresholds by inheritance; missing AF retained"},
        {"path": "transparent", "stage_order": 7, "stage_id": "primary_candidate_genes", "unit": "gene", "count": len(primary_genes), "parent_stage": "", "rule": "genes with primary qualifying alleles"},
        {"path": "transparent", "stage_order": 8, "stage_id": "primary_architectures", "unit": "architecture", "count": len(primary_architectures), "parent_stage": "", "rule": "explicit hom-alt, comp-het, or rare single-het architecture"},
        {"path": "rescue", "stage_order": 5, "stage_id": "quality_rescue_alleles", "unit": "gene_allele", "count": len(rescue), "parent_stage": "relevant_annotated_alleles", "rule": "excluded solely by FILTER/DP/GQ"},
        {"path": "rescue", "stage_order": 6, "stage_id": "quality_rescue_architectures", "unit": "architecture", "count": len(rescue_architectures), "parent_stage": "", "rule": "primary+rescue or rescue-only; separate from ranking"},
        {"path": "exomiser", "stage_order": 7, "stage_id": "exomiser_top20", "unit": "gene", "count": len(exomiser_genes), "parent_stage": "", "rule": "phenotype-aware PASS_ONLY genome-wide ranking"},
        {"path": "reconciled", "stage_order": 9, "stage_id": "reconciled_candidates", "unit": "candidate", "count": len(reconciliation), "parent_stage": "", "rule": "predeclared three-tier reconciliation"},
        {"path": "reconciled", "stage_order": 10, "stage_id": "frozen_shortlist", "unit": "candidate", "count": len(shortlist), "parent_stage": "reconciled_candidates", "rule": "stable gene-deduplicated top shortlist"},
    ]
    validate_funnel(rows)
    return rows


def validate_funnel(rows: Sequence[Mapping[str, Any]]) -> None:
    by_stage = {str(row["stage_id"]): row for row in rows}
    if len(by_stage) != len(rows):
        raise PipelineError("funnel stage identifiers are not unique")
    for row in rows:
        count = int(row["count"])
        if count < 0:
            raise PipelineError(f"negative funnel count at {row['stage_id']}")
        parent_id = str(row.get("parent_stage", ""))
        if not parent_id:
            continue
        parent = by_stage.get(parent_id)
        if parent is None:
            raise PipelineError(f"missing funnel parent stage: {parent_id}")
        if row["unit"] == parent["unit"] and count > int(parent["count"]):
            raise PipelineError(f"funnel count exceeds parent at {row['stage_id']}")


def bias_guard(paths: Sequence[Path]) -> dict[str, Any]:
    forbidden_yaml = re.compile(r"genePanelFilter|target[_ -]?gene", re.IGNORECASE)
    checked = []
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="replace")
        if forbidden_yaml.search(text):
            raise PipelineError(f"bias guard found a gene-targeting construct in {path}")
        if path.name == "commands.log":
            for line in text.splitlines():
                if not line.strip():
                    continue
                record = json.loads(line)
                if "--gene" in record.get("argv", []):
                    raise PipelineError("bias guard found a targeted-gene command in unbiased run")
        checked.append({"path": str(path), "sha256": sha256_file(path)})
    return {
        "passed": True,
        "target_gene_prior": None,
        "gene_panel": None,
        "added_hpo_terms": 0,
        "checked": checked,
    }


def verify_reference_manifest(config: Mapping[str, Any]) -> tuple[Path, dict[str, Any]]:
    reference_root = Path(config["storage"]["reference_root"])
    manifest_path = reference_root / "reference-manifest.json"
    if not manifest_path.is_file():
        raise PipelineError("reference setup manifest is missing; run setup first")
    manifest = load_json(manifest_path)
    expected_releases = {name: item["release"] for name, item in config["references"].items()}
    if manifest.get("releases") != expected_releases:
        raise PipelineError("reference manifest release pins do not match configuration")
    for record in manifest.get("assets", []):
        path = Path(record["path"])
        if not path.is_file() or path.stat().st_size != int(record["bytes"]):
            raise PipelineError(f"reference asset is missing or changed in size: {path}")
    return manifest_path, manifest


def git_commit(repo: Path) -> str:
    result = run(["git", "rev-parse", "HEAD"], cwd=repo, capture=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else "UNCOMMITTED"


def write_checksums(run_dir: Path) -> Path:
    checksum_path = run_dir / "checksums.sha256"
    entries = []
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file() or path in {checksum_path, run_dir / "FROZEN.json"}:
            continue
        entries.append((sha256_file(path), path.relative_to(run_dir).as_posix()))
    with checksum_path.open("w", encoding="utf-8") as handle:
        for digest, relative in entries:
            handle.write(f"{digest}  {relative}\n")
    return checksum_path


def validate_checksums(run_dir: Path) -> None:
    checksum_path = run_dir / "checksums.sha256"
    if not checksum_path.is_file():
        raise PipelineError("frozen run checksum manifest is missing")
    with checksum_path.open(encoding="utf-8") as handle:
        for line in handle:
            digest, relative = line.rstrip("\n").split("  ", 1)
            path = run_dir / relative
            if not path.is_file() or sha256_file(path) != digest:
                raise PipelineError(f"frozen run checksum failed: {relative}")


def make_run_id(prefix: str) -> str:
    return f"{prefix}-{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}"


DISEASE_RANKED_FIELDS = [
    "rank",
    *DISEASE_ARCHITECTURE_FIELDS,
    "submission_eligible",
    "variant_1_consequence",
    "variant_2_consequence",
]


def _indexed_record_count(vcf: Path, *, tmp_path: Path | None = None) -> int:
    env = external_env(tmp_path) if tmp_path is not None else None
    result = run(["bcftools", "index", "-n", str(vcf)], capture=True, check=False, env=env)
    try:
        indexed = int(result.stdout.strip())
    except ValueError:
        indexed = 0
    if result.returncode == 0 and indexed > 0:
        return indexed
    # Older supplied TBI files can be readable but lack count metadata. Keep the
    # count-only contract by streaming records to a counter without retaining or
    # emitting variant content.
    process = subprocess.Popen(
        ["bcftools", "view", "-H", str(vcf)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert process.stdout is not None
    count = sum(1 for _ in process.stdout)
    process.stdout.close()
    stderr = process.stderr.read() if process.stderr else ""
    code = process.wait()
    if code:
        raise PipelineError(f"unable to read tabix index record count: {vcf}: {stderr[-500:]}")
    return count


def _read_header_checked(vcf: Path, expected_samples: int) -> dict[str, Any]:
    header = parse_vcf_header(run(["bcftools", "view", "-h", str(vcf)], capture=True).stdout)
    if len(header["samples"]) != expected_samples:
        raise PipelineError(f"expected {expected_samples} VCF sample(s); found {len(header['samples'])}")
    if not header["contigs"]:
        raise PipelineError("VCF header contains no contigs")
    return header


def run_disease_informed(
    repo: Path,
    config: Mapping[str, Any],
    *,
    panel_path: Path,
    dry_run: bool,
) -> None:
    """Run the small diagnosis-informed panel workflow and freeze its artifacts."""
    panel = load_panel(panel_path, expected_assembly=str(config["inputs"]["assembly"]))
    lightweight = config.get("lightweight", {})
    required_free = int(lightweight.get("minimum_run_free_gib", 2))
    storage = validate_storage(
        config,
        phase="unbiased",
        create=False,
        require_docker=False,
        minimum_free_gib=required_free,
    )
    require_executable("bcftools")
    inputs = config["inputs"]
    source_vcf = repo / inputs["vcf"]
    source_index = repo / inputs["vcf_index"]
    phenotype = repo / inputs["phenotype_docx"]
    missing = [path for path in (source_vcf, source_index, phenotype) if not path.is_file()]
    if missing:
        raise PipelineError(f"required gated input is missing: {', '.join(map(str, missing))}")
    if dry_run:
        print(
            json.dumps(
                {
                    "status": "dry-run-ok",
                    "storage": storage,
                    "panel": str(panel_path),
                    "panel_gene_count": len(panel["genes"]),
                    "input_files_present": len((source_vcf, source_index, phenotype)),
                    "input_records_read": 0,
                    "planned_stages": [
                        "exact_hpo",
                        "panel_intervals",
                        "targeted_extract",
                        "normalize_biallelic",
                        "local_csq",
                        "architectures",
                        "funnel",
                        "freeze",
                    ],
                    "additional_downloads": [],
                },
                indent=2,
            )
        )
        return

    validate_storage(
        config,
        phase="unbiased",
        create=True,
        require_docker=False,
        minimum_free_gib=required_free,
    )
    light_manifest_path, light_manifest = verify_light_manifest(config)
    reference_root = Path(config["storage"]["reference_root"])
    genome_dir = reference_root / config["references"]["genome"]["directory"]
    fasta = genome_dir / "Homo_sapiens_assembly38.fasta.gz"
    fai = genome_dir / "Homo_sapiens_assembly38.fasta.gz.fai"
    # The earlier approved Exomiser checkpoint retained the Broad files in
    # reference/downloads/. Install hard links into the pinned genome directory
    # when the archive-only checkpoint has not yet populated that directory.
    if not fasta.is_file() or not fai.is_file():
        downloads = reference_root / "downloads"
        for name in ("Homo_sapiens_assembly38.fasta.gz", "Homo_sapiens_assembly38.fasta.gz.fai"):
            source = downloads / name
            if not source.is_file():
                raise PipelineError(f"Broad GRCh38 reference file is missing: {source}")
            copy_reference_file(source, genome_dir / name)
    if not fasta.is_file() or not fai.is_file():
        raise PipelineError("Broad GRCh38 FASTA and FAI are required for targeted normalization")
    private_root = Path(config["storage"]["private_run_root"])
    run_root = private_root / "disease-informed"
    run_root.mkdir(parents=True, exist_ok=True)
    run_dir = run_root / make_run_id("mva")
    if run_dir.exists():
        raise PipelineError(f"refusing to overwrite existing run: {run_dir}")
    for name in ("hpo", "input", "working"):
        (run_dir / name).mkdir(parents=True, exist_ok=False)
    command_log = run_dir / "commands.log"
    command_log.touch()
    run_tmp = Path(config["storage"]["run_tmp"]) / run_dir.name
    run_tmp.mkdir(parents=True, exist_ok=False)
    started_at = utc_now()

    hpo_ids = extract_hpo_docx(phenotype)
    if len(hpo_ids) != int(inputs["expected_hpo_terms"]):
        raise PipelineError(
            f"expected {inputs['expected_hpo_terms']} unique HPO terms; extracted {len(hpo_ids)}"
        )
    (run_dir / "hpo-exact.tsv").write_text(
        "hpo_id\n" + "\n".join(hpo_ids) + "\n", encoding="utf-8"
    )
    panel_snapshot = panel_path.read_text(encoding="utf-8")
    (run_dir / "panel-snapshot.yml").write_text(panel_snapshot, encoding="utf-8")

    source_hash = sha256_file(source_vcf)
    source_header = _read_header_checked(source_vcf, int(inputs["expected_samples"]))
    source_records = _indexed_record_count(source_vcf, tmp_path=run_tmp)
    annotation_dir = reference_root / config["references"]["lightweight_annotation"]["directory"]
    gff_path = annotation_dir / "Homo_sapiens.GRCh38.116.gff3.gz"
    if not gff_path.is_file():
        raise PipelineError("Ensembl GFF3 is missing from the lightweight setup")
    interval_info = panel_gff_and_intervals(
        gff_path=gff_path,
        panel=panel,
        vcf_contigs=source_header["contigs"],
        destination_gff=run_dir / "input/panel.ensembl.chr.gff3.gz",
        destination_bed=run_dir / "panel-intervals.bed",
        padding_bp=int(lightweight.get("interval_padding_bp", 2000)),
    )
    panel_extract = run_dir / "input/panel.extract.vcf.gz"
    run_logged(
        [
            "bcftools",
            "view",
            "--regions-file",
            str(run_dir / "panel-intervals.bed"),
            "-Oz",
            "-o",
            str(panel_extract),
            str(source_vcf),
        ],
        stage="targeted-extract",
        command_log=command_log,
        env=external_env(run_tmp),
    )
    run_logged(
        ["bcftools", "index", "--tbi", "--force", str(panel_extract)],
        stage="targeted-extract-index",
        command_log=command_log,
        env=external_env(run_tmp),
    )
    panel_records = _indexed_record_count(panel_extract, tmp_path=run_tmp)
    normalization = normalize_vcf(
        original=panel_extract,
        fasta=fasta,
        fai=fai,
        run_dir=run_dir,
        tmp_path=run_tmp,
        expected_samples=int(inputs["expected_samples"]),
    )
    normalized_path, normalized_summary = rename_normalized_to_chr(
        normalization,
        run_dir,
        command_log=command_log,
        tmp_path=run_tmp,
        expected_samples=int(inputs["expected_samples"]),
    )
    csq_vcf = run_dir / "panel.csq.vcf.gz"
    run_logged(
        [
            "bcftools",
            "csq",
            "--local-csq",
            "-f",
            str(fasta),
            "-g",
            str(run_dir / "input/panel.ensembl.chr.gff3.gz"),
            "--ncsq",
            "46",
            "-Oz",
            "-o",
            str(csq_vcf),
            str(normalized_path),
        ],
        stage="local-csq",
        command_log=command_log,
        env=external_env(run_tmp),
    )
    run_logged(
        ["bcftools", "index", "--csi", "--force", str(csq_vcf)],
        stage="local-csq-index",
        command_log=command_log,
        env=external_env(run_tmp),
    )
    parsed_rows = parse_csquery(
        csq_vcf,
        panel_gene_ids=interval_info["gene_ids"],
        panel_gene_lookup=panel["gene_lookup"],
        command_log=command_log,
        tmp_path=run_tmp,
    )
    classified = classify_vep_rows(parsed_rows, config["thresholds"])
    primary_architectures = build_disease_architectures(classified["primary"], panel)
    rescue_architectures = build_disease_rescue_architectures(
        classified["primary"], classified["rescue"], panel
    )
    ranked = rank_disease_architectures(primary_architectures, panel)
    clear, clarity_reason = submission_clarity(ranked, panel)
    # The sample identifier remains private and is used only in the official CSV.
    sample_id = source_header["samples"][0]
    submission_rows = (
        [architecture_submission_row(row, sample_id, rank_index=0) for row in ranked if row["submission_eligible"]]
        if clear
        else []
    )
    if submission_rows:
        submission_rows = submission_rows[:1]

    write_tsv(run_dir / "alleles.tsv", classified["all_nonref"], ALLELE_FIELDS)
    write_tsv(run_dir / "architectures.tsv", ranked, DISEASE_RANKED_FIELDS)
    write_tsv(run_dir / "ranked-candidates.tsv", ranked, DISEASE_RANKED_FIELDS)
    write_tsv(run_dir / "quality-rescue.tsv", classified["rescue"], ALLELE_FIELDS)
    write_tsv(
        run_dir / "quality-rescue-architectures.tsv",
        rescue_architectures,
        [*DISEASE_ARCHITECTURE_FIELDS, "rescue_alert"],
    )
    write_submission_csv(run_dir / "submission-1.csv", submission_rows)
    write_json(
        run_dir / "source-summary.json",
        {
            "assembly": "GRCh38",
            "source_sha256": source_hash,
            "source_records": source_records,
            "panel_interval_records": panel_records,
            "sample_count": len(source_header["samples"]),
            "sample_identifiers_emitted": False,
            "source_contig_style": "unprefixed",
            "panel_gene_count": len(panel["genes"]),
            "hpo_term_count": len(hpo_ids),
            "population_frequency_evaluated": False,
            "clinvar_evaluated": False,
        },
    )
    write_json(
        run_dir / "normalization-summary.json",
        {
            **{key: (str(value) if isinstance(value, Path) else value) for key, value in normalization.items()},
            **normalized_summary,
            "source_hash_immutable": source_hash,
        },
    )
    funnel = disease_funnel(
        source_records=source_records,
        panel_records=panel_records,
        canonical_alleles=int(normalization["canonical_biallelic_records"]),
        nonref=count_nonreference_genotypes(normalized_path),
        all_nonref=classified["all_nonref"],
        classified=classified,
        primary_architectures=primary_architectures,
        rescue_architectures=rescue_architectures,
        ranked=ranked,
        final_rows=submission_rows,
    )
    write_tsv(run_dir / "funnel.tsv", funnel, FUNNEL_FIELDS)
    guard = disease_bias_guard(repo, panel_path, command_log)
    preflight = {
        "assembly": "GRCh38",
        "sample_count": len(source_header["samples"]),
        "sample_identifiers_emitted": False,
        "hpo_term_count": len(hpo_ids),
        "storage": storage,
        "light_reference_manifest_sha256": sha256_file(light_manifest_path),
        "light_reference_release": light_manifest["release"],
        "panel_gene_count": len(panel["genes"]),
        "clarity_gate": {"passed": clear, "reason": clarity_reason},
        "bias_guard": guard,
    }
    write_json(run_dir / "preflight.json", preflight)
    manifest = {
        "schema_version": 1,
        "workflow": "disease-informed-minimal",
        "run_id": run_dir.name,
        "started_at": started_at,
        "completed_at": utc_now(),
        "code_commit": git_commit(repo),
        "config_sha256": sha256_file(config_path(repo)),
        "panel_sha256": sha256_file(panel_path),
        "light_reference_manifest_sha256": sha256_file(light_manifest_path),
        "input_vcf_sha256": source_hash,
        "canonical_vcf_sha256": normalized_summary["sha256"],
        "sample_count": len(source_header["samples"]),
        "hpo_term_count": len(hpo_ids),
        "target_gene_override": None,
        "submission_ready": clear,
        "clarity_reason": clarity_reason,
        "tools": {
            "bcftools": command_version(["bcftools", "--version"]),
            "python": sys.version.split()[0],
        },
        "outputs": {
            "normalized_vcf": str(normalized_path),
            "csq_vcf": str(csq_vcf),
            "alleles": str(run_dir / "alleles.tsv"),
            "architectures": str(run_dir / "architectures.tsv"),
            "ranked_candidates": str(run_dir / "ranked-candidates.tsv"),
            "submission": str(run_dir / "submission-1.csv"),
        },
    }
    write_json(run_dir / "run-manifest.json", manifest)
    checksums = write_checksums(run_dir)
    frozen = {
        "schema_version": 1,
        "run_id": run_dir.name,
        "state": "FROZEN_DISEASE_INFORMED",
        "frozen_at": utc_now(),
        "technical_complete": True,
        "submission_ready": clear,
        "clarity_reason": clarity_reason,
        "code_commit": manifest["code_commit"],
        "input_vcf_sha256": source_hash,
        "canonical_vcf_sha256": normalized_summary["sha256"],
        "config_sha256": manifest["config_sha256"],
        "panel_sha256": manifest["panel_sha256"],
        "light_reference_manifest_sha256": manifest["light_reference_manifest_sha256"],
        "shortlist_sha256": sha256_file(run_dir / "architectures.tsv"),
        "submission_sha256": sha256_file(run_dir / "submission-1.csv"),
        "checksums_sha256": sha256_file(checksums),
        "bias_guard": guard,
    }
    write_json(run_dir / "FROZEN.json", frozen)
    print(json.dumps({"status": "frozen", "run": str(run_dir), "submission_ready": clear}, indent=2))


def export_submission(
    config: Mapping[str, Any], *, run_id: str, rows: int, dry_run: bool
) -> None:
    if rows < 1 or rows > 10:
        raise PipelineError("--rows must be between 1 and 10")
    validate_storage(config, phase="unbiased", create=False, require_docker=False, minimum_free_gib=2)
    private_root = Path(config["storage"]["private_run_root"])
    run_dir = Path(run_id)
    if not run_dir.is_absolute():
        run_dir = private_root / "disease-informed" / run_id
    run_dir = resolved_without_escape(run_dir, private_root)
    frozen_path = run_dir / "FROZEN.json"
    if not frozen_path.is_file():
        raise PipelineError("submission export requires a frozen disease-informed run")
    frozen = load_json(frozen_path)
    if frozen.get("state") != "FROZEN_DISEASE_INFORMED" or not frozen.get("technical_complete"):
        raise PipelineError("run is not a technically complete disease-informed freeze")
    checksum_path = run_dir / "checksums.sha256"
    if sha256_file(checksum_path) != frozen.get("checksums_sha256"):
        raise PipelineError("frozen checksum manifest hash does not match")
    validate_checksums(run_dir)
    if not frozen.get("submission_ready"):
        raise PipelineError(f"submission clarity gate failed: {frozen.get('clarity_reason', 'unknown')}")
    source = run_dir / "submission-1.csv"
    if not source.is_file():
        raise PipelineError("frozen submission CSV is missing")
    with source.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        source_rows = list(reader)
    if not source_rows:
        raise PipelineError("frozen submission CSV contains no candidate row")
    selected = source_rows[:rows]
    if dry_run:
        print(json.dumps({"status": "dry-run-ok", "run": str(run_dir), "rows": len(selected)}, indent=2))
        return
    destination_root = private_root / "submissions"
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = destination_root / f"{run_dir.name}-submission.csv"
    write_submission_csv(destination, selected)
    print(json.dumps({"status": "exported", "path": str(destination), "rows": len(selected)}, indent=2))


def run_unbiased(repo: Path, config: Mapping[str, Any], *, dry_run: bool) -> None:
    storage = validate_storage(config, phase="unbiased", create=False, require_docker=True)
    require_executable("bcftools")
    require_executable("docker")
    inputs = config["inputs"]
    required_inputs = [repo / inputs[key] for key in ("vcf", "vcf_index", "phenotype_docx")]
    missing = [path for path in required_inputs if not path.is_file()]
    if missing:
        raise PipelineError(f"required gated input is missing: {', '.join(map(str, missing))}")
    if dry_run:
        print(json.dumps({
            "status": "dry-run-ok",
            "storage": storage,
            "input_files_present": len(required_inputs),
            "input_records_read": 0,
            "planned_stages": ["normalize", "exomiser", "vep", "inheritance", "reconcile", "freeze"],
        }, indent=2))
        return
    validate_storage(config, phase="unbiased", create=True, require_docker=True)
    reference_manifest_path, reference_manifest = verify_reference_manifest(config)
    private_root = Path(config["storage"]["private_run_root"])
    run_dir = private_root / "unbiased" / make_run_id("unbiased")
    if run_dir.exists():
        raise PipelineError(f"refusing to overwrite existing run: {run_dir}")
    for name in ("hpo", "input", "working", "exomiser", "vep", "transparent"):
        (run_dir / name).mkdir(parents=True, exist_ok=False if name == "hpo" else True)
    command_log = run_dir / "commands.log"
    command_log.touch()
    tmp_path = Path(config["storage"]["run_tmp"])
    started_at = utc_now()
    hpo_ids = extract_hpo_docx(repo / inputs["phenotype_docx"])
    if len(hpo_ids) != int(inputs["expected_hpo_terms"]):
        raise PipelineError(
            f"expected {inputs['expected_hpo_terms']} unique HPO terms; extracted {len(hpo_ids)}"
        )
    (run_dir / "hpo/hpo-exact.tsv").write_text("hpo_id\n" + "\n".join(hpo_ids) + "\n", encoding="utf-8")
    reference_root = Path(config["storage"]["reference_root"])
    genome_dir = reference_root / config["references"]["genome"]["directory"]
    fasta = genome_dir / "Homo_sapiens_assembly38.fasta.gz"
    fai = genome_dir / "Homo_sapiens_assembly38.fasta.gz.fai"
    normalization = normalize_vcf(
        original=repo / inputs["vcf"],
        fasta=fasta,
        fai=fai,
        run_dir=run_dir,
        tmp_path=tmp_path,
        expected_samples=int(inputs["expected_samples"]),
    )
    generate_exomiser_job(
        repo,
        run_dir / "hpo/exomiser-job.yml",
        sample_id=str(normalization.pop("sample_id")),
        hpo_ids=hpo_ids,
    )
    exomiser_genes_path, exomiser_variants_path, _ = run_exomiser(
        config, run_dir, command_log=command_log, tmp_path=tmp_path
    )
    _, alleles_path = run_vep(config, run_dir, command_log=command_log, tmp_path=tmp_path)
    classified = classify_vep_rows(read_tsv(alleles_path), config["thresholds"])
    primary_architectures = build_primary_architectures(classified["primary"])
    exomiser_top = read_exomiser_genes(
        exomiser_genes_path, int(config["thresholds"]["exomiser_reconcile_top"])
    )
    rescue_architectures = annotate_rescue_alerts(
        build_rescue_architectures(classified["primary"], classified["rescue"]),
        primary_architectures,
        {str(row["gene"]) for row in exomiser_top},
    )
    transparent = run_dir / "transparent"
    write_tsv(transparent / "primary-candidates.tsv", classified["primary"], ALLELE_FIELDS)
    write_tsv(transparent / "primary-architectures.tsv", primary_architectures, ARCHITECTURE_FIELDS)
    write_tsv(transparent / "quality-rescue.tsv", classified["rescue"], ALLELE_FIELDS)
    write_tsv(
        transparent / "quality-rescue-architectures.tsv",
        rescue_architectures,
        [*ARCHITECTURE_FIELDS, "rescue_alert"],
    )
    write_tsv(transparent / "filter-reasons.tsv", classified["filter_rows"], ALLELE_FIELDS)
    reconciliation, shortlist = reconcile_candidates(
        exomiser_top,
        primary_architectures,
        int(config["thresholds"]["frozen_shortlist"]),
    )
    write_tsv(run_dir / "reconciliation.tsv", reconciliation, RECONCILIATION_FIELDS)
    write_tsv(run_dir / "unbiased-shortlist.tsv", shortlist, SHORTLIST_FIELDS)
    nonref_count = count_nonreference_genotypes(Path(normalization["canonical"]))
    funnel = make_funnel(
        normalization,
        classified,
        primary_architectures,
        rescue_architectures,
        exomiser_top,
        reconciliation,
        shortlist,
        nonref_count,
    )
    write_tsv(run_dir / "funnel.tsv", funnel, FUNNEL_FIELDS)
    guard = bias_guard(
        [repo / "config/exomiser-unbiased.yml", run_dir / "hpo/exomiser-job.yml", command_log]
    )
    preflight = {
        "assembly": normalization["assembly"],
        "contig_style": normalization["contig_style"],
        "sample_count": normalization["sample_count"],
        "sample_identifiers_emitted": False,
        "hpo_term_count": len(hpo_ids),
        "storage": storage,
        "bias_guard": guard,
    }
    write_json(run_dir / "preflight.json", preflight)
    manifest = {
        "schema_version": 1,
        "run_id": run_dir.name,
        "started_at": started_at,
        "completed_at": utc_now(),
        "code_commit": git_commit(repo),
        "config_sha256": sha256_file(config_path(repo)),
        "exomiser_template_sha256": sha256_file(repo / "config/exomiser-unbiased.yml"),
        "reference_manifest": str(reference_manifest_path),
        "reference_manifest_sha256": sha256_file(reference_manifest_path),
        "reference_releases": reference_manifest["releases"],
        "input_vcf_sha256": normalization["source_sha256"],
        "canonical_vcf_sha256": normalization["canonical_sha256"],
        "hpo_term_count": len(hpo_ids),
        "sample_count": normalization["sample_count"],
        "target_gene_prior": None,
        "gene_panel": None,
        "tools": {
            "bcftools": command_version(["bcftools", "--version"]),
            "python": sys.version.split()[0],
            "docker": command_version(["docker", "--version"]),
        },
        "outputs": {
            "canonical_vcf": str(normalization["canonical"]),
            "exomiser_genes": str(exomiser_genes_path),
            "exomiser_variants": str(exomiser_variants_path),
            "shortlist": str(run_dir / "unbiased-shortlist.tsv"),
        },
    }
    write_json(run_dir / "run-manifest.json", manifest)
    checksums = write_checksums(run_dir)
    frozen = {
        "schema_version": 1,
        "run_id": run_dir.name,
        "state": "FROZEN_UNBIASED",
        "frozen_at": utc_now(),
        "technical_complete": True,
        "code_commit": manifest["code_commit"],
        "input_vcf_sha256": normalization["source_sha256"],
        "canonical_vcf_sha256": normalization["canonical_sha256"],
        "config_sha256": manifest["config_sha256"],
        "reference_manifest_sha256": manifest["reference_manifest_sha256"],
        "shortlist_sha256": sha256_file(run_dir / "unbiased-shortlist.tsv"),
        "checksums_sha256": sha256_file(checksums),
        "bias_guard": guard,
    }
    write_json(run_dir / "FROZEN.json", frozen)
    print(f"unbiased run frozen: {run_dir}")


def rows_for_gene(path: Path, gene: str) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    rows = read_tsv(path)
    if not rows:
        return []
    gene_upper = gene.upper()
    return [row for row in rows if any(normalize_missing(value).upper() == gene_upper for value in row.values())]


def run_confirmation(
    config: Mapping[str, Any], *, unbiased_run: str, gene: str, dry_run: bool
) -> None:
    validate_storage(config, phase="unbiased", create=False, require_docker=False)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", gene):
        raise PipelineError("gene must be a simple gene symbol")
    private_root = Path(config["storage"]["private_run_root"])
    run_dir = Path(unbiased_run)
    if not run_dir.is_absolute():
        run_dir = private_root / "unbiased" / unbiased_run
    run_dir = resolved_without_escape(run_dir, private_root)
    frozen_path = run_dir / "FROZEN.json"
    if not frozen_path.is_file():
        raise PipelineError("targeted confirmation requires an existing FROZEN.json")
    frozen = load_json(frozen_path)
    if frozen.get("state") != "FROZEN_UNBIASED" or not frozen.get("technical_complete"):
        raise PipelineError("unbiased run is not a technically complete freeze")
    if sha256_file(run_dir / "checksums.sha256") != frozen.get("checksums_sha256"):
        raise PipelineError("frozen checksum manifest hash does not match")
    validate_checksums(run_dir)
    if dry_run:
        print(json.dumps({"status": "dry-run-ok", "unbiased_run": str(run_dir), "gene": gene}, indent=2))
        return
    target_dir = private_root / "targeted" / make_run_id("confirmation")
    target_dir.mkdir(parents=True, exist_ok=False)
    sources = {
        "unbiased_shortlist": run_dir / "unbiased-shortlist.tsv",
        "reconciliation": run_dir / "reconciliation.tsv",
        "primary_candidates": run_dir / "transparent/primary-candidates.tsv",
        "primary_architectures": run_dir / "transparent/primary-architectures.tsv",
        "quality_rescue": run_dir / "transparent/quality-rescue.tsv",
        "quality_rescue_architectures": run_dir / "transparent/quality-rescue-architectures.tsv",
        "exomiser_genes": run_dir / "exomiser/genes.tsv",
        "exomiser_variants": run_dir / "exomiser/variants.tsv",
    }
    index_rows = []
    for source_name, source_path in sources.items():
        matches = rows_for_gene(source_path, gene)
        if not matches:
            continue
        output = target_dir / f"{source_name}.tsv"
        write_tsv(output, matches, list(matches[0]))
        index_rows.append(
            {
                "source": source_name,
                "source_path": str(source_path),
                "matching_rows": len(matches),
                "output": str(output),
            }
        )
    write_tsv(target_dir / "confirmation-index.tsv", index_rows, ["source", "source_path", "matching_rows", "output"])
    write_json(
        target_dir / "confirmation-manifest.json",
        {
            "schema_version": 1,
            "created_at": utc_now(),
            "gene": gene,
            "unbiased_run": str(run_dir),
            "unbiased_frozen_sha256": sha256_file(frozen_path),
            "unbiased_files_modified": False,
            "matching_sources": len(index_rows),
        },
    )
    print(f"targeted confirmation complete: {target_dir}")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--repo", type=Path, default=Path.cwd())
    subparsers = result.add_subparsers(dest="command", required=True)
    setup = subparsers.add_parser("setup", help="validate and download external references")
    setup.add_argument("--dry-run", action="store_true")
    setup.add_argument("--stop-after", help="stop after this asset is downloaded and verified")
    setup_light_parser = subparsers.add_parser(
        "setup-light", help="download and verify the small Ensembl GFF3 annotation"
    )
    setup_light_parser.add_argument("--dry-run", action="store_true")
    disease = subparsers.add_parser(
        "disease-informed", help="run and freeze the diagnosis-informed panel analysis"
    )
    disease.add_argument("--panel", type=Path, help="disease panel YAML-compatible JSON")
    disease.add_argument("--dry-run", action="store_true")
    export = subparsers.add_parser(
        "export-submission", help="copy a frozen official CSV to the private submissions directory"
    )
    export.add_argument("--run", required=True)
    export.add_argument("--rows", type=int, default=1)
    export.add_argument("--dry-run", action="store_true")
    unbiased = subparsers.add_parser("unbiased", help="run and freeze the unbiased analysis")
    unbiased.add_argument("--dry-run", action="store_true")
    confirm = subparsers.add_parser("confirm", help="inspect a gene only after a valid unbiased freeze")
    confirm.add_argument("--unbiased-run", required=True)
    confirm.add_argument("--gene", required=True)
    confirm.add_argument("--dry-run", action="store_true")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    repo = args.repo.resolve()
    try:
        config = load_config(repo)
        if args.command == "setup":
            setup_references(repo, config, dry_run=args.dry_run, stop_after=args.stop_after)
        elif args.command == "setup-light":
            setup_light(repo, config, dry_run=args.dry_run)
        elif args.command == "disease-informed":
            panel_path = (args.panel or repo / config["lightweight"]["panel"]).resolve()
            run_disease_informed(repo, config, panel_path=panel_path, dry_run=args.dry_run)
        elif args.command == "export-submission":
            export_submission(config, run_id=args.run, rows=args.rows, dry_run=args.dry_run)
        elif args.command == "unbiased":
            run_unbiased(repo, config, dry_run=args.dry_run)
        elif args.command == "confirm":
            run_confirmation(
                config,
                unbiased_run=args.unbiased_run,
                gene=args.gene,
                dry_run=args.dry_run,
            )
        else:
            raise PipelineError(f"unsupported command: {args.command}")
    except PipelineError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
