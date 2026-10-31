import hashlib
import os

import requests
from tqdm import tqdm


class DownloadManager:
    def __init__(self, download_dir: str, chunk_size: int = 8192):
        self.download_dir = download_dir
        self.chunk_size = chunk_size
        os.makedirs(self.download_dir, exist_ok=True)

    def download_file(self, url: str, model_name: str, expected_hash: str | None = None) -> bool:
        """Download a file with resume support and checksum validation."""
        file_path = os.path.join(self.download_dir, model_name)
        temp_path = f"{file_path}.part"

        # Check if file already exists and is valid
        if os.path.exists(file_path):
            if expected_hash and self._validate_checksum(file_path, expected_hash):
                return True
            os.remove(file_path)

        # Resume partial download if possible
        headers = {}
        downloaded_bytes = 0
        if os.path.exists(temp_path):
            downloaded_bytes = os.path.getsize(temp_path)
            headers["Range"] = f"bytes={downloaded_bytes}-"

        try:
            response = requests.get(url, headers=headers, stream=True)
            response.raise_for_status()

            total_size = int(response.headers.get("content-length", 0)) + downloaded_bytes

            with open(temp_path, "ab" if downloaded_bytes else "wb") as f, tqdm(
                desc=model_name,
                total=total_size,
                unit="B",
                unit_scale=True,
                initial=downloaded_bytes,
            ) as pbar:
                for chunk in response.iter_content(chunk_size=self.chunk_size):
                    f.write(chunk)
                    pbar.update(len(chunk))

            os.rename(temp_path, file_path)

            if expected_hash and not self._validate_checksum(file_path, expected_hash):
                os.remove(file_path)
                return False

            return True
        except Exception as e:
            print(f"Download failed: {e}")
            return False

    def _validate_checksum(self, file_path: str, expected_hash: str) -> bool:
        """Validate file checksum."""
        sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(self.chunk_size), b""):
                sha256.update(chunk)
        return sha256.hexdigest() == expected_hash