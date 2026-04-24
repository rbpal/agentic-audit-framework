"""Command-line entrypoints for the agentic audit framework."""

from agentic_audit.cli.generate_gold import (
    generate_engagement_corpus,
    main,
    write_hash_manifest,
)

__all__ = ["generate_engagement_corpus", "main", "write_hash_manifest"]
