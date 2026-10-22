"""
Model Manifest schema and allowlist validation for ComfyUI remote model storage.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Set

# Standard ComfyUI model categories and their canonical destination folders
CANONICAL_CATEGORY_DIRS: Dict[str, str] = {
    "checkpoints": "models/checkpoints",
    "loras": "models/loras",
    "vae": "models/vae",
    "diffusion_models": "models/diffusion_models",
    "text_encoders": "models/text_encoders",
    "clip": "models/clip",
    "unet": "models/unet",
    "controlnet": "models/controlnet",
    "upscale_models": "models/upscale_models",
    "embeddings": "models/embeddings",
    "clip_vision": "models/clip_vision",
    "style_models": "models/style_models",
    "photomaker": "models/photomaker",
}

SAFE_FILENAME_PATTERN = re.compile(r"^[a-zA-Z0-9_\-\.][a-zA-Z0-9_\-\. ]*\.(safetensors|ckpt|pt|bin|pth|sft|onnx|yaml|json|txt)$", re.IGNORECASE)
SAFE_DRIVE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_\-]{15,100}$")


class ManifestValidationError(ValueError):
    """Raised when model manifest entries fail security or schema validation."""
    pass


@dataclass(frozen=True)
class ModelEntry:
    """Represents a validated, allowlisted model in Google Drive."""
    model_id: str
    gdrive_file_id: str
    category: str
    dest_rel_path: str
    filename: str
    expected_sha256: Optional[str] = None
    file_size: Optional[int] = None
    description: str = ""

    def validate(self, allowed_categories: Optional[Set[str]] = None) -> None:
        """Validate safety and integrity of model entry fields."""
        if not self.model_id or not isinstance(self.model_id, str):
            raise ManifestValidationError("Model entry must have a non-empty string 'model_id'.")

        if not self.gdrive_file_id or not SAFE_DRIVE_ID_PATTERN.match(self.gdrive_file_id):
            raise ManifestValidationError(
                f"Invalid gdrive_file_id '{self.gdrive_file_id}' for model '{self.model_id}'. "
                "Must be a standard Google Drive file identifier (alphanumeric, dashes, underscores)."
            )

        categories = allowed_categories or set(CANONICAL_CATEGORY_DIRS.keys())
        if self.category not in categories:
            raise ManifestValidationError(
                f"Unknown model category '{self.category}' for model '{self.model_id}'. "
                f"Allowed categories: {sorted(categories)}"
            )

        # Path traversal and filename safety
        if not self.filename or "/" in self.filename or "\\" in self.filename or ".." in self.filename:
            raise ManifestValidationError(
                f"Invalid filename '{self.filename}' for model '{self.model_id}'. Must be a base filename without directory separators."
            )

        if not SAFE_FILENAME_PATTERN.match(self.filename):
            raise ManifestValidationError(
                f"Filename '{self.filename}' contains invalid characters or unsupported model extension. "
                "Supported: .safetensors, .ckpt, .pt, .bin, .pth, .sft, .onnx, .yaml, .json, .txt"
            )

        # Destination path validation
        norm_dest = os.path.normpath(self.dest_rel_path).replace("\\", "/")
        if norm_dest.startswith("/") or norm_dest.startswith("../") or "/../" in norm_dest or norm_dest == "..":
            raise ManifestValidationError(
                f"Path traversal detected in dest_rel_path '{self.dest_rel_path}' for model '{self.model_id}'."
            )

        # Checksum validation if provided
        if self.expected_sha256:
            if not isinstance(self.expected_sha256, str) or not re.match(r"^[a-fA-F0-9]{64}$", self.expected_sha256):
                raise ManifestValidationError(
                    f"expected_sha256 for model '{self.model_id}' must be a 64-character hex string."
                )

        # File size validation if provided
        if self.file_size is not None:
            if not isinstance(self.file_size, int) or self.file_size <= 0:
                raise ManifestValidationError(
                    f"file_size for model '{self.model_id}' must be a positive integer."
                )


class ModelManifest:
    """Allowlisted registry of model files mapped to Google Drive storage."""

    def __init__(self, entries: Optional[Dict[str, ModelEntry]] = None, custom_categories: Optional[Set[str]] = None):
        self._entries: Dict[str, ModelEntry] = entries or {}
        self._custom_categories: Set[str] = custom_categories or set()
        self._filename_index: Dict[str, ModelEntry] = {}
        for entry in self._entries.values():
            self._filename_index[entry.filename] = entry

    @classmethod
    def from_dict(cls, data: dict, custom_categories: Optional[Set[str]] = None) -> ModelManifest:
        """Construct and validate a ModelManifest from a dictionary."""
        if not isinstance(data, dict):
            raise ManifestValidationError("Manifest root must be a dictionary.")

        raw_models = data.get("models")
        if raw_models is None:
            raise ManifestValidationError("Manifest dictionary must contain a 'models' key.")

        entries: Dict[str, ModelEntry] = {}
        allowed_cats = set(CANONICAL_CATEGORY_DIRS.keys())
        if custom_categories:
            allowed_cats.update(custom_categories)

        # Support both list format [{"model_id": ...}] and dict format {"model_id": {...}}
        if isinstance(raw_models, list):
            model_items = [(item.get("model_id"), item) for item in raw_models if isinstance(item, dict)]
        elif isinstance(raw_models, dict):
            model_items = list(raw_models.items())
        else:
            raise ManifestValidationError("'models' must be a list or dictionary.")

        for mid, item in model_items:
            if not mid or not isinstance(item, dict):
                raise ManifestValidationError(f"Invalid model entry specification: {item}")

            cat = item.get("category", "")
            dest = item.get("dest_rel_path") or CANONICAL_CATEGORY_DIRS.get(cat, f"models/{cat}")
            filename = item.get("filename") or os.path.basename(mid)

            entry = ModelEntry(
                model_id=str(mid),
                gdrive_file_id=str(item.get("gdrive_file_id", "")),
                category=str(cat),
                dest_rel_path=str(dest),
                filename=str(filename),
                expected_sha256=item.get("expected_sha256") or item.get("sha256"),
                file_size=int(item["file_size"]) if item.get("file_size") is not None else None,
                description=str(item.get("description", "")),
            )
            entry.validate(allowed_categories=allowed_cats)

            if entry.model_id in entries:
                raise ManifestValidationError(f"Duplicate model_id '{entry.model_id}' in manifest.")

            entries[entry.model_id] = entry

        return cls(entries=entries, custom_categories=custom_categories)

    @classmethod
    def from_file(cls, filepath: str, custom_categories: Optional[Set[str]] = None) -> ModelManifest:
        """Load and validate manifest from a JSON file."""
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"Manifest file not found: {filepath}")

        with open(filepath, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError as e:
                raise ManifestValidationError(f"Malformed JSON in manifest file: {e}")

        return cls.from_dict(data, custom_categories=custom_categories)

    def get_by_id(self, model_id: str) -> Optional[ModelEntry]:
        """Look up an entry by model_id."""
        return self._entries.get(model_id)

    def get_by_filename(self, filename: str) -> Optional[ModelEntry]:
        """Look up an entry by filename."""
        return self._filename_index.get(filename)

    def contains(self, identifier: str) -> bool:
        """Check if manifest contains model by ID or filename."""
        return identifier in self._entries or identifier in self._filename_index

    def resolve(self, identifier: str) -> Optional[ModelEntry]:
        """Resolve model entry by model_id first, then filename."""
        return self.get_by_id(identifier) or self.get_by_filename(identifier)

    def list_entries(self) -> List[ModelEntry]:
        """Return list of all registered ModelEntry objects."""
        return list(self._entries.values())

    def to_public_dict(self) -> List[dict]:
        """Export safe public metadata (excluding private keys / sensitive internal IDs)."""
        return [
            {
                "model_id": e.model_id,
                "category": e.category,
                "filename": e.filename,
                "file_size": e.file_size,
                "description": e.description,
                "has_checksum": bool(e.expected_sha256),
            }
            for e in self._entries.values()
        ]
