"""Package the available synthetic-case ledgers for offline, repeatable scoring."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import zipfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
EXTENSION = HERE.parent / "2026-11-10-paired-v2-extension"
ARCHIVE = HERE / "evidence.zip"


def main():
    result = subprocess.run([sys.executable, str(HERE / "analyze_all.py")], cwd=ROOT,
                            capture_output=True, text=True, encoding="utf-8", check=True)
    summary = json.loads(result.stdout)
    for arm in ("causal", "one_shot"):
        row = summary["arms"][arm]
        if row["cases_expected"] != 32 or row["cases_invalid"] != 0 or row["cases_verified"] + row["cases_missing"] != 32:
            raise ValueError("refusing to package inconsistent or invalid experiment results")
    batches = [("pilot", HERE, ROOT / "validation" / "paired-v2-2026-11-10"),
               ("extension", EXTENSION, ROOT / "validation" / "paired-v2-2026-11-10-extension")]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as evidence:
        for batch, experiment, output in batches:
            manifest = json.loads((experiment / "manifest.json").read_text(encoding="utf-8"))
            paths = ["fingerprints.json"]
            for case_id in manifest["case_ids"]:
                for arm in manifest["arms"]:
                    pair_paths = (f"{arm}/{case_id}/run.json", f"{arm}/{case_id}/diagnose/case_state.json")
                    present = [(output / name).exists() for name in pair_paths]
                    if all(present):
                        paths.extend(pair_paths)
                    elif any(present):
                        raise ValueError(f"incomplete local run: {batch}/{arm}/{case_id}")
            for path in sorted(paths):
                info = zipfile.ZipInfo(f"{batch}/{path}", (2026, 11, 10, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                evidence.writestr(info, (output / path).read_bytes())
    data = buffer.getvalue()
    if ARCHIVE.exists() and ARCHIVE.read_bytes() != data:
        raise ValueError(f"refusing to overwrite a different archive: {ARCHIVE}")
    if not ARCHIVE.exists():
        ARCHIVE.write_bytes(data)
    print(f"packaged {len(summary['state_hashes'])} verified states; bytes={len(data)}; sha256={hashlib.sha256(data).hexdigest()}")


if __name__ == "__main__":
    main()
