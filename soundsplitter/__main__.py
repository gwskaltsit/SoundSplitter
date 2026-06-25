"""Run the desktop app: ``python -m soundsplitter``."""

from __future__ import annotations

import logging

import flet as ft

from .config.settings import default_settings_path
from .ui.app import build


def _setup_logging() -> None:
    log_path = default_settings_path().parent / "soundsplitter.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(log_path, encoding="utf-8")],
    )
    logging.getLogger("soundsplitter").info("logging to %s", log_path)


def main() -> None:
    _setup_logging()
    ft.run(build)


if __name__ == "__main__":
    main()
