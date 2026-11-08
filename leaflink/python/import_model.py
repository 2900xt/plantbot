"""Import model data only, without executing any code from the archive."""
import argparse
import hashlib
import json
import re
from pathlib import Path
import zipfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("archive", type=Path)
parser.add_argument("--output", type=Path)
args = parser.parse_args()
hashes = {}
with zipfile.ZipFile(args.archive) as archive:
    cnn_configs = [name for name in archive.namelist() if re.fullmatch(r"leaf_cnn_epoch[1-9][0-9]*/config\.json", name)]
    if len(cnn_configs) > 1:
        raise ValueError("Archive contains multiple model configurations")
    if cnn_configs:
        epoch = re.fullmatch(r"leaf_cnn_epoch([1-9][0-9]*)/config\.json", cnn_configs[0]).group(1)
        prefix = f"leaf_cnn_epoch{epoch}/"
        expected_architecture = "leaf-cnn"
        names = ["config.json", "weights.pt", "classes.json", "history.json", "README.txt"]
        args.output = args.output or Path(f"../outputs/models/leaf-cnn-epoch{epoch}")
    else:
        prefix = "plant_resnet18_model/"
        expected_architecture = "resnet18"
        names = ["config.json", "resnet18_plant_fp16.pt", "metrics_test.json", "metrics_validation.json"]
        args.output = args.output or Path("../outputs/models/plant-resnet18")
    cfg = json.loads(archive.read(prefix + "config.json"))
    if cfg["architecture"] != expected_architecture or len(cfg["classes"]) != 38 or len(set(cfg["classes"])) != 38:
        raise ValueError("Invalid model architecture or class order")
    if expected_architecture == "leaf-cnn" and json.loads(archive.read(prefix + "classes.json")) != cfg["classes"]:
        raise ValueError("classes.json and config.json disagree")
    args.output.mkdir(parents=True, exist_ok=True)
    for name in names:
        entry = archive.getinfo(prefix + name)
        if entry.file_size > 100 * 1024 * 1024:
            raise ValueError("Oversized model file")
        data = archive.read(entry)
        (args.output / name).write_bytes(data)
        hashes[name] = hashlib.sha256(data).hexdigest()
provenance = {"source_archive": args.archive.name, "architecture":expected_architecture, "files_sha256": hashes, "imported_code": False}
(args.output / "provenance.json").write_text(json.dumps(provenance, indent=2))
print(json.dumps({"output": str(args.output), **provenance}, indent=2))
