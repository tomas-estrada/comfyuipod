#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/.." && pwd)"
lock_path="${repo_root}/upgrades-ultra-version/locks/ffmpeg-ubuntu24.04-amd64.lock.json"

mapfile -t package_fields < <(
    python3 - "${lock_path}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as lock_file:
    package = json.load(lock_file)["package"]

if package["distribution"] != "Ubuntu 24.04 LTS (noble)":
    raise SystemExit("CI FFmpeg lock must target Ubuntu 24.04 LTS")
if package["architecture"] != "amd64":
    raise SystemExit("CI FFmpeg lock must target amd64")
for key in ("filename", "url", "sha256", "version"):
    print(package[key])
PY
)

if [[ "${#package_fields[@]}" -ne 4 ]]; then
    echo "Invalid FFmpeg lock fields" >&2
    exit 1
fi

filename="${package_fields[0]}"
url="${package_fields[1]}"
expected_sha256="${package_fields[2]}"
expected_version="${package_fields[3]}"
temp_dir="$(mktemp -d)"
trap 'rm -rf -- "${temp_dir}"' EXIT
package_path="${temp_dir}/${filename}"

curl --fail --location --retry 5 --retry-all-errors --retry-delay 5 \
    --output "${package_path}" "${url}"
printf '%s  %s\n' "${expected_sha256}" "${package_path}" | sha256sum --check --strict
sudo apt-get update
sudo apt-get install --yes --no-install-recommends "${package_path}"

actual_version="$(dpkg-query --show --showformat='${Version}' ffmpeg)"
if [[ "${actual_version}" != "${expected_version}" ]]; then
    echo "FFmpeg version mismatch: ${actual_version} != ${expected_version}" >&2
    exit 1
fi
command -v ffmpeg >/dev/null
command -v ffprobe >/dev/null
ffmpeg -version | head -n 1
ffprobe -version | head -n 1
