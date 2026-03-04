import logging
import os
import sys


def setup_logging(level=logging.INFO):
    """Sets up basic logging configuration."""
    formatter = logging.Formatter("[%(asctime)s] %(levelname)s in %(module)s: %(message)s")

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    # Root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Avoid duplicate handlers if setup_logging is called multiple times
    if not root_logger.handlers:
        root_logger.addHandler(console_handler)

    return root_logger
