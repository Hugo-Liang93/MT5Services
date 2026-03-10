#!/usr/bin/env python3
"""Compatibility entrypoint for simple mode.

This script delegates to the unified launcher in app.py.
"""

from app import main


if __name__ == "__main__":
    main(mode_override="simple")
