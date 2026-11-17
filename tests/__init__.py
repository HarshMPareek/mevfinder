"""Offline tests: every chain-facing case runs on recorded receipts and traces in fixtures/, no network."""

from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
