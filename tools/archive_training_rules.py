"""Archive rules and solver source for replaying samples from this release."""
import json
import zipfile
from pathlib import Path

from app.catalog import ROOT
from app.result_cache import _catalog_fingerprint, _code_fingerprint


def main():
    catalog, solver = _catalog_fingerprint(), _code_fingerprint()
    directory = ROOT / "artifacts" / "training-rules"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{catalog}-{solver}.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps({"catalogHash": catalog, "solverHash": solver}))
        files = list((ROOT / "app").glob("*.py"))
        files += [ROOT / "assets" / name for name in ("wiki_artifacts.json", "wiki_tablets.json.gz", "wiki_zh_cn.json")]
        files += [ROOT / "requirements-runtime.txt"]
        for file in files:
            archive.write(file, file.relative_to(ROOT))
    print(path)


if __name__ == "__main__":
    main()
