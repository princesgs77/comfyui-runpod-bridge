"""
Model Resolver for ComfyUI Workflows and Explicit Job Configuration.
Maps workflow model loader nodes and explicit prefetch lists to allowlisted
manifest entries without altering workflow semantics or executing untrusted paths.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Set, Tuple

from .manifest import CANONICAL_CATEGORY_DIRS, ModelEntry, ModelManifest

logger = logging.getLogger("worker.model_cache.model_resolver")

# Explicit registry mapping supported ComfyUI node types and their model fields to categories
MODEL_LOADER_REGISTRY: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    "CheckpointLoaderSimple": ("checkpoints", ("ckpt_name",)),
    "CheckpointLoader": ("checkpoints", ("ckpt_name",)),
    "LoraLoader": ("loras", ("lora_name",)),
    "LoraLoaderModelOnly": ("loras", ("lora_name",)),
    "VAELoader": ("vae", ("vae_name",)),
    "CLIPLoader": ("clip", ("clip_name",)),
    "DualCLIPLoader": ("text_encoders", ("clip_name1", "clip_name2")),
    "TripleCLIPLoader": ("text_encoders", ("clip_name1", "clip_name2", "clip_name3")),
    "UNETLoader": ("diffusion_models", ("unet_name",)),
    "UnetLoaderGGUF": ("diffusion_models", ("unet_name",)),
    "Hy3DModelLoader": ("diffusion_models", ("model",)),
    "UpscaleModelLoader": ("upscale_models", ("model_name",)),
    "ControlNetLoader": ("controlnet", ("control_net_name",)),
    "ControlNetLoaderAdvanced": ("controlnet", ("control_net_name",)),
    "DiffusersLoader": ("diffusion_models", ("model_path",)),
    "GLIGENLoader": ("gligen", ("gligen_name",)),
    "IPAdapterModelLoader": ("ipadapter", ("ipadapter_file",)),
}


class UnresolvedModelError(Exception):
    """Raised when one or more required models are neither cached on disk nor registered in the manifest."""
    pass


class ModelResolver:
    """Resolves models needed for a job against local cache and allowlisted manifest."""

    def __init__(self, manifest: ModelManifest, base_dir: str = "/comfyui"):
        self.manifest = manifest
        self.base_dir = os.path.abspath(base_dir)

    def is_file_on_disk(self, category: str, filename: str) -> bool:
        """Check if a model file is physically present in the standard ComfyUI directory."""
        rel_dir = CANONICAL_CATEGORY_DIRS.get(category, f"models/{category}")
        candidate = os.path.join(self.base_dir, rel_dir, filename)
        return os.path.isfile(candidate)

    def resolve_job_models(
        self,
        workflow: Optional[Dict[str, Any]] = None,
        explicit_models: Optional[List[str]] = None,
        prefetch_models: Optional[List[str]] = None,
    ) -> List[ModelEntry]:
        """
        Determine the exact list of ModelEntry objects to download.
        Only downloads models that are missing from disk.
        Fails fast if any model is unknown/unallowlisted.
        """
        to_download: Dict[str, ModelEntry] = {}
        unresolved: List[Dict[str, Any]] = []

        # 1. Process explicit prefetch models
        all_explicit = []
        if explicit_models:
            all_explicit.extend(explicit_models)
        if prefetch_models:
            all_explicit.extend(prefetch_models)

        for identifier in all_explicit:
            if not identifier or not isinstance(identifier, str):
                continue
            entry = self.manifest.resolve(identifier)
            if not entry:
                unresolved.append({
                    "source": "explicit_request",
                    "identifier": identifier,
                    "reason": "Identifier not found in allowlisted model manifest",
                })
                continue

            if not self.is_file_on_disk(entry.category, entry.filename):
                to_download[entry.model_id] = entry

        # 2. Inspect workflow nodes if workflow dictionary is provided
        if workflow and isinstance(workflow, dict):
            for node_id, node in workflow.items():
                if not isinstance(node, dict):
                    continue
                class_type = node.get("class_type")
                inputs = node.get("inputs")
                if not class_type or not isinstance(inputs, dict):
                    continue

                if class_type in MODEL_LOADER_REGISTRY:
                    category, fields = MODEL_LOADER_REGISTRY[class_type]
                    for field in fields:
                        val = inputs.get(field)
                        # Skip link references (e.g. ["12", 0])
                        if not val or not isinstance(val, str):
                            continue

                        # Check if file is already on disk
                        if self.is_file_on_disk(category, val):
                            logger.debug("Model '%s' for node '%s' is already on disk.", val, node_id)
                            continue

                        # Search manifest by filename or model_id
                        entry = self.manifest.resolve(val)
                        if entry:
                            to_download[entry.model_id] = entry
                        else:
                            unresolved.append({
                                "source": f"node_{node_id}_{class_type}",
                                "field": field,
                                "filename": val,
                                "category": category,
                                "reason": f"File '{val}' is not on worker disk and not registered in model_manifest.json",
                            })

        # 3. Fail with an actionable error if any model is unresolved
        if unresolved:
            error_msg = (
                "ComfyUI Model Resolution Failed: The workflow or job references model(s) that are neither "
                "cached on the worker nor allowlisted in Google Drive model_manifest.json:\n\n"
            )
            for item in unresolved:
                if item["source"] == "explicit_request":
                    error_msg += f"  • Explicit model '{item['identifier']}': {item['reason']}\n"
                else:
                    error_msg += f"  • Node '{item['source']}' requires '{item['filename']}' ({item['category']}): {item['reason']}\n"

            available_models = [e.model_id for e in self.manifest.list_entries()]
            error_msg += (
                f"\nAllowlisted manifest entries ({len(available_models)}): {available_models}\n\n"
                "Action needed: Add the required model(s) to model_manifest.json with their Google Drive file ID, "
                "or update the workflow to use an allowlisted model."
            )
            logger.error(error_msg)
            raise UnresolvedModelError(error_msg)

        return list(to_download.values())
