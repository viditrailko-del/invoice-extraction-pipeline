"""Fetch the images for the real-invoice eval fixtures (eval/fixtures/real/*.json).

    python -m scripts.fetch_real_fixtures          (stdlib only: runs on the host or in Docker)

The images are NOT committed: they are third-party data (Zenodo record 7213544,
CC BY 4.0) and most show a private person's tax number. Only our hand-checked
labels live in git. This script rebuilds the image folder reproducibly:

  1. download 1_Images.zip from Zenodo once (cached in storage/, md5-verified)
  2. extract only the images our fixtures reference into eval/fixtures/real/files/
  3. verify each image's sha256 against the fixture, so a changed dataset is caught

Attribution: "Dataset of personal invoices and receipts including annotation of
relevant fields", Zenodo, https://zenodo.org/records/7213544 (CC BY 4.0).
"""

import hashlib
import json
import sys
import urllib.request
import zipfile
from pathlib import Path

FIXTURES = Path("eval/fixtures/real")
FILES = FIXTURES / "files"
CACHE = Path("storage/datasets/zenodo-7213544")
ZIP = CACHE / "1_Images.zip"
ZIP_URL = "https://zenodo.org/api/records/7213544/files/1_Images.zip/content"
ZIP_MD5 = "1b7c2c1ebb3e88f5294c5ec785135069"  # published by Zenodo for this record


def digest(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_zip() -> None:
    if ZIP.exists() and digest(ZIP, "md5") == ZIP_MD5:
        return
    CACHE.mkdir(parents=True, exist_ok=True)
    print(f"downloading {ZIP_URL} (~115 MB) ...")
    tmp = ZIP.with_suffix(".part")
    urllib.request.urlretrieve(ZIP_URL, tmp)
    if digest(tmp, "md5") != ZIP_MD5:
        tmp.unlink()
        sys.exit("downloaded archive failed the md5 check; aborting")
    tmp.replace(ZIP)


def main() -> None:
    fixtures = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(FIXTURES.glob("*.json"))]
    todo = [fx for fx in fixtures
            if not (FIXTURES / fx["file"]).exists()
            or digest(FIXTURES / fx["file"], "sha256") != fx["source"]["sha256"]]
    if not todo:
        print(f"all {len(fixtures)} real fixture images present and verified")
        return
    ensure_zip()
    FILES.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(ZIP) as z:
        for fx in todo:
            member = fx["source"]["original_file"]
            target = FIXTURES / fx["file"]
            # Write by our own name, never the archive's path (no path traversal possible).
            target.write_bytes(z.read(member))
            if digest(target, "sha256") != fx["source"]["sha256"]:
                target.unlink()
                sys.exit(f"{fx['id']}: {member} does not match the expected sha256; dataset changed?")
            print(f"{fx['id']}: {target}")
    print(f"done: {len(todo)} image(s) extracted and verified")


if __name__ == "__main__":
    main()
