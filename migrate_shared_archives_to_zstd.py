"""Migrate legacy GZIP Parquet shared-market archives to ZSTD level 3.

The destination is written to a temporary file, read back, and checked for
matching row/column counts before an atomic rename. Legacy files are retained
unless ``--delete-source`` is explicitly supplied.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from config.labs_config import SHARED_ARCHIVE_DIR


def migrate_file(source: Path, *, delete_source: bool = False) -> tuple[int, int]:
    destination = source.with_name(source.name.removesuffix(".parquet.gz") + ".parquet.zst")
    source_size = source.stat().st_size

    frame = pd.read_parquet(source)
    if destination.exists():
        verified = pd.read_parquet(destination)
    else:
        temporary = destination.with_name(destination.name + ".tmp")
        frame.to_parquet(
            temporary,
            compression={"type": "zstd", "args": {"level": 3}},
            index=False,
        )
        verified = pd.read_parquet(temporary)
        if len(verified) != len(frame) or list(verified.columns) != list(frame.columns):
            temporary.unlink(missing_ok=True)
            raise RuntimeError(
                f"Verification failed for {source}: "
                f"source={frame.shape}, destination={verified.shape}"
            )
        temporary.replace(destination)

    if len(verified) != len(frame) or list(verified.columns) != list(frame.columns):
        raise RuntimeError(f"Existing ZSTD archive does not match {source}")
    if delete_source:
        source.unlink()
    return source_size, destination.stat().st_size


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=SHARED_ARCHIVE_DIR)
    parser.add_argument("--delete-source", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    sources = sorted(args.root.glob("**/*_options_1min.parquet.gz"))
    if args.dry_run:
        for source in sources:
            print(source)
        print(f"{len(sources)} legacy archive(s) found")
        return

    before = after = 0
    for source in sources:
        old_size, new_size = migrate_file(
            source, delete_source=args.delete_source
        )
        before += old_size
        after += new_size
        print(
            f"{source.name}: {old_size / 1024 / 1024:.2f} MiB -> "
            f"{new_size / 1024 / 1024:.2f} MiB"
        )

    print(
        f"Migrated {len(sources)} archive(s): "
        f"{before / 1024 / 1024:.2f} MiB -> {after / 1024 / 1024:.2f} MiB"
    )


if __name__ == "__main__":
    main()
