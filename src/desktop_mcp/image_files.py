"""Opt-in local image export for clients that do not forward MCP image blocks."""

from __future__ import annotations

from collections import deque
from dataclasses import replace
from pathlib import Path
import tempfile
import uuid

from desktop_mcp.contracts import Observation


class ImageFiles:
    """Own only the individual files this instance creates, never a caller path."""

    def __init__(self, max_files: int = 16) -> None:
        if isinstance(max_files, bool) or not isinstance(max_files, int) or max_files < 1:
            raise ValueError("max_files must be a positive integer.")
        self._max_files = max_files
        self._root: Path | None = None
        self._files: deque[Path] = deque()

    def export(self, observation: Observation) -> Observation:
        if observation.image is None:
            raise ValueError("Image export requires a full observation; omit since.")
        suffix = {"image/png": ".png", "image/jpeg": ".jpg"}.get(observation.mime_type)
        if suffix is None:
            raise ValueError("Only PNG and JPEG frame exports are supported.")
        if self._root is None:
            self._root = Path(tempfile.mkdtemp(prefix="desktop-mcp-frames-")).resolve()
        while len(self._files) >= self._max_files:
            oldest = self._files[0]
            oldest.unlink(missing_ok=True)
            self._files.popleft()
        path = self._root / f"{uuid.uuid4().hex}{suffix}"
        # Exclusive creation prevents accidental replacement of an existing file.
        with path.open("xb") as stream:
            self._files.append(path)
            stream.write(observation.image)
        metadata = dict(observation.metadata)
        metadata["image_path"] = str(path)
        metadata["image_path_lifetime"] = (
            f"Temporary; retained among the latest {self._max_files} exports until server exit."
        )
        return replace(observation, metadata=metadata)

    def close(self) -> None:
        while self._files:
            path = self._files[0]
            path.unlink(missing_ok=True)
            self._files.popleft()
        if self._root is not None:
            # Nonrecursive removal cannot erase an unexpected file placed in this folder.
            self._root.rmdir()
            self._root = None
