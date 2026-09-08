"""Private offline encoder process. stdout is only bounded JSON protocol data."""

import contextlib
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys


def main():
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    os.environ["OMP_NUM_THREADS"] = "1"
    request = json.load(sys.stdin)
    path = Path(request["modelPath"])
    with contextlib.redirect_stdout(sys.stderr):
        digest = hashlib.sha256()
        for item in sorted(path.rglob("*")):
            if item.is_file() and item.suffix in {
                ".json",
                ".safetensors",
                ".bin",
                ".model",
                ".txt",
            }:
                digest.update(item.relative_to(path).as_posix().encode())
                with item.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
        if not request["probe"] and digest.hexdigest() != request["fingerprint"]:
            raise ValueError(
                "embedding weights changed; reindex with a new provider version"
            )
        model_type = importlib.import_module(
            "sentence_transformers"
        ).SentenceTransformer
        model = model_type(
            str(path), device="cpu", local_files_only=True, trust_remote_code=False
        )
        if request["probe"]:
            result = {
                "dimensions": model.get_sentence_embedding_dimension(),
                "fingerprint": digest.hexdigest(),
            }
        else:
            result = {
                "vectors": model.encode(
                    request["texts"],
                    batch_size=32,
                    show_progress_bar=False,
                    normalize_embeddings=True,
                ).tolist()
            }
    print(json.dumps(result, allow_nan=False))


if __name__ == "__main__":
    main()
