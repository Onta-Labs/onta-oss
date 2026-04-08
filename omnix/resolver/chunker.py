"""Content chunking utilities for large text, JSON, and CSV data."""

from __future__ import annotations

import json
import re


def chunk_text(content: str, max_chars: int = 3000, overlap: int = 200) -> list[str]:
    """Split text into chunks on sentence boundaries with overlap.

    Args:
        content: Raw text to split.
        max_chars: Maximum characters per chunk.
        overlap: Characters of overlap between chunks for context continuity.

    Returns:
        List of text chunks. Returns [content] if it fits in one chunk.
    """
    if len(content) <= max_chars:
        return [content]

    # Split into sentences
    sentences = re.split(r'(?<=[.!?])\s+', content)
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for sentence in sentences:
        sentence_len = len(sentence)

        if current_len + sentence_len > max_chars and current:
            # Emit current chunk
            chunks.append(" ".join(current))
            # Start new chunk with overlap from the end of the previous
            overlap_text = " ".join(current)
            if len(overlap_text) > overlap:
                overlap_text = overlap_text[-overlap:]
            current = [overlap_text]
            current_len = len(overlap_text)

        current.append(sentence)
        current_len += sentence_len + 1  # +1 for space

    if current:
        chunks.append(" ".join(current))

    return chunks if chunks else [content]


def chunk_json_array(content: str, batch_size: int = 50) -> list[str]:
    """Split a JSON array into batches of objects.

    If the root is not an array, returns the content as a single chunk.

    Args:
        content: JSON string.
        batch_size: Number of objects per chunk.

    Returns:
        List of JSON string chunks.
    """
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return [content]

    if not isinstance(data, list):
        return [content]

    if len(data) <= batch_size:
        return [content]

    chunks: list[str] = []
    for i in range(0, len(data), batch_size):
        batch = data[i : i + batch_size]
        chunks.append(json.dumps(batch, default=str))

    return chunks
