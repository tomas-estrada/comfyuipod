import argparse
import json
import os

from studio.download_manager import DownloadManager


def main():
    parser = argparse.ArgumentParser(description="Download models with resume support.")
    parser.add_argument("--manifest", type=str, required=True, help="Path to models manifest JSON")
    parser.add_argument("--download-dir", type=str, required=True, help="Directory to save models")
    args = parser.parse_args()

    if not os.path.exists(args.manifest):
        print(f"Manifest file not found: {args.manifest}")
        return

    with open(args.manifest, "r") as f:
        models = json.load(f)

    downloader = DownloadManager(args.download_dir)

    for model in models:
        if not downloader.download_file(model["url"], model["name"], model.get("sha256")):
            print(f"Failed to download {model['name']}")

if __name__ == "__main__":
    main()