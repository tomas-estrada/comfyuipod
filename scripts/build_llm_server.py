#!/usr/bin/env python3
"""Build the pinned llama.cpp CPU server and retain its upstream notices."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


def _read_memory_limit() -> int:
    limits = []
    for limit_path, usage_path in (
        (Path("/sys/fs/cgroup/memory.max"), Path("/sys/fs/cgroup/memory.current")),
        (
            Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
            Path("/sys/fs/cgroup/memory/memory.usage_in_bytes"),
        ),
    ):
        try:
            raw = limit_path.read_text().strip()
            value = int(raw)
        except (OSError, ValueError):
            continue
        if value > 0 and value < (1 << 60):
            try:
                used = int(usage_path.read_text().strip())
            except (OSError, ValueError):
                used = 0
            limits.append(max(0, value - used))
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                limits.append(int(line.split()[1]) * 1024)
                break
    except (OSError, ValueError, IndexError):
        pass
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        available_pages = os.sysconf("SC_AVPHYS_PAGES")
        available = page_size * available_pages
        if available > 0:
            limits.append(available)
    except (OSError, ValueError):
        pass
    if not limits:
        try:
            physical = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
            if physical > 0:
                limits.append(physical)
        except (OSError, ValueError):
            pass
    return min(limits) if limits else 4 * 1024**3


def _available_cpu_count() -> int:
    counts = []
    try:
        counts.append(len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        pass
    for quota_path, period_path in (
        (Path("/sys/fs/cgroup/cpu.max"), None),
        (Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us"), Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us")),
    ):
        try:
            if period_path is None:
                quota, period = quota_path.read_text().split()
                if quota == "max":
                    continue
                quota_value, period_value = int(quota), int(period)
            else:
                quota_value = int(quota_path.read_text())
                period_value = int(period_path.read_text())
                if quota_value < 0:
                    continue
            if quota_value > 0 and period_value > 0:
                counts.append(max(1, quota_value // period_value))
        except (OSError, ValueError, ZeroDivisionError):
            pass
    counts.append(os.cpu_count() or 1)
    return max(1, min(counts))


def build_jobs(memory_bytes: int, cpu_count: int | None = None) -> int:
    """Bound parallel compile jobs to both visible CPUs and available RAM."""
    cpus = cpu_count or _available_cpu_count()
    # llama.cpp C++ compilation is memory-heavy; budget 4 GiB per worker.
    return max(1, min(cpus, memory_bytes // (4 * 1024**3) or 1))


def _run(command: list[str], *, cwd: Path | None = None) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def validate_supplemental_notice(source: Path, expected_sha256: str) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"aviso suplementario ausente: {source}")
    actual_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    if actual_sha256 != expected_sha256:
        raise ValueError(
            f"SHA-256 de aviso suplementario no coincide: {actual_sha256} != "
            f"{expected_sha256}"
        )


def install_supplemental_notice(
    source: Path, expected_sha256: str, destination: Path
) -> None:
    validate_supplemental_notice(source, expected_sha256)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def parse_ldd_dependencies(output: str) -> list[str]:
    """Return absolute ELF dependency paths, rejecting unresolved libraries."""
    paths: set[str] = set()
    for line in output.splitlines():
        if " => not found" in line:
            raise RuntimeError(f"llama-server tiene una biblioteca dinámica ausente: {line}")
        match = re.search(r"(?:=>\s+)?(/\S+)", line)
        if match:
            paths.add(match.group(1))
    if not paths:
        raise RuntimeError("ldd no reportó bibliotecas dinámicas para llama-server")
    return sorted(paths)


def _parse_dpkg_owners(output: str) -> set[str]:
    return {
        line.partition(": ")[0]
        for line in output.splitlines()
        if ": " in line
    }


def package_owners_for_library(
    library_path: Path, *, soname: str | None = None
) -> tuple[set[str], Path]:
    """Resolve a linked library to its Debian package or a packaged same-SONAME copy.

    CUDA-enabled Python wheels can vendor runtime libraries such as libgomp in
    site-packages. Those files are not owned by dpkg, even though Ubuntu ships
    the same ELF SONAME as a normal system package. Use that package's notice
    only when the exact vendored path has no dpkg owner and ldconfig exposes a
    package-owned copy with the identical SONAME; otherwise fail closed.
    """
    try:
        owners = _parse_dpkg_owners(
            subprocess.check_output(
                ["dpkg-query", "-S", str(library_path)], text=True
            )
        )
    except subprocess.CalledProcessError:
        owners = set()
    if owners:
        return owners, library_path

    cache = subprocess.check_output(["ldconfig", "-p"], text=True)
    soname = soname or library_path.name
    matches: list[tuple[str, Path]] = []
    for line in cache.splitlines():
        name, separator, raw_path = line.partition("=>")
        if not separator or name.split("(", 1)[0].strip() != soname:
            continue
        candidate = Path(raw_path.strip())
        try:
            candidate = candidate.resolve(strict=True)
        except OSError:
            continue
        if candidate == library_path:
            continue
        try:
            candidate_owners = _parse_dpkg_owners(
                subprocess.check_output(
                    ["dpkg-query", "-S", str(candidate)], text=True
                )
            )
        except subprocess.CalledProcessError:
            continue
        matches.extend((owner, candidate) for owner in candidate_owners)

    if not matches:
        raise RuntimeError(
            f"sin paquete Debian propietario ni copia de sistema con SONAME "
            f"{soname} para biblioteca {library_path}"
        )
    return {owner for owner, _ in matches}, matches[0][1]


def _ubuntu_source_options(
    temporary: Path,
    *,
    sources_directory: Path = Path("/etc/apt/sources.list.d"),
    legacy_source_list: Path = Path("/etc/apt/sources.list"),
) -> list[str]:
    """Build isolated apt source settings from the image's Ubuntu mirrors."""
    source_parts = temporary / "sources.list.d"
    source_parts.mkdir(parents=True, exist_ok=True)
    source_list = temporary / "sources.list"
    source_list.touch()
    stanzas: list[str] = []

    for source_file in sorted(sources_directory.glob("*.sources")):
        contents = source_file.read_text(encoding="utf-8")
        for stanza in re.split(r"\n\s*\n", contents.strip()):
            uri_line = next(
                (line.partition(":")[2].strip() for line in stanza.splitlines()
                 if line.startswith("URIs:")),
                "",
            )
            types_line = next(
                (line.partition(":")[2].split() for line in stanza.splitlines()
                 if line.startswith("Types:")),
                [],
            )
            if (
                not {"deb", "deb-src"}.intersection(types_line)
                or not any("ubuntu.com" in uri for uri in uri_line.split())
            ):
                continue
            stanzas.append(
                re.sub(r"(?m)^Types:.*$", "Types: deb-src", stanza, count=1)
                if "deb" in types_line
                else stanza
            )

    for source_file in [legacy_source_list, *sorted(sources_directory.glob("*.list"))]:
        if not source_file.is_file():
            continue
        for line in source_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if "ubuntu.com" not in stripped:
                continue
            if stripped.startswith("deb-src "):
                stanzas.append(stripped)
            elif stripped.startswith("deb "):
                stanzas.append("deb-src " + stripped[4:])

    if not stanzas:
        raise RuntimeError("no se encontraron mirrors Ubuntu para descargar el paquete fuente")
    (source_parts / "ubuntu-src.sources").write_text(
        "\n\n".join(stanzas) + "\n", encoding="utf-8"
    )
    return [
        "-o", f"Dir::Etc::sourcelist={source_list}",
        "-o", f"Dir::Etc::sourceparts={source_parts}",
    ]


