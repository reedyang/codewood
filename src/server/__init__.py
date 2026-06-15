"""Headless serve mode for the desktop GUI.

This package exposes the existing terminal Agent over a localhost
HTTP + Server-Sent-Events (SSE) API so a separate GUI process can
drive it without re-implementing any command logic.
"""

from .serve_app import ServeApp

__all__ = ["ServeApp"]
