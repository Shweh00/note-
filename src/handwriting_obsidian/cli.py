from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
import shutil
import sys

from .config import load_config
from .ocr import create_ocr_engine
from .processor import process_batch, watch


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="handwriting-obsidian")
    subparsers = parser.add_subparsers(dest="command", required=True)

    batch_parser = subparsers.add_parser("batch", help="Process all new images once.")
    batch_parser.add_argument("--config", default="config.toml", help="Path to TOML config.")

    watch_parser = subparsers.add_parser("watch", help="Continuously poll for new images.")
    watch_parser.add_argument("--config", default="config.toml", help="Path to TOML config.")

    init_parser = subparsers.add_parser("init-config", help="Create a starter config file.")
    init_parser.add_argument("--output", default="config.toml", help="Where to write the config.")
    init_parser.add_argument("--force", action="store_true", help="Overwrite an existing file.")
    return parser


def run_batch(config_path: str) -> int:
    config = load_config(Path(config_path))
    ocr_engine = create_ocr_engine(config.ocr)
    results = process_batch(config, ocr_engine)
    processed = [result for result in results if not result.skipped]
    skipped = [result for result in results if result.skipped]
    for result in processed:
        print(f"processed {result.image_path} -> {result.note_path}")
    print(f"done: {len(processed)} processed, {len(skipped)} skipped")
    return 0


def run_watch(config_path: str) -> int:
    config = load_config(Path(config_path))
    ocr_engine = create_ocr_engine(config.ocr)
    watch(config, ocr_engine)
    return 0


def init_config(output: str, force: bool) -> int:
    target = Path(output)
    if target.exists() and not force:
        print(f"{target} already exists; use --force to overwrite", file=sys.stderr)
        return 2
    source = Path(__file__).resolve().parents[2] / "config.example.toml"
    if source.exists():
        shutil.copyfile(source, target)
    else:
        target.write_text(
            'input_dir = "incoming"\n'
            'output_dir = "vault/Handwriting Notes"\n'
            'assets_dir = "vault/Handwriting Notes/assets"\n'
            'state_path = ".handwriting-obsidian-state.json"\n'
            'tags = ["handwriting", "ocr"]\n'
            'watch_interval_seconds = 5\n\n'
            '[ocr]\nprovider = "mock"\nfallback_text = "TODO: replace with OCR text"\n',
            encoding="utf-8",
        )
    print(f"wrote {target}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "batch":
        return run_batch(args.config)
    if args.command == "watch":
        return run_watch(args.config)
    if args.command == "init-config":
        return init_config(args.output, args.force)
    raise AssertionError(f"unhandled command {args.command}")