def source_package_copyright(
    package: str, package_version: str, temporary: Path
) -> tuple[Path, str, str]:
    """Fetch the exact source package's Debian copyright file with apt verification."""
    package_name = package.partition(":")[0]
    source_fields = subprocess.check_output(
        ["dpkg-query", "-W", "-f=${source:Package}\t${source:Version}", package],
        text=True,
    ).strip().split("\t", 1)
    source_package = source_fields[0] if source_fields and source_fields[0] else package_name
    source_version = (
        source_fields[1]
        if len(source_fields) > 1 and source_fields[1]
        else package_version
    )
    apt_options = _ubuntu_source_options(temporary / "apt")
    subprocess.run(["apt-get", *apt_options, "update"], check=True)
    source_root = temporary / "source"
    source_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["apt-get", *apt_options, "source", f"{source_package}={source_version}"],
        cwd=source_root,
        check=True,
    )
    candidates = list(source_root.glob("*/debian/copyright"))
    if len(candidates) != 1 or not candidates[0].is_file():
        raise RuntimeError(
            f"no se encontró debian/copyright para el paquete fuente exacto "
            f"{source_package}={source_version} ({package}={package_version})"
        )
    return candidates[0], source_package, source_version


def install_system_library_notices(binary: Path, license_root: Path) -> list[dict[str, str]]:
    """Copy copyright files for every package owning a linked ELF dependency.

    The Ubuntu base image and compiler packages can change independently of the
    pinned llama.cpp source. Resolve notices from the exact runtime ELF closure
    during the image build and retain a hash-indexed inventory alongside them.
    """
    ldd_output = subprocess.check_output(["ldd", str(binary)], text=True)
    dependencies = parse_ldd_dependencies(ldd_output)
    notice_dir = license_root / "system-libraries"
    notice_dir.mkdir(parents=True, exist_ok=True)
    package_notices: dict[str, tuple[str, str, str, str, str, str]] = {}
    records: list[dict[str, str]] = []
    apt_indexes_ready = False

    def archive_notice(
        package: str, package_version: str, architecture: str
    ) -> tuple[Path, str, str, str]:
        package_name = package.partition(":")[0]
        with tempfile.TemporaryDirectory(prefix="llm-system-notice-") as temporary:
            package_root = Path(temporary)
            package_spec = f"{package_name}:{architecture}={package_version}"
            try:
                subprocess.run(
                    ["apt-get", "download", package_spec], cwd=package_root, check=True
                )
            except subprocess.CalledProcessError:
                # Ubuntu may have superseded the exact binary archive while its
                # source package metadata still resolves the installed version.
                source, source_package, source_version = source_package_copyright(
                    package, package_version, package_root
                )
                notice_source = "exact-source-package"
            else:
                archives = sorted(package_root.glob("*.deb"))
                if len(archives) != 1:
                    raise RuntimeError(
                        f"apt no devolvió un único archivo para paquete {package_spec}"
                    )
                archive = archives[0]
                fields = subprocess.check_output(
                    ["dpkg-deb", "--field", str(archive), "Package", "Version", "Architecture"],
                    text=True,
                )
                metadata = {
                    key: value.strip()
                    for line in fields.splitlines()
                    if ":" in line
                    for key, value in [line.split(":", 1)]
                }
                if (
                    metadata.get("Package") != package_name
                    or metadata.get("Version") != package_version
                    or metadata.get("Architecture") not in {architecture, "all"}
                ):
                    raise RuntimeError(
                        f"paquete descargado no coincide con el instalado: {package_spec}"
                    )
                extracted = package_root / "extracted"
                subprocess.run(
                    ["dpkg-deb", "--extract", str(archive), str(extracted)], check=True
                )
                source = extracted / "usr" / "share" / "doc" / package_name / "copyright"
                notice_source = "exact-package-archive"
                source_package = ""
                source_version = ""
                if not source.is_file():
                    source, source_package, source_version = source_package_copyright(
                        package, package_version, package_root
                    )
                    notice_source = "exact-source-package"
            destination = notice_dir / f"{re.sub(r'[^A-Za-z0-9_.+-]', '_', package)}.copyright"
            shutil.copy2(source, destination)
            return destination, notice_source, source_package, source_version

    for raw_path in dependencies:
        linked_path = Path(raw_path)
        library_path = linked_path.resolve(strict=True)
        packages, notice_owner_path = package_owners_for_library(
            library_path, soname=linked_path.name
        )
        if not packages:
            raise RuntimeError(f"sin paquete Debian propietario para biblioteca {library_path}")
        for package in sorted(packages):
            if package not in package_notices:
                package_version = subprocess.check_output(
                    ["dpkg-query", "-W", "-f=${Version}", package], text=True
                ).strip()
                architecture = subprocess.check_output(
                    ["dpkg-query", "-W", "-f=${Architecture}", package], text=True
                ).strip()
                if not package_version or not architecture:
                    raise RuntimeError(f"versión/arquitectura ausente para paquete {package}")
                files = subprocess.check_output(
                    ["dpkg-query", "-L", package], text=True
                ).splitlines()
                candidates = [
                    Path(item) for item in files
                    if item.startswith("/") and Path(item).name == "copyright"
                ]
                source_notice = next((item for item in candidates if item.is_file()), None)
                if source_notice is None:
                    if not apt_indexes_ready:
                        subprocess.run(["apt-get", "update"], check=True)
                        apt_indexes_ready = True
                    destination, source_kind, source_package, source_version = archive_notice(
                        package, package_version, architecture
                    )
                else:
                    safe_name = re.sub(r"[^A-Za-z0-9_.+-]", "_", package)
                    destination = notice_dir / f"{safe_name}.copyright"
                    shutil.copy2(source_notice, destination)
                    source_kind = "installed-package"
                    source_package = ""
                    source_version = ""
                digest = hashlib.sha256(destination.read_bytes()).hexdigest()
                package_notices[package] = (
                    str(destination.relative_to(license_root)), digest, package_version,
                    source_kind, source_package, source_version
                )
            (
                notice_path, digest, package_version, source_kind,
                source_package, source_version,
            ) = package_notices[package]
            record = {
                "library": str(library_path),
                "package": package,
                "package_version": package_version,
                "notice": notice_path,
                "sha256": digest,
                "notice_source": source_kind,
            }
            if notice_owner_path != library_path:
                record["notice_owner_resolution"] = "same-soname-system-package"
                record["notice_owner_library"] = str(notice_owner_path)
            if source_package:
                record["source_package"] = source_package
                record["source_version"] = source_version
            records.append(record)

    manifest = notice_dir / "manifest.json"
    manifest.write_text(
        json.dumps({"schema": "llama-server-system-notices/1", "libraries": records}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    return records


def _validate_supplemental_destination(lock: dict, supplemental: dict) -> str:
    destination = supplemental.get("destination")
    if (
        not isinstance(destination, str)
        or not destination
        or destination in {".", ".."}
        or "\x00" in destination
        or Path(destination).is_absolute()
        or Path(destination).name != destination
        or destination in lock["license_files"]
    ):
        raise ValueError(f"destination de aviso suplementario inválido: {destination!r}")
    return destination


def build(
    lock_path: Path,
    install_prefix: Path,
    work_root: Path | None = None,
    supplemental_license: Path | None = None,
) -> None:
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    revision = lock["revision"]
    if len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision):
        raise ValueError("revision debe ser un SHA Git de 40 caracteres")
    if lock["architecture"] != "linux-amd64":
        raise ValueError("este lock solo declara linux-amd64")
    supplemental = lock.get("supplemental_license")
    if supplemental is not None:
        _validate_supplemental_destination(lock, supplemental)
        if supplemental_license is None:
            raise ValueError("falta --supplemental-license requerido por el lock")
        validate_supplemental_notice(supplemental_license, supplemental["sha256"])
    elif supplemental_license is not None:
        raise ValueError("--supplemental-license no está declarado en el lock")

    if work_root is None:
        temp = tempfile.TemporaryDirectory(prefix="llama-cpp-build-")
        root = Path(temp.name)
    else:
        temp = None
        root = work_root
        root.mkdir(parents=True, exist_ok=True)
    try:
        source = root / "source"
        build_dir = root / "build"
        _run(["git", "init", str(source)])
        _run(["git", "-C", str(source), "remote", "add", "origin", lock["source"]])
        _run(["git", "-C", str(source), "fetch", "--depth=1", "origin", revision])
        _run(["git", "-C", str(source), "checkout", "--detach", "FETCH_HEAD"])
        actual = subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
        ).strip()
        if actual != revision:
            raise RuntimeError(f"checkout cambió de pin: {actual} != {revision}")

        options = [f"-D{key}={value}" for key, value in lock["cmake_options"].items()]
        _run(["cmake", "-S", str(source), "-B", str(build_dir), *options])
        jobs = build_jobs(_read_memory_limit())
        _run(
            [
                "cmake",
                "--build",
                str(build_dir),
                "--target",
                lock["target"],
                "--parallel",
                str(jobs),
            ]
        )
        binary = build_dir / "bin" / lock["target"]
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise RuntimeError(f"no se generó el binario ejecutable {binary}")
        install_prefix.mkdir(parents=True, exist_ok=True)
        shutil.copy2(binary, install_prefix / "llama-server")

        notice_root = Path("/usr/share/licenses/llama.cpp")
        for relative in lock["license_files"]:
            source_notice = source / relative
            if not source_notice.is_file():
                raise RuntimeError(f"notice listado en lock ausente: {relative}")
            destination = notice_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_notice, destination)
        if supplemental is not None:
            assert supplemental_license is not None
            install_supplemental_notice(
                supplemental_license,
                supplemental["sha256"],
                notice_root / supplemental["destination"],
            )
        install_system_library_notices(install_prefix / "llama-server", notice_root)
        _run([str(install_prefix / "llama-server"), "--version"])
    finally:
        if temp is not None:
            temp.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--install-prefix", type=Path, default=Path("/usr/local/bin"))
    parser.add_argument("--supplemental-license", type=Path)
    args = parser.parse_args()
    build(args.lock, args.install_prefix, supplemental_license=args.supplemental_license)


if __name__ == "__main__":
    main()
