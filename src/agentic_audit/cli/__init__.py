"""Command-line entrypoints for the agentic audit framework."""

from agentic_audit.cli.generate_gold import generate_gold, main, write_hash_manifest

__all__ = ["generate_gold", "main", "write_hash_manifest"]
