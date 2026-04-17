"""
Live Blender addon implementation for Nymphs.
"""

bl_info = {
    "name": "Nymphs",
    "author": "Nymphs3D",
    "version": (1, 1, 141),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > Nymphs",
    "description": "Blender client for NymphsCore image, shape, and texture backends",
    "category": "3D View",
}

import base64
import atexit
import json
import mimetypes
import ntpath
import os
import queue
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import textwrap
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)


LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
EVENT_QUEUE = queue.Queue()
PROCESS_LOCK = threading.Lock()
CACHE_LOCK = threading.Lock()
MANAGED_BACKENDS = {}
MANAGED_BACKEND_TARGETS = {}
TRANSIENT_CACHE = {}
ATEXIT_REGISTERED = False
STAGE_LINE = re.compile(r"STAGE:\s*([A-Za-z0-9_]+)")
PROGRESS_LINE = re.compile(r"PROGRESS:\s*([A-Za-z0-9_ -]+)\s+(\d+)/(\d+)")
GPU_REFRESH_SECONDS = 5.0
SERVER_POLL_SECONDS = 8.0
SERVER_POLL_FAST_SECONDS = 2.0
ACTIVE_POLL_IDLE_SECONDS = 12.0
ACTIVE_POLL_BUSY_SECONDS = 2.0
ACTIVE_POLL_UNAVAILABLE_SECONDS = 20.0
ACTIVE_POLL_DIRECT_JOB_SECONDS = 1.0
UI_REFRESH_ACTIVE_SECONDS = 1.0
UI_REFRESH_IDLE_SECONDS = 5.0
PRESET_CACHE_TTL_SECONDS = 1.0
WSL_DISTRO_CACHE_TTL_SECONDS = 15.0
LOG_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[^\|]*\|\s*[A-Z]+\s*\|\s*(?:stdout|stderr|[^|]+)\s*\|\s*")
LOCAL_SHAPE_OUTPUT_DIRNAME = "nymphs_shape_outputs"
LOCAL_IMAGEGEN_OUTPUT_DIRNAME = "nymphs_image_outputs"
CACHE_MISS = object()
PART_GUIDANCE_SYNC_GUARD = False
IMAGEGEN_PROMPT_SYNC_GUARD = False

DEFAULT_WSL_DISTRO = "NymphsCore"
DEFAULT_WSL_USER = "nymph"
DEFAULT_REPO_2MV_PATH = "~/Hunyuan3D-2"
DEFAULT_REPO_N2D2_PATH = "~/Z-Image"
DEFAULT_REPO_TRELLIS_PATH = "~/TRELLIS.2"
DEFAULT_2MV_PYTHON_PATH = "~/Hunyuan3D-2/.venv/bin/python"
DEFAULT_N2D2_PYTHON_PATH = "~/Z-Image/.venv-nunchaku/bin/python"
DEFAULT_TRELLIS_PYTHON_PATH = "~/TRELLIS.2/.venv/bin/python"
DEFAULT_N2D2_MODEL_ID = "Tongyi-MAI/Z-Image-Turbo"
DEFAULT_N2D2_MODEL_VARIANT = ""
DEFAULT_N2D2_NUNCHAKU_RANK = "32"
DEFAULT_N2D2_MODEL_PRESET = "zimage_nunchaku_r32"
OPENROUTER_API_ROOT = "https://openrouter.ai/api/v1"
GEMINI_MODEL_IDS = {
    "gemini_2_5_flash_image": "google/gemini-2.5-flash-image",
    "gemini_3_1_flash_image_preview": "google/gemini-3.1-flash-image-preview",
    "gemini_3_pro_image_preview": "google/gemini-3-pro-image-preview",
}
GEMINI_PLANNER_MODEL_IDS = {
    "gemini_2_5_flash": "google/gemini-2.5-flash",
    "gemini_3_1_pro_preview": "google/gemini-3.1-pro-preview",
}
DEFAULT_PART_EXTRACTION_GUIDANCE = (
    "Preserve the exact illustration style, proportions, color palette, material details, and scale relationship from the reference image. "
    "Preserve the character's inferred body type and silhouette proportions from the master image, including short, squat, stout, bulky, chibi, or stylized proportions when present. "
    "Do not replace the character with a generic slim adult anatomy template. "
    "Match the same line weight, brush texture, paper texture, color softness, rendering flatness, and level of detail. "
    "Use the same media feel as the source image on a simple plain background. "
    "Do not convert the result into a 3D render, product render, glossy material render, realistic render, toy render, or studio product photo. "
    "Do not include unrelated body parts, heads, mannequins, labels, extra objects, scenery, or shadows."
)
GEMINI_IMAGE_SIZE_MODELS = {
    "google/gemini-3.1-flash-image-preview",
    "google/gemini-3-pro-image-preview",
}
DEFAULT_IMAGEGEN_PROMPT_PRESET = "clean_asset_concept"
DEFAULT_IMAGEGEN_PROMPT_TEXT_NAME = "Nymphs Image Prompt"
DEFAULT_PART_EXTRACTION_GUIDANCE_TEXT_NAME = "Nymphs Part Extraction Guidance"
PACKAGED_IMAGEGEN_PROMPT_PRESET_DIR = "prompt_presets"
DEFAULT_IMAGEGEN_STYLE_PRESET = "__none__"
PACKAGED_IMAGEGEN_STYLE_PRESET_DIR = "style_presets"
PROMPT_KIND_SUBJECT = "subject"
PROMPT_KIND_STYLE = "style"
PROMPT_KIND_SAVED = "saved"
PROMPT_KIND_LABELS = {
    PROMPT_KIND_SUBJECT: "Subject",
    PROMPT_KIND_STYLE: "Style",
    PROMPT_KIND_SAVED: "Saved Prompt",
}
NO_PROMPT_PRESET_KEY = "__none__"
WSL_DISTRO_ITEMS = []
N2D2_PRESET_SYNC_GUARD = False
N2D2_AUTORESTART_GUARD = False
IMAGEGEN_MV_VIEW_SPECS = (
    ("front", "Front", "front view"),
    ("left", "Left", "left side view"),
    ("right", "Right", "right side view"),
    ("back", "Back", "back view"),
)
IMAGEGEN_PROMPT_PRESETS = {
    "clean_asset_concept": {
        "label": "Clean Asset Reference",
        "description": "General-purpose asset prompt for props, creatures, buildings, and objects",
        "prompt": (
            "single game asset reference, centered subject, isolated on a plain light background, "
            "clean readable silhouette, whole subject visible, consistent design, soft even lighting, "
            "minimal shadows, no scenery, no text, clear shape language, "
            "designed for 3D modeling reference"
        ),
    },
    "stylized_prop": {
        "label": "Prop Asset",
        "description": "Readable prop/object prompt for game assets",
        "prompt": (
            "single game prop asset, centered, isolated on a plain light background, clean silhouette, "
            "readable materials, simple appealing shapes, soft even lighting, "
            "clear front-facing design, no scenery, no text"
        ),
    },
    "character_asset": {
        "label": "Character Asset",
        "description": "Readable full-character prompt for image-to-3D workflows",
        "prompt": (
            "single original character reference, full body, centered, head to toe visible, clean silhouette, "
            "simple plain light background, soft even lighting, readable costume design, clear limb separation, "
            "appealing proportions, no scenery, no props crossing the body, designed for 3D modeling reference"
        ),
    },
    "character_master_reference": {
        "label": "Character Master Reference",
        "description": "Full-character master image for guided part extraction",
        "prompt": (
            "Create one complete master character reference image from the character description below. "
            "Show the full character as one cohesive design, centered, head to toe visible, front-readable, "
            "with the complete outfit, hair, facial hair, weapons, accessories, and carried props included together. "
            "Use a clean plain light background, soft even lighting, readable silhouette, clear materials, and enough space around the character. "
            "This image will be used as the canonical source for later guided part extraction, so keep the design consistent and complete. "
            "Critical exclusions: do not make a parts sheet, grid, collage, lineup, turnaround, catalog page, or separate item layout. "
            "Do not split the clothing, hair, weapons, or props into separate images. "
            "Do not add labels, text, scenery, extra characters, duplicate versions, or extra floating items. "
            "Character description: "
        ),
    },
    "character_part_breakout": {
        "label": "Character Part Breakout",
        "description": "Generates separate anatomy base, hair, clothing, weapon, and prop reference images from a character description",
        "prompt": (
            "Create separate standalone asset reference images from the character description below. "
            "Each generated image must contain exactly one thing from the character design. "
            "One image should be the complete neutral anatomy base body for a game asset in a clean A-pose or T-pose, "
            "centered, head-to-toe visible, with simplified non-explicit anatomy suitable for base mesh modeling reference, "
            "with no clothing, armor, accessories, weapons, hair, censor bars, black bars, blur, stickers, or coverings. "
            "Hair must be generated as its own separate standalone image, never attached to the anatomy base body. "
            "Every other generated image should be exactly one isolated character part: one hairstyle or hair asset, "
            "one clothing garment, one armor piece, one accessory, one weapon, or one carried object from the same character design. "
            "Return a full breakout set as multiple separate images in one request, generating as many images as needed "
            "to cover the anatomy base body, hair, and each major wearable or carried item from the design. "
            "Start with the anatomy base body, then hair, then remaining items. "
            "For the anatomy base body, keep it neutral, front-readable, uncluttered, and appropriate as a base mesh game asset reference. "
            "For hair, clothing, armor, accessories, weapons, and props, show only the item itself, centered, complete, "
            "unobstructed, and not worn by a person or mannequin. "
            "Use a plain light background, soft even studio lighting, clean readable silhouette, clear materials, "
            "and asset-reference framing suitable for 3D modeling. "
            "Critical exclusions: do not make a parts sheet, lineup, grid, collage, catalog page, or multi-item layout. "
            "Do not combine the anatomy base body with hair, clothing, or props in the same image. "
            "Never place more than one item in the same image. "
            "No duplicate items, no text, no labels, no scenery, and no mannequin unless structurally unavoidable. "
            "Character description: "
        ),
    },
    "creature_asset": {
        "label": "Creature Asset",
        "description": "Creature/monster prompt with a readable whole-body shape",
        "prompt": (
            "single creature reference, whole creature visible, centered, isolated on a plain light background, "
            "clean readable silhouette, clear anatomy, distinctive shape language, soft even lighting, minimal shadows, "
            "designed as a 3D creature reference, no scenery, no text"
        ),
    },
    "building_asset": {
        "label": "Building Asset",
        "description": "Isolated building or environment-piece prompt",
        "prompt": (
            "single building asset, centered, isolated on a plain light background, full structure visible, "
            "clean silhouette, readable roof and wall shapes, clear material zones, soft even lighting, "
            "designed for 3D modeling reference, no surrounding scene, no text"
        ),
    },
    "hard_surface_asset": {
        "label": "Hard Surface Asset",
        "description": "Vehicle, machine, robot, or sci-fi object prompt",
        "prompt": (
            "single hard-surface asset reference, centered, isolated on a plain light background, whole object visible, "
            "clean silhouette, readable mechanical forms, consistent proportions, clear panel lines and material breaks, "
            "soft studio lighting, designed for 3D modeling reference, no scenery, no text"
        ),
    },
}


def _packaged_imagegen_prompt_preset_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), PACKAGED_IMAGEGEN_PROMPT_PRESET_DIR)


def _load_packaged_imagegen_prompt_presets():
    preset_dir = _packaged_imagegen_prompt_preset_dir()
    presets = {}
    if not os.path.isdir(preset_dir):
        return presets
    for filename in sorted(os.listdir(preset_dir)):
        if not filename.lower().endswith(".json"):
            continue
        key = os.path.splitext(filename)[0]
        path = os.path.join(preset_dir, filename)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            continue
        label = str(data.get("name") or data.get("label") or key.replace("_", " ").title()).strip()
        description = str(data.get("description") or f"Packaged prompt preset: {filename}").strip()
        prompt = str(data.get("prompt") or "").strip()
        if not prompt:
            continue
        presets[key] = {
            "label": label,
            "description": description,
            "prompt": prompt,
        }
    return presets


IMAGEGEN_PROMPT_PRESETS.update(_load_packaged_imagegen_prompt_presets())
IMAGEGEN_STYLE_PRESETS = {
    "painterly_fantasy": {
        "label": "Painterly Fantasy",
        "description": "Painterly fantasy concept art with rich costume detail",
        "style": (
            "painterly fantasy concept art, elegant linework, soft watercolor shading, readable silhouette, "
            "ornate costume detail, handcrafted illustration finish"
        ),
    },
    "clean_anime": {
        "label": "Clean Anime",
        "description": "Polished anime key art with crisp cel shading",
        "style": (
            "clean anime illustration, crisp linework, cel-shaded rendering, polished key art finish, "
            "controlled highlights, appealing stylized proportions"
        ),
    },
    "grimdark_realism": {
        "label": "Grimdark Realism",
        "description": "Grounded dark-fantasy realism with worn materials",
        "style": (
            "grimdark fantasy realism, grounded material rendering, worn surfaces, moody natural palette, "
            "cinematic concept art finish"
        ),
    },
    "storybook_inkwash": {
        "label": "Storybook Inkwash",
        "description": "Strong ink-and-wash storybook illustration style",
        "style": (
            "strong storybook ink-and-wash illustration style, visible black ink contour lines, loose brush ink details, "
            "transparent watercolor washes, paper texture, gentle muted fantasy palette, hand-painted traditional media finish, "
            "not 3D render, not plastic, not glossy, not photorealistic"
        ),
    },
    "japanese_watercolor_woodblock": {
        "label": "Japanese Watercolor Woodblock",
        "description": "Ukiyo-e inspired watercolor and woodblock print styling",
        "style": (
            "Japanese watercolor woodblock print style, ukiyo-e inspired composition, elegant carved ink outlines, "
            "flat layered color washes, subtle washi paper texture, restrained natural palette, decorative but readable silhouette, "
            "traditional printmaking feel, not 3D render, not glossy, not photorealistic"
        ),
    },
    "minimalist_chinese_watercolor": {
        "label": "Minimalist Chinese Watercolor",
        "description": "Sparse Chinese ink-wash watercolor styling",
        "style": (
            "minimalist Chinese watercolor and ink-wash painting style, sparse expressive brushwork, soft mineral watercolor washes, "
            "generous negative space, delicate ink contours, calm restrained palette, xieyi-inspired simplicity, rice paper texture, "
            "not 3D render, not glossy, not photorealistic"
        ),
    },
}


def _packaged_imagegen_style_preset_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), PACKAGED_IMAGEGEN_STYLE_PRESET_DIR)


def _load_packaged_imagegen_style_presets():
    preset_dir = _packaged_imagegen_style_preset_dir()
    presets = {}
    if not os.path.isdir(preset_dir):
        return presets
    for filename in sorted(os.listdir(preset_dir)):
        if not filename.lower().endswith(".json"):
            continue
        key = os.path.splitext(filename)[0]
        path = os.path.join(preset_dir, filename)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            continue
        label = str(data.get("name") or data.get("label") or key.replace("_", " ").title()).strip()
        description = str(data.get("description") or f"Packaged style preset: {filename}").strip()
        style = str(data.get("style") or data.get("prompt") or "").strip()
        if not style:
            continue
        presets[key] = {
            "label": label,
            "description": description,
            "style": style,
        }
    return presets


IMAGEGEN_STYLE_PRESETS.update(_load_packaged_imagegen_style_presets())
IMAGEGEN_PROMPT_PRESET_ITEMS = tuple(
    (key, data["label"], data["description"]) for key, data in IMAGEGEN_PROMPT_PRESETS.items()
)
DEFAULT_IMAGEGEN_SETTINGS_PRESET = "turbo_default"
IMAGEGEN_SETTINGS_PRESETS = {
    "turbo_fast_draft": {
        "label": "Fast Draft",
        "description": "Fastest Z-Image Turbo profile for quick iteration. Uses Nunchaku r32.",
        "values": {
            "n2d2_model_preset": "zimage_nunchaku_r32",
            "imagegen_width": 1024,
            "imagegen_height": 1024,
            "imagegen_steps": 9,
            "imagegen_guidance_scale": 0.0,
            "imagegen_variant_count": 1,
            "imagegen_seed_step": 1,
        },
    },
    "turbo_default": {
        "label": "Default",
        "description": "Balanced Z-Image Turbo profile. Uses Nunchaku r128.",
        "values": {
            "n2d2_model_preset": "zimage_nunchaku_r128",
            "imagegen_width": 1024,
            "imagegen_height": 1024,
            "imagegen_steps": 9,
            "imagegen_guidance_scale": 0.0,
            "imagegen_variant_count": 1,
            "imagegen_seed_step": 1,
        },
    },
    "turbo_higher_detail": {
        "label": "High Quality",
        "description": "Higher-resolution Z-Image Turbo profile. Uses Nunchaku r256.",
        "values": {
            "n2d2_model_preset": "zimage_nunchaku_r256",
            "imagegen_width": 1536,
            "imagegen_height": 1536,
            "imagegen_steps": 9,
            "imagegen_guidance_scale": 0.0,
            "imagegen_variant_count": 1,
            "imagegen_seed_step": 1,
        },
    },
    "turbo_character_sheet": {
        "label": "Tall Character",
        "description": "Portrait-friendly Z-Image Turbo profile for full-body or sheet-like references. Uses Nunchaku r128.",
        "values": {
            "n2d2_model_preset": "zimage_nunchaku_r128",
            "imagegen_width": 1024,
            "imagegen_height": 1536,
            "imagegen_steps": 9,
            "imagegen_guidance_scale": 0.0,
            "imagegen_variant_count": 1,
            "imagegen_seed_step": 1,
        },
    },
}
LEGACY_IMAGEGEN_SETTINGS_PRESET_KEYS = {"turbo_mv_source"}
DEFAULT_TRELLIS_SHAPE_PRESET = "official_default_1024_cascade"
TRELLIS_SHAPE_PRESETS = {
    "official_fast_512": {
        "label": "Official Fast 512",
        "description": "Fastest shipped TRELLIS lane using the upstream sampler defaults.",
        "values": {
            "trellis_pipeline_type": "512",
            "trellis_max_tokens": 49152,
            "trellis_texture_size": "2048",
            "trellis_decimation_target": 500000,
            "trellis_ss_sampling_steps": 12,
            "trellis_ss_guidance_strength": 7.5,
            "trellis_ss_guidance_rescale": 0.7,
            "trellis_ss_guidance_interval_start": 0.6,
            "trellis_ss_guidance_interval_end": 1.0,
            "trellis_ss_rescale_t": 5.0,
            "trellis_shape_sampling_steps": 12,
            "trellis_shape_guidance_strength": 7.5,
            "trellis_shape_guidance_rescale": 0.5,
            "trellis_shape_guidance_interval_start": 0.6,
            "trellis_shape_guidance_interval_end": 1.0,
            "trellis_shape_rescale_t": 3.0,
            "trellis_tex_sampling_steps": 12,
            "trellis_tex_guidance_strength": 1.0,
            "trellis_tex_guidance_rescale": 0.0,
            "trellis_tex_guidance_interval_start": 0.6,
            "trellis_tex_guidance_interval_end": 0.9,
            "trellis_tex_rescale_t": 3.0,
        },
    },
    "official_direct_1024": {
        "label": "Official Direct 1024",
        "description": "Single-stage 1024 lane using the upstream sampler defaults.",
        "values": {
            "trellis_pipeline_type": "1024",
            "trellis_max_tokens": 49152,
            "trellis_texture_size": "2048",
            "trellis_decimation_target": 500000,
            "trellis_ss_sampling_steps": 12,
            "trellis_ss_guidance_strength": 7.5,
            "trellis_ss_guidance_rescale": 0.7,
            "trellis_ss_guidance_interval_start": 0.6,
            "trellis_ss_guidance_interval_end": 1.0,
            "trellis_ss_rescale_t": 5.0,
            "trellis_shape_sampling_steps": 12,
            "trellis_shape_guidance_strength": 7.5,
            "trellis_shape_guidance_rescale": 0.5,
            "trellis_shape_guidance_interval_start": 0.6,
            "trellis_shape_guidance_interval_end": 1.0,
            "trellis_shape_rescale_t": 3.0,
            "trellis_tex_sampling_steps": 12,
            "trellis_tex_guidance_strength": 1.0,
            "trellis_tex_guidance_rescale": 0.0,
            "trellis_tex_guidance_interval_start": 0.6,
            "trellis_tex_guidance_interval_end": 0.9,
            "trellis_tex_rescale_t": 3.0,
        },
    },
    "official_default_1024_cascade": {
        "label": "Official Default 1024 Cascade",
        "description": "Matches the upstream default pipeline type and shipped sampler defaults.",
        "values": {
            "trellis_pipeline_type": "1024_cascade",
            "trellis_max_tokens": 49152,
            "trellis_texture_size": "2048",
            "trellis_decimation_target": 500000,
            "trellis_ss_sampling_steps": 12,
            "trellis_ss_guidance_strength": 7.5,
            "trellis_ss_guidance_rescale": 0.7,
            "trellis_ss_guidance_interval_start": 0.6,
            "trellis_ss_guidance_interval_end": 1.0,
            "trellis_ss_rescale_t": 5.0,
            "trellis_shape_sampling_steps": 12,
            "trellis_shape_guidance_strength": 7.5,
            "trellis_shape_guidance_rescale": 0.5,
            "trellis_shape_guidance_interval_start": 0.6,
            "trellis_shape_guidance_interval_end": 1.0,
            "trellis_shape_rescale_t": 3.0,
            "trellis_tex_sampling_steps": 12,
            "trellis_tex_guidance_strength": 1.0,
            "trellis_tex_guidance_rescale": 0.0,
            "trellis_tex_guidance_interval_start": 0.6,
            "trellis_tex_guidance_interval_end": 0.9,
            "trellis_tex_rescale_t": 3.0,
        },
    },
    "experimental_1536_cascade": {
        "label": "Experimental 1536 Cascade",
        "description": "Higher-detail experimental cascade path with a larger token budget.",
        "values": {
            "trellis_pipeline_type": "1536_cascade",
            "trellis_max_tokens": 98304,
            "trellis_texture_size": "2048",
            "trellis_decimation_target": 750000,
            "trellis_ss_sampling_steps": 12,
            "trellis_ss_guidance_strength": 7.5,
            "trellis_ss_guidance_rescale": 0.7,
            "trellis_ss_guidance_interval_start": 0.6,
            "trellis_ss_guidance_interval_end": 1.0,
            "trellis_ss_rescale_t": 5.0,
            "trellis_shape_sampling_steps": 12,
            "trellis_shape_guidance_strength": 7.5,
            "trellis_shape_guidance_rescale": 0.5,
            "trellis_shape_guidance_interval_start": 0.6,
            "trellis_shape_guidance_interval_end": 1.0,
            "trellis_shape_rescale_t": 3.0,
            "trellis_tex_sampling_steps": 12,
            "trellis_tex_guidance_strength": 1.0,
            "trellis_tex_guidance_rescale": 0.0,
            "trellis_tex_guidance_interval_start": 0.6,
            "trellis_tex_guidance_interval_end": 0.9,
            "trellis_tex_rescale_t": 3.0,
        },
    },
}
SERVICE_LABELS = {
    "2mv": "Hunyuan 2mv",
    "n2d2": "Z-Image",
    "trellis": "TRELLIS.2",
}
SERVICE_PROP_PREFIXES = {
    "2mv": "service_2mv",
    "n2d2": "service_n2d2",
    "trellis": "service_trellis",
}
SERVICE_ORDER = ("n2d2", "trellis", "2mv")


@dataclass
class ImportEvent:
    scene_name: str
    mesh_path: str
    source_object_name: str = ""
    hide_source: bool = True
    update_shape_result: bool = True


def _active_state(scene_name):
    scene = bpy.data.scenes.get(scene_name)
    if scene is None:
        return None
    return getattr(scene, "nymphs_state", None)


def _touch_ui():
    wm = getattr(bpy.context, "window_manager", None)
    if wm is None:
        return
    for window in wm.windows:
        screen = getattr(window, "screen", None)
        if screen is None:
            continue
        for area in screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _state_needs_periodic_ui_refresh(scene_name, state):
    if state is None:
        return False
    if state.launch_state in {"Launching", "Stopping"}:
        return True
    if state.is_busy or state.imagegen_is_busy or state.waiting_for_backend_progress:
        return True
    if (state.task_status or "").lower() in {"processing", "queued"}:
        return True
    if (state.imagegen_task_status or "").lower() in {"processing", "queued"}:
        return True
    return False


def _ui_refresh_timer():
    for scene in bpy.data.scenes:
        state = getattr(scene, "nymphs_state", None)
        if _state_needs_periodic_ui_refresh(scene.name, state):
            _touch_ui()
            return UI_REFRESH_ACTIVE_SECONDS
    return UI_REFRESH_IDLE_SECONDS


def _sync_shape_texture_state(state):
    if state is None:
        return
    if not bool(getattr(state, "server_supports_texture", False)):
        state.shape_generate_texture = False


def _launch_backend_label(state):
    if state.launch_backend == "BACKEND_TRELLIS":
        return "TRELLIS.2"
    return "2mv"


def _service_prefix(service_key):
    return SERVICE_PROP_PREFIXES[service_key]


def _service_prop_name(service_key, suffix):
    return f"{_service_prefix(service_key)}_{suffix}"


def _service_get(state, service_key, suffix, default=None):
    return getattr(state, _service_prop_name(service_key, suffix), default)


def _service_changes(service_key, **changes):
    return {
        _service_prop_name(service_key, key): value
        for key, value in changes.items()
    }


def _selected_3d_service_key(state):
    if state.launch_backend == "BACKEND_TRELLIS":
        return "trellis"
    return "2mv"


def _service_port(state, service_key):
    value = (_service_get(state, service_key, "port", "") or "").strip()
    if value:
        return value
    if service_key == "trellis":
        return "8094"
    if service_key == "n2d2":
        return "8090"
    return "8080"


def _n2d2_model_family(model_id: str | None) -> str:
    normalized = (model_id or DEFAULT_N2D2_MODEL_ID).strip().lower()
    if "z-image" in normalized:
        return "zimage"
    return "generic"

def _n2d2_default_dtype(model_id: str | None) -> str:
    if _n2d2_model_family(model_id) == "zimage":
        return "bfloat16"
    return "float16"


def _transient_cache_get(bucket, key, ttl_seconds):
    now = time.monotonic()
    with CACHE_LOCK:
        bucket_cache = TRANSIENT_CACHE.get(bucket)
        if not bucket_cache:
            return CACHE_MISS
        entry = bucket_cache.get(key)
        if entry is None:
            return CACHE_MISS
        if now >= entry["expires_at"]:
            bucket_cache.pop(key, None)
            if not bucket_cache:
                TRANSIENT_CACHE.pop(bucket, None)
            return CACHE_MISS
        return entry["value"]


def _transient_cache_set(bucket, key, value, ttl_seconds):
    expires_at = time.monotonic() + max(0.0, float(ttl_seconds))
    with CACHE_LOCK:
        bucket_cache = TRANSIENT_CACHE.setdefault(bucket, {})
        bucket_cache[key] = {
            "value": value,
            "expires_at": expires_at,
        }
    return value


def _transient_cached(bucket, key, ttl_seconds, loader):
    cached = _transient_cache_get(bucket, key, ttl_seconds)
    if cached is not CACHE_MISS:
        return cached
    value = loader()
    return _transient_cache_set(bucket, key, value, ttl_seconds)


def _service_api_root(state, service_key):
    return f"http://127.0.0.1:{_service_port(state, service_key)}"


def _service_enabled(state, service_key):
    return bool(_service_get(state, service_key, "enabled", False))


def _service_summary_is_ready(summary):
    cleaned = (summary or "").strip()
    if cleaned in {"", "Unavailable", "Not checked"}:
        return False
    return not cleaned.lower().startswith("waiting for /server_info")


def _service_runtime_is_available(state, service_key):
    if _service_summary_is_ready(_service_get(state, service_key, "backend_summary", "Unavailable")):
        return True
    return _backend_is_alive(service_key)


def _short_n2d2_runtime_label(state):
    preset = getattr(state, "n2d2_model_preset", DEFAULT_N2D2_MODEL_PRESET)
    rank = getattr(state, "n2d2_nunchaku_rank", DEFAULT_N2D2_NUNCHAKU_RANK)
    if preset.startswith("zimage_nunchaku_r"):
        return f"Nunchaku r{rank}"
    if preset == "custom":
        model = _path_leaf(getattr(state, "n2d2_model_id", DEFAULT_N2D2_MODEL_ID)) or "Custom"
        return f"Custom {model}"
    return preset.replace("_", " ").title()


def _schedule_event_loop():
    if not bpy.app.timers.is_registered(_drain_events):
        bpy.app.timers.register(_drain_events, first_interval=0.0)


def _emit_status(scene_name, **changes):
    EVENT_QUEUE.put(("status", scene_name, changes))
    _schedule_event_loop()


def _emit_import(
    scene_name,
    mesh_path,
    source_object_name="",
    hide_source=True,
    update_shape_result=True,
):
    EVENT_QUEUE.put(
        (
            "import",
            ImportEvent(
                scene_name,
                mesh_path,
                source_object_name,
                hide_source,
                update_shape_result,
            ),
            None,
        )
    )
    _schedule_event_loop()


def _object_root(obj):
    current = obj
    while current is not None and current.parent is not None:
        current = current.parent
    return current


def _hide_object_tree(obj):
    if obj is None:
        return
    obj.hide_set(True)
    obj.hide_render = True
    for child in obj.children_recursive:
        child.hide_set(True)
        child.hide_render = True


def _show_object_tree(obj):
    if obj is None:
        return
    obj.hide_set(False)
    obj.hide_viewport = False
    obj.hide_render = False
    for child in obj.children_recursive:
        child.hide_set(False)
        child.hide_viewport = False
        child.hide_render = False


def _emit_service_status(scene_name, service_key, **changes):
    payload = _service_changes(service_key, **changes)
    state = _active_state(scene_name)
    if state is not None and service_key == _selected_3d_service_key(state):
        mirrored = {}
        if "launch_state" in changes:
            mirrored["launch_state"] = changes["launch_state"]
        if "launch_detail" in changes:
            mirrored["launch_detail"] = changes["launch_detail"]
        payload.update(mirrored)
    _emit_status(scene_name, **payload)


def _drain_events():
    processed = False
    while True:
        try:
            kind, payload_a, payload_b = EVENT_QUEUE.get_nowait()
        except queue.Empty:
            break

        processed = True
        if kind == "status":
            state = _active_state(payload_a)
            if state is not None:
                for key, value in payload_b.items():
                    setattr(state, key, value)
                _sync_shape_texture_state(state)
                if "part_extraction_plan_json" in payload_b:
                    _sync_part_extraction_items_from_plan(state)
        elif kind == "import":
            _import_result(payload_a)

    if processed:
        _touch_ui()

    if not EVENT_QUEUE.empty():
        return 0.1

    for scene in bpy.data.scenes:
        state = getattr(scene, "nymphs_state", None)
        if state is not None and (
            state.is_busy or state.launch_state in {"Launching", "Stopping"}
        ):
            return 0.2

    return None


def _normalize_api_root(raw_value):
    value = (raw_value or "").strip()
    if not value:
        raise RuntimeError("API URL is required.")
    return value.rstrip("/")


def _online_access_enabled():
    value = getattr(bpy.app, "online_access", None)
    if value is not None:
        return bool(value)
    return True


def _require_network_access(api_root):
    parsed = urlparse(api_root)
    hostname = (parsed.hostname or "").strip().lower()
    if hostname and hostname not in LOCAL_HOSTS and not _online_access_enabled():
        raise RuntimeError("Enable Blender online access before using a remote API URL.")


def _http_call(method, url, payload=None, timeout=10, headers=None):
    body = None
    request_headers = dict(headers or {})
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")

    request = Request(url=url, data=body, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"HTTP {exc.code}: {detail or exc.reason}") from exc
    except URLError as exc:
        parsed = urlparse(url)
        host = parsed.hostname or "unknown-host"
        port = parsed.port
        target = f"{host}:{port}" if port else host
        if host in LOCAL_HOSTS:
            raise RuntimeError(
                f"Could not reach local server at {target}. Start the backend in Nymphs Server or check the configured port."
            ) from exc
        raise RuntimeError(f"Network error reaching {target}: {exc.reason}") from exc


def _json_call(method, url, payload=None, timeout=10):
    _, _, body = _http_call(method, url, payload=payload, timeout=timeout)
    try:
        return json.loads(body.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("Server returned invalid JSON.") from exc


def _format_progress_text(current=None, total=None, percent=None):
    if current is not None and total:
        return f"{current}/{total}"
    if percent is not None:
        try:
            return f"{float(percent):.0f}%"
        except Exception:
            return str(percent)
    return ""


def _stage_label(stage):
    labels = {
        "startup": "Starting server",
        "request_received": "Request Received",
        "loading_input_image": "Loading Input",
        "loading_input_mesh": "Loading Mesh",
        "normalizing_mesh": "Normalizing Mesh",
        "loading_shape_pipeline": "Loading Shape",
        "sampling_shape": "Generating Shape",
        "shape_ready": "Shape Ready",
        "loading_texture_pipeline": "Loading Texture",
        "generating_texture": "Generating Texture",
        "texture": "Generating Texture",
        "texture_ready": "Texture Ready",
        "exporting_mesh": "Exporting Mesh",
        "exporting_textured_mesh": "Exporting Textured Mesh",
        "failed": "Failed",
    }
    if not stage:
        return ""
    return labels.get(stage, stage.replace("_", " ").title())


def _detect_n2d2_model_preset(state) -> str:
    model_id = (getattr(state, "n2d2_model_id", DEFAULT_N2D2_MODEL_ID) or DEFAULT_N2D2_MODEL_ID).strip()
    variant = (getattr(state, "n2d2_model_variant", DEFAULT_N2D2_MODEL_VARIANT) or DEFAULT_N2D2_MODEL_VARIANT).strip()
    rank = (getattr(state, "n2d2_nunchaku_rank", DEFAULT_N2D2_NUNCHAKU_RANK) or DEFAULT_N2D2_NUNCHAKU_RANK).strip()

    if model_id != DEFAULT_N2D2_MODEL_ID:
        return "custom"
    if variant:
        return "custom"
    if rank == "32":
        return "zimage_nunchaku_r32"
    if rank == "128":
        return "zimage_nunchaku_r128"
    if rank == "256":
        return "zimage_nunchaku_r256"
    return "custom"


def _sync_n2d2_model_preset(state):
    global N2D2_PRESET_SYNC_GUARD
    if N2D2_PRESET_SYNC_GUARD:
        return
    detected = _detect_n2d2_model_preset(state)
    current = getattr(state, "n2d2_model_preset", DEFAULT_N2D2_MODEL_PRESET)
    if current == detected:
        return
    N2D2_PRESET_SYNC_GUARD = True
    try:
        state.n2d2_model_preset = detected
    finally:
        N2D2_PRESET_SYNC_GUARD = False


def _on_n2d2_model_preset_changed(self, context):
    global N2D2_PRESET_SYNC_GUARD
    if N2D2_PRESET_SYNC_GUARD:
        return
    preset = getattr(self, "n2d2_model_preset", DEFAULT_N2D2_MODEL_PRESET)
    if preset == "custom":
        return
    N2D2_PRESET_SYNC_GUARD = True
    try:
        self.n2d2_model_id = DEFAULT_N2D2_MODEL_ID
        self.n2d2_model_variant = DEFAULT_N2D2_MODEL_VARIANT
        if preset == "zimage_nunchaku_r32":
            self.n2d2_nunchaku_rank = "32"
        elif preset == "zimage_nunchaku_r128":
            self.n2d2_nunchaku_rank = "128"
        elif preset == "zimage_nunchaku_r256":
            self.n2d2_nunchaku_rank = "256"
    finally:
        N2D2_PRESET_SYNC_GUARD = False
    _restart_n2d2_after_model_change(context, self)


def _on_n2d2_model_config_changed(self, context):
    if N2D2_PRESET_SYNC_GUARD:
        return
    _sync_n2d2_model_preset(self)
    _restart_n2d2_after_model_change(context, self)


def _openrouter_config_path():
    config_dir = bpy.utils.user_resource("CONFIG", path="nymphs", create=True)
    return os.path.join(config_dir, "openrouter.json")


def _load_openrouter_api_key_config():
    try:
        with open(_openrouter_config_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return ""
    return str(data.get("api_key") or "").strip()


def _save_openrouter_api_key_config(api_key):
    try:
        path = _openrouter_config_path()
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"api_key": (api_key or "").strip()}, handle, indent=2)
            handle.write("\n")
    except Exception:
        pass


def _on_openrouter_api_key_changed(self, context):
    _save_openrouter_api_key_config(getattr(self, "openrouter_api_key", ""))


def _ensure_openrouter_api_key_loaded(state):
    if (getattr(state, "openrouter_api_key", "") or "").strip():
        return
    api_key = _load_openrouter_api_key_config()
    if api_key:
        try:
            state.openrouter_api_key = api_key
        except Exception:
            pass


def _part_guidance_block_lines():
    return {
        "base_face": [
            "Keep facial structure on the anatomy base.",
            "Match the source character's nose, mouth, cheeks, jaw, and brow structure.",
            "Do not simplify the whole head into a blank mannequin if Face is enabled.",
        ],
        "eyes_in_base": [
            "Keep finished eyes on the anatomy base.",
            "Match source eye placement, shape, stylization, and rendering treatment.",
        ],
        "eyeball_part": [
            "Add one separate reusable Eyeball part.",
            "It must be exactly one isolated spherical eyeball asset.",
            "Do not include eyelids, eyelashes, skin, brow, socket, tear duct, face crop, head, or surrounding flesh.",
        ],
    }


def _part_guidance_block_text(key):
    labels = {
        "base_face": "Auto Base Face",
        "eyes_in_base": "Auto Eyes In Base",
        "eyeball_part": "Auto Eyeball Part",
    }
    lines = _part_guidance_block_lines().get(key, [])
    if not lines:
        return ""
    body = "\n".join(f"- {line}" for line in lines)
    label = labels.get(key, key)
    return f"[{label}]\n{body}\n[/{label}]"


def _remove_part_guidance_block(text, key):
    labels = {
        "base_face": "Auto Base Face",
        "eyes_in_base": "Auto Eyes In Base",
        "eyeball_part": "Auto Eyeball Part",
    }
    label = re.escape(labels.get(key, key))
    pattern = re.compile(rf"\n?\[{label}\]\n.*?\n\[/{label}\]\n?", re.DOTALL)
    cleaned = pattern.sub("\n", text or "")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _upsert_part_guidance_block(text, key, enabled):
    cleaned = _remove_part_guidance_block(text, key)
    block = _part_guidance_block_text(key) if enabled else ""
    if not block:
        return cleaned
    if not cleaned:
        return block
    return f"{cleaned}\n\n{block}"


def _strip_part_guidance_markers(text):
    value = (text or "").strip()
    value = re.sub(r"\n?\[Auto [^\]]+\]\n.*?\n\[/Auto [^\]]+\]\n?", "\n", value, flags=re.DOTALL)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _sync_part_option_guidance(state):
    global PART_GUIDANCE_SYNC_GUARD
    if PART_GUIDANCE_SYNC_GUARD:
        return
    PART_GUIDANCE_SYNC_GUARD = True
    try:
        if not bool(getattr(state, "part_base_include_face", False)):
            try:
                state.part_base_include_eyes = False
            except Exception:
                pass
        guidance = getattr(state, "part_extraction_guidance", "") or ""
        guidance = _upsert_part_guidance_block(guidance, "base_face", bool(getattr(state, "part_base_include_face", False)))
        guidance = _upsert_part_guidance_block(
            guidance,
            "eyes_in_base",
            bool(getattr(state, "part_base_include_face", False)) and bool(getattr(state, "part_base_include_eyes", False)),
        )
        guidance = _upsert_part_guidance_block(guidance, "eyeball_part", bool(getattr(state, "part_include_eye_part", False)))
        state.part_extraction_guidance = guidance
    finally:
        PART_GUIDANCE_SYNC_GUARD = False


def _on_part_extraction_option_changed(self, context):
    _sync_part_option_guidance(self)


def _imagegen_managed_prompt_block_text(kind, preset):
    labels = {
        PROMPT_KIND_SUBJECT: "Auto Subject",
        PROMPT_KIND_STYLE: "Auto Style",
    }
    prompt = (preset.get("prompt") or "").strip()
    if not prompt or kind not in labels:
        return ""
    return f"[{labels[kind]}]\n{prompt}\n[/{labels[kind]}]"


def _remove_imagegen_managed_prompt_block(text, kind):
    labels = {
        PROMPT_KIND_SUBJECT: "Auto Subject",
        PROMPT_KIND_STYLE: "Auto Style",
    }
    label = re.escape(labels.get(kind, ""))
    if not label:
        return (text or "").strip()
    pattern = re.compile(rf"\n?\[{label}\]\n.*?\n\[/{label}\]\n?", re.DOTALL)
    cleaned = pattern.sub("\n", text or "")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _strip_imagegen_prompt_markers(text):
    value = (text or "").strip()
    value = re.sub(r"\n?\[Auto (?:Subject|Style)\]\n.*?\n\[/Auto (?:Subject|Style)\]\n?", "\n", value, flags=re.DOTALL)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _sync_imagegen_managed_prompt_blocks(state):
    global IMAGEGEN_PROMPT_SYNC_GUARD
    if IMAGEGEN_PROMPT_SYNC_GUARD:
        return
    IMAGEGEN_PROMPT_SYNC_GUARD = True
    try:
        current_prompt = getattr(state, "imagegen_prompt", "") or ""
        body = _remove_imagegen_managed_prompt_block(current_prompt, PROMPT_KIND_SUBJECT)
        body = _remove_imagegen_managed_prompt_block(body, PROMPT_KIND_STYLE)

        blocks = []
        subject_key = _sync_imagegen_prompt_preset(state, "imagegen_prompt_preset", PROMPT_KIND_SUBJECT)
        style_key = _sync_imagegen_style_preset(state)

        subject = _imagegen_prompt_preset_data(subject_key, PROMPT_KIND_SUBJECT)
        subject_block = _imagegen_managed_prompt_block_text(PROMPT_KIND_SUBJECT, subject)
        if subject_block:
            blocks.append(subject_block)

        style = _imagegen_prompt_preset_data(style_key, PROMPT_KIND_STYLE)
        style_block = _imagegen_managed_prompt_block_text(PROMPT_KIND_STYLE, style)
        if style_block:
            blocks.append(style_block)

        new_prompt = "\n\n".join(blocks + ([body.strip()] if body.strip() else []))
        if (current_prompt or "").strip() != (new_prompt or "").strip():
            _set_imagegen_prompt_value(state, "prompt", new_prompt)
    finally:
        IMAGEGEN_PROMPT_SYNC_GUARD = False


def _clear_imagegen_managed_prompt_blocks(state, *, reset_dropdowns=False):
    prompt = getattr(state, "imagegen_prompt", "") or ""
    prompt = _remove_imagegen_managed_prompt_block(prompt, PROMPT_KIND_SUBJECT)
    prompt = _remove_imagegen_managed_prompt_block(prompt, PROMPT_KIND_STYLE)
    _set_imagegen_prompt_value(state, "prompt", prompt)
    if reset_dropdowns:
        try:
            state.imagegen_prompt_preset = NO_PROMPT_PRESET_KEY
            state.imagegen_style_preset = NO_PROMPT_PRESET_KEY
        except Exception:
            pass


def _reset_imagegen_prompt_builder_state(state):
    _clear_imagegen_managed_prompt_blocks(state, reset_dropdowns=True)


def _on_imagegen_managed_prompt_changed(self, context):
    _sync_imagegen_managed_prompt_blocks(self)


def _load_selected_saved_prompt_into_prompt(state):
    key = _sync_imagegen_prompt_preset(state, "imagegen_saved_prompt_preset", PROMPT_KIND_SAVED)
    preset = _imagegen_prompt_preset_data(key, PROMPT_KIND_SAVED)
    if not preset.get("prompt"):
        return False
    _reset_imagegen_prompt_builder_state(state)
    _set_imagegen_prompt_value(state, "prompt", preset["prompt"])
    return True


def _on_imagegen_saved_prompt_changed(self, context):
    if _load_selected_saved_prompt_into_prompt(self):
        self.imagegen_status_text = f"Loaded saved prompt: {(_imagegen_prompt_preset_data(self.imagegen_saved_prompt_preset, PROMPT_KIND_SAVED).get('label') or 'Saved Prompt')}"


def _imagegen_preset_dir():
    return bpy.utils.user_resource(
        "CONFIG",
        path=os.path.join("nymphs", "image_presets"),
        create=True,
    )


def _imagegen_preset_slug(name):
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
    return slug or "prompt_preset"


def _imagegen_preset_file(key):
    filename_key = re.sub(r"[^a-z0-9_]+", "_", (key or "").strip().lower()).strip("_")
    return os.path.join(_imagegen_preset_dir(), f"{filename_key or 'prompt_preset'}.json")


def _prompt_preset_key(kind, key):
    key = _imagegen_preset_slug(key)
    return f"{kind}__{key}"


def _split_prompt_preset_key(value):
    raw = (value or "").strip()
    for kind in (PROMPT_KIND_SUBJECT, PROMPT_KIND_STYLE, PROMPT_KIND_SAVED):
        prefix = f"{kind}__"
        if raw.startswith(prefix):
            return kind, raw[len(prefix):]
    return PROMPT_KIND_SUBJECT, raw


def _prompt_preset_text(data):
    return str(data.get("prompt") or data.get("style") or data.get("text") or "").strip()


def _prompt_preset_payload(name, kind, prompt, description=""):
    payload = {
        "name": name,
        "kind": kind,
        "prompt": prompt,
    }
    if description:
        payload["description"] = description
    return payload


def _seed_imagegen_prompt_presets():
    try:
        preset_dir = _imagegen_preset_dir()
    except Exception:
        return
    marker = os.path.join(preset_dir, ".defaults_seeded")
    for key, data in IMAGEGEN_PROMPT_PRESETS.items():
        path = _imagegen_preset_file(_prompt_preset_key(PROMPT_KIND_SUBJECT, key))
        if os.path.exists(path):
            continue
        payload = _prompt_preset_payload(
            data["label"],
            PROMPT_KIND_SUBJECT,
            data["prompt"],
            data.get("description", ""),
        )
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
    for key, data in IMAGEGEN_STYLE_PRESETS.items():
        path = _imagegen_preset_file(_prompt_preset_key(PROMPT_KIND_STYLE, key))
        if os.path.exists(path):
            continue
        payload = _prompt_preset_payload(
            data["label"],
            PROMPT_KIND_STYLE,
            data["style"],
            data.get("description", ""),
        )
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
    with open(marker, "w", encoding="utf-8") as handle:
        handle.write("Defaults seeded.\n")


def _load_imagegen_prompt_presets():
    preset_dir = _imagegen_preset_dir()

    def _load():
        presets = {}
        for key, data in IMAGEGEN_PROMPT_PRESETS.items():
            preset_key = _prompt_preset_key(PROMPT_KIND_SUBJECT, key)
            presets[preset_key] = {
                "label": data["label"],
                "description": data["description"],
                "kind": PROMPT_KIND_SUBJECT,
                "prompt": data["prompt"],
            }
        for key, data in IMAGEGEN_STYLE_PRESETS.items():
            preset_key = _prompt_preset_key(PROMPT_KIND_STYLE, key)
            presets[preset_key] = {
                "label": data["label"],
                "description": data["description"],
                "kind": PROMPT_KIND_STYLE,
                "prompt": data["style"],
            }
        try:
            _seed_imagegen_prompt_presets()
            for filename in sorted(os.listdir(preset_dir)):
                if not filename.lower().endswith(".json"):
                    continue
                file_key = os.path.splitext(filename)[0]
                path = os.path.join(preset_dir, filename)
                try:
                    with open(path, "r", encoding="utf-8") as handle:
                        data = json.load(handle)
                except Exception:
                    continue
                raw_kind = str(data.get("kind") or data.get("type") or "").strip().lower()
                inferred_kind, inferred_key = _split_prompt_preset_key(file_key)
                kind = raw_kind if raw_kind in PROMPT_KIND_LABELS else inferred_kind
                if raw_kind in PROMPT_KIND_LABELS:
                    old_prefix = f"{raw_kind}_"
                    if inferred_key.startswith(old_prefix):
                        inferred_key = inferred_key[len(old_prefix):]
                name = str(data.get("name") or data.get("label") or inferred_key.replace("_", " ").title()).strip()
                prompt = _prompt_preset_text(data)
                if not prompt:
                    continue
                preset_key = _prompt_preset_key(kind, inferred_key or name)
                presets[preset_key] = {
                    "label": name,
                    "description": str(data.get("description") or f"{PROMPT_KIND_LABELS.get(kind, 'Prompt')} preset file: {filename}").strip(),
                    "kind": kind,
                    "prompt": prompt,
                }
        except Exception:
            pass
        return presets

    return _transient_cached("imagegen_prompt_presets", preset_dir, PRESET_CACHE_TTL_SECONDS, _load)


def _imagegen_prompt_presets_by_kind(kind):
    presets = _load_imagegen_prompt_presets()
    return {
        key: data
        for key, data in presets.items()
        if data.get("kind") == kind
    }


def _imagegen_prompt_preset_data(preset_key, kind=PROMPT_KIND_SUBJECT):
    presets = _imagegen_prompt_presets_by_kind(kind)
    resolved_key = (preset_key or "").strip()
    if resolved_key == NO_PROMPT_PRESET_KEY:
        return {"label": "None", "prompt": "", "kind": kind}
    if resolved_key and resolved_key != NO_PROMPT_PRESET_KEY and resolved_key in presets:
        return presets[resolved_key]
    default_key = _prompt_preset_key(PROMPT_KIND_SUBJECT, DEFAULT_IMAGEGEN_PROMPT_PRESET)
    if kind == PROMPT_KIND_SUBJECT and default_key in presets:
        return presets[default_key]
    return next(iter(presets.values()), {"label": "No Preset", "prompt": ""})


def _imagegen_prompt_preset_items_for_kind(kind, include_none=False):
    presets = _imagegen_prompt_presets_by_kind(kind)
    items = []
    if include_none:
        items.append((NO_PROMPT_PRESET_KEY, "None", f"Do not insert a {PROMPT_KIND_LABELS.get(kind, 'prompt').lower()} prompt"))
    if not presets:
        return tuple(items or [(NO_PROMPT_PRESET_KEY, "No Presets", "No prompt presets found")])
    items.extend((key, data["label"], data["description"]) for key, data in presets.items())
    return tuple(items)


def _imagegen_subject_preset_items(self, context):
    return _imagegen_prompt_preset_items_for_kind(PROMPT_KIND_SUBJECT, include_none=True)


def _imagegen_style_preset_items(self, context):
    return _imagegen_prompt_preset_items_for_kind(PROMPT_KIND_STYLE, include_none=True)


def _imagegen_saved_prompt_items(self, context):
    return _imagegen_prompt_preset_items_for_kind(PROMPT_KIND_SAVED, include_none=True)


def _imagegen_prompt_preset_items(self, context):
    return _imagegen_subject_preset_items(self, context)


def _resolve_imagegen_prompt_preset_key(preset_key, kind=PROMPT_KIND_SUBJECT):
    presets = _imagegen_prompt_presets_by_kind(kind)
    resolved_key = (preset_key or "").strip()
    if resolved_key == NO_PROMPT_PRESET_KEY:
        return NO_PROMPT_PRESET_KEY
    if resolved_key in presets:
        return resolved_key
    default_key = _prompt_preset_key(PROMPT_KIND_SUBJECT, DEFAULT_IMAGEGEN_PROMPT_PRESET)
    if kind == PROMPT_KIND_SUBJECT and default_key in presets:
        return default_key
    return NO_PROMPT_PRESET_KEY if kind != PROMPT_KIND_SUBJECT else next(iter(presets.keys()), NO_PROMPT_PRESET_KEY)


def _sync_imagegen_prompt_preset(state, attr_name="imagegen_prompt_preset", kind=PROMPT_KIND_SUBJECT):
    key = _resolve_imagegen_prompt_preset_key(getattr(state, attr_name, ""), kind)
    try:
        if getattr(state, attr_name, "") != key:
            setattr(state, attr_name, key)
    except Exception:
        pass
    return key


def _imagegen_style_preset_dir():
    return _imagegen_preset_dir()


def _imagegen_style_preset_slug(name):
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
    return slug or "style_preset"


def _imagegen_style_preset_file(key):
    return _imagegen_preset_file(_prompt_preset_key(PROMPT_KIND_STYLE, key))


def _seed_imagegen_style_presets():
    _seed_imagegen_prompt_presets()


def _load_imagegen_style_presets():
    return {
        key: {
            "label": data["label"],
            "description": data["description"],
            "style": data["prompt"],
        }
        for key, data in _imagegen_prompt_presets_by_kind(PROMPT_KIND_STYLE).items()
    }


def _imagegen_style_preset_data(preset_key):
    data = _imagegen_prompt_preset_data(preset_key, PROMPT_KIND_STYLE)
    if data.get("prompt"):
        return {
            "label": data.get("label", "Style"),
            "description": data.get("description", ""),
            "style": data.get("prompt", ""),
        }
    return {"label": "No Style", "style": ""}


def _imagegen_style_preset_items(self, context):
    return _imagegen_prompt_preset_items_for_kind(PROMPT_KIND_STYLE, include_none=True)


def _resolve_imagegen_style_preset_key(preset_key):
    return _resolve_imagegen_prompt_preset_key(preset_key, PROMPT_KIND_STYLE)


def _sync_imagegen_style_preset(state):
    return _sync_imagegen_prompt_preset(state, "imagegen_style_preset", PROMPT_KIND_STYLE)


def _compose_imagegen_prompt(prompt_text, style_text):
    return _strip_imagegen_prompt_markers(prompt_text or "")


def _resolved_imagegen_prompt(state):
    return _strip_imagegen_prompt_markers(getattr(state, "imagegen_prompt", "") or "")


def _imagegen_settings_preset_dir():
    return bpy.utils.user_resource(
        "CONFIG",
        path=os.path.join("nymphs", "image_settings_presets"),
        create=True,
    )


def _imagegen_settings_preset_slug(name):
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
    return slug or "image_settings_preset"


def _imagegen_settings_preset_file(key):
    return os.path.join(_imagegen_settings_preset_dir(), f"{_imagegen_settings_preset_slug(key)}.json")


def _current_imagegen_settings_values(state):
    return {
        "n2d2_model_preset": getattr(state, "n2d2_model_preset", DEFAULT_N2D2_MODEL_PRESET),
        "imagegen_width": int(getattr(state, "imagegen_width", 1024)),
        "imagegen_height": int(getattr(state, "imagegen_height", 1024)),
        "imagegen_steps": int(getattr(state, "imagegen_steps", 9)),
        "imagegen_guidance_scale": float(getattr(state, "imagegen_guidance_scale", 0.0)),
        "imagegen_variant_count": int(getattr(state, "imagegen_variant_count", 1)),
        "imagegen_seed_step": int(getattr(state, "imagegen_seed_step", 1)),
    }


def _apply_imagegen_settings_preset(state, values):
    ordered_keys = ("n2d2_model_preset", "n2d2_model_id", "n2d2_nunchaku_rank", "n2d2_model_variant")
    for key in ordered_keys:
        if key in values and hasattr(state, key):
            setattr(state, key, values[key])
    for key, value in values.items():
        if key in ordered_keys:
            continue
        if hasattr(state, key):
            setattr(state, key, value)


def _seed_imagegen_settings_presets():
    try:
        preset_dir = _imagegen_settings_preset_dir()
    except Exception:
        return
    marker = os.path.join(preset_dir, ".defaults_seeded")
    if os.path.exists(marker):
        return
    for key, data in IMAGEGEN_SETTINGS_PRESETS.items():
        path = _imagegen_settings_preset_file(key)
        if os.path.exists(path):
            continue
        payload = {
            "name": data["label"],
            "description": data["description"],
            "values": data["values"],
        }
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
    with open(marker, "w", encoding="utf-8") as handle:
        handle.write("Defaults seeded.\n")


def _load_imagegen_settings_presets():
    preset_dir = _imagegen_settings_preset_dir()

    def _load():
        presets = {
            key: {
                "label": data["label"],
                "description": data["description"],
                "values": dict(data["values"]),
            }
            for key, data in IMAGEGEN_SETTINGS_PRESETS.items()
        }
        try:
            _seed_imagegen_settings_presets()
            for filename in sorted(os.listdir(preset_dir)):
                if not filename.lower().endswith(".json"):
                    continue
                key = os.path.splitext(filename)[0]
                if key in IMAGEGEN_SETTINGS_PRESETS or key in LEGACY_IMAGEGEN_SETTINGS_PRESET_KEYS:
                    continue
                path = os.path.join(preset_dir, filename)
                try:
                    with open(path, "r", encoding="utf-8") as handle:
                        data = json.load(handle)
                except Exception:
                    continue
                values = data.get("values")
                if not isinstance(values, dict):
                    continue
                name = str(data.get("name") or key.replace("_", " ").title()).strip()
                presets[key] = {
                    "label": name,
                    "description": str(data.get("description") or f"Generation profile file: {filename}").strip(),
                    "values": values,
                }
        except Exception:
            pass
        return presets

    return _transient_cached("imagegen_settings_presets", preset_dir, PRESET_CACHE_TTL_SECONDS, _load)


def _imagegen_settings_preset_data(preset_key):
    presets = _load_imagegen_settings_presets()
    resolved_key = (preset_key or "").strip()
    if resolved_key in presets:
        return presets[resolved_key]
    if DEFAULT_IMAGEGEN_SETTINGS_PRESET in presets:
        return presets[DEFAULT_IMAGEGEN_SETTINGS_PRESET]
    return next(iter(presets.values()), {"label": "No Preset", "values": {}})


def _imagegen_settings_preset_items(self, context):
    presets = _load_imagegen_settings_presets()
    if not presets:
        return (("__none__", "No Profiles", "No generation profiles found"),)
    return tuple((key, data["label"], data["description"]) for key, data in presets.items())


def _resolve_imagegen_settings_preset_key(preset_key):
    presets = _load_imagegen_settings_presets()
    resolved_key = (preset_key or "").strip()
    if resolved_key in presets:
        return resolved_key
    if DEFAULT_IMAGEGEN_SETTINGS_PRESET in presets:
        return DEFAULT_IMAGEGEN_SETTINGS_PRESET
    return next(iter(presets.keys()), "__none__")


def _sync_imagegen_settings_preset(state):
    key = _resolve_imagegen_settings_preset_key(getattr(state, "imagegen_settings_preset", ""))
    try:
        if getattr(state, "imagegen_settings_preset", "") != key:
            state.imagegen_settings_preset = key
    except Exception:
        pass
    return key


def _trellis_shape_preset_dir():
    return bpy.utils.user_resource(
        "CONFIG",
        path=os.path.join("nymphs", "trellis_shape_presets"),
        create=True,
    )


def _trellis_shape_preset_slug(name):
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
    return slug or "trellis_shape_preset"


def _trellis_shape_preset_file(key):
    return os.path.join(_trellis_shape_preset_dir(), f"{_trellis_shape_preset_slug(key)}.json")


def _current_trellis_shape_preset_values(state):
    return {
        "trellis_pipeline_type": getattr(state, "trellis_pipeline_type", "1024_cascade"),
        "trellis_max_tokens": int(getattr(state, "trellis_max_tokens", 49152)),
        "trellis_texture_size": str(getattr(state, "trellis_texture_size", "2048")),
        "trellis_decimation_target": int(getattr(state, "trellis_decimation_target", 500000)),
        "trellis_ss_sampling_steps": int(getattr(state, "trellis_ss_sampling_steps", 12)),
        "trellis_ss_guidance_strength": float(getattr(state, "trellis_ss_guidance_strength", 7.5)),
        "trellis_ss_guidance_rescale": float(getattr(state, "trellis_ss_guidance_rescale", 0.7)),
        "trellis_ss_guidance_interval_start": float(getattr(state, "trellis_ss_guidance_interval_start", 0.6)),
        "trellis_ss_guidance_interval_end": float(getattr(state, "trellis_ss_guidance_interval_end", 1.0)),
        "trellis_ss_rescale_t": float(getattr(state, "trellis_ss_rescale_t", 5.0)),
        "trellis_shape_sampling_steps": int(getattr(state, "trellis_shape_sampling_steps", 12)),
        "trellis_shape_guidance_strength": float(getattr(state, "trellis_shape_guidance_strength", 7.5)),
        "trellis_shape_guidance_rescale": float(getattr(state, "trellis_shape_guidance_rescale", 0.5)),
        "trellis_shape_guidance_interval_start": float(getattr(state, "trellis_shape_guidance_interval_start", 0.6)),
        "trellis_shape_guidance_interval_end": float(getattr(state, "trellis_shape_guidance_interval_end", 1.0)),
        "trellis_shape_rescale_t": float(getattr(state, "trellis_shape_rescale_t", 3.0)),
        "trellis_tex_sampling_steps": int(getattr(state, "trellis_tex_sampling_steps", 12)),
        "trellis_tex_guidance_strength": float(getattr(state, "trellis_tex_guidance_strength", 1.0)),
        "trellis_tex_guidance_rescale": float(getattr(state, "trellis_tex_guidance_rescale", 0.0)),
        "trellis_tex_guidance_interval_start": float(getattr(state, "trellis_tex_guidance_interval_start", 0.6)),
        "trellis_tex_guidance_interval_end": float(getattr(state, "trellis_tex_guidance_interval_end", 0.9)),
        "trellis_tex_rescale_t": float(getattr(state, "trellis_tex_rescale_t", 3.0)),
    }


def _apply_trellis_shape_preset(state, values):
    for key, value in values.items():
        if hasattr(state, key):
            setattr(state, key, value)


def _seed_trellis_shape_presets():
    try:
        preset_dir = _trellis_shape_preset_dir()
    except Exception:
        return
    marker = os.path.join(preset_dir, ".defaults_seeded")
    if os.path.exists(marker):
        return
    for key, data in TRELLIS_SHAPE_PRESETS.items():
        path = _trellis_shape_preset_file(key)
        if os.path.exists(path):
            continue
        payload = {
            "name": data["label"],
            "description": data["description"],
            "values": data["values"],
        }
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
    with open(marker, "w", encoding="utf-8") as handle:
        handle.write("Defaults seeded.\n")


def _load_trellis_shape_presets():
    preset_dir = _trellis_shape_preset_dir()

    def _load():
        presets = {
            key: {
                "label": data["label"],
                "description": data["description"],
                "values": dict(data["values"]),
            }
            for key, data in TRELLIS_SHAPE_PRESETS.items()
        }
        try:
            _seed_trellis_shape_presets()
            for filename in sorted(os.listdir(preset_dir)):
                if not filename.lower().endswith(".json"):
                    continue
                key = os.path.splitext(filename)[0]
                if key in TRELLIS_SHAPE_PRESETS:
                    continue
                path = os.path.join(preset_dir, filename)
                try:
                    with open(path, "r", encoding="utf-8") as handle:
                        data = json.load(handle)
                except Exception:
                    continue
                values = data.get("values")
                if not isinstance(values, dict):
                    continue
                name = str(data.get("name") or key.replace("_", " ").title()).strip()
                presets[key] = {
                    "label": name,
                    "description": str(data.get("description") or f"TRELLIS shape preset file: {filename}").strip(),
                    "values": values,
                }
        except Exception:
            pass
        return presets

    return _transient_cached("trellis_shape_presets", preset_dir, PRESET_CACHE_TTL_SECONDS, _load)


def _trellis_shape_preset_data(preset_key):
    presets = _load_trellis_shape_presets()
    resolved_key = (preset_key or "").strip()
    if resolved_key in presets:
        return presets[resolved_key]
    if DEFAULT_TRELLIS_SHAPE_PRESET in presets:
        return presets[DEFAULT_TRELLIS_SHAPE_PRESET]
    return next(iter(presets.values()), {"label": "No Preset", "values": {}})


def _trellis_shape_preset_items(self, context):
    presets = _load_trellis_shape_presets()
    if not presets:
        return (("__none__", "No Presets", "No TRELLIS shape presets found"),)
    return tuple((key, data["label"], data["description"]) for key, data in presets.items())


def _resolve_trellis_shape_preset_key(preset_key):
    presets = _load_trellis_shape_presets()
    resolved_key = (preset_key or "").strip()
    if resolved_key in presets:
        return resolved_key
    if DEFAULT_TRELLIS_SHAPE_PRESET in presets:
        return DEFAULT_TRELLIS_SHAPE_PRESET
    return next(iter(presets.keys()), "__none__")


def _sync_trellis_shape_preset(state):
    key = _resolve_trellis_shape_preset_key(getattr(state, "trellis_shape_preset", ""))
    try:
        if getattr(state, "trellis_shape_preset", "") != key:
            state.trellis_shape_preset = key
    except Exception:
        pass
    return key


def _imagegen_text_field_name(target):
    if target == "guidance":
        return "part_extraction_guidance"
    return "imagegen_prompt"


def _imagegen_text_name_field(target):
    if target == "guidance":
        return "part_extraction_guidance_text_name"
    return "imagegen_prompt_text_name"


def _default_imagegen_text_name(target):
    if target == "guidance":
        return DEFAULT_PART_EXTRACTION_GUIDANCE_TEXT_NAME
    return DEFAULT_IMAGEGEN_PROMPT_TEXT_NAME


def _linked_imagegen_text(state, target):
    stored_name = (getattr(state, _imagegen_text_name_field(target), "") or "").strip()
    if stored_name:
        return bpy.data.texts.get(stored_name)
    return None


def _sync_imagegen_text_block(state, target, value):
    text = _linked_imagegen_text(state, target)
    if text is None:
        return False
    text.clear()
    if value:
        text.write(value)
    return True


def _set_imagegen_prompt_value(state, target, value, *, sync_text=True):
    value = value or ""
    setattr(state, _imagegen_text_field_name(target), value)
    if sync_text:
        _sync_imagegen_text_block(state, target, value)


def _insert_imagegen_prompt_text(state, value):
    insert_text = (value or "").strip()
    if not insert_text:
        return False
    current = (getattr(state, "imagegen_prompt", "") or "").strip()
    prompt = insert_text if not current else f"{current}\n\n{insert_text}"
    _set_imagegen_prompt_value(state, "prompt", prompt)
    return True


def _clear_prompt_preset_cache():
    with CACHE_LOCK:
        TRANSIENT_CACHE.pop("imagegen_prompt_presets", None)


def _ensure_imagegen_text(state, target):
    text = _linked_imagegen_text(state, target)
    created = False
    if text is None:
        name = _default_imagegen_text_name(target)
        text = bpy.data.texts.get(name)
        if text is None:
            text = bpy.data.texts.new(name)
            created = True
        setattr(state, _imagegen_text_name_field(target), text.name)
    current_value = getattr(state, _imagegen_text_field_name(target), "") or ""
    text_value = _text_to_prompt_value(text)
    if created or not text_value.strip() or text_value != current_value:
        text.clear()
        if current_value:
            text.write(current_value)
    return text


def _text_to_prompt_value(text):
    return text.as_string().rstrip("\n")


def _pull_imagegen_text_from_block(state, target):
    text = _linked_imagegen_text(state, target)
    if text is None:
        return False
    _set_imagegen_prompt_value(state, target, _text_to_prompt_value(text), sync_text=False)
    return True


def _open_text_in_editor(context, text):
    screen = getattr(context, "screen", None)
    target_area = None
    if screen is not None:
        for area in screen.areas:
            if area.type == "TEXT_EDITOR":
                target_area = area
                break
    if target_area is None:
        target_area = getattr(context, "area", None)
        if target_area is not None and target_area.type != "TEXT_EDITOR":
            try:
                target_area.type = "TEXT_EDITOR"
            except Exception:
                target_area = None
    if target_area is None or target_area.type != "TEXT_EDITOR":
        return False
    for space in target_area.spaces:
        if space.type == "TEXT_EDITOR":
            space.text = text
            try:
                space.show_word_wrap = True
            except Exception:
                pass
            return True
    return False


def _resolve_file_path(raw_path):
    path = (raw_path or "").strip()
    if not path:
        return ""

    if path.startswith("//"):
        blend_dir = os.path.dirname(bpy.data.filepath)
        if blend_dir:
            path = os.path.join(blend_dir, path[2:])

    return bpy.path.abspath(path)


def _file_to_base64(raw_path):
    path = _resolve_file_path(raw_path)
    if not path or not os.path.exists(path):
        raise RuntimeError(f"Missing file: {raw_path}")
    with open(path, "rb") as handle:
        return base64.b64encode(handle.read()).decode("utf-8")


def _parse_optional_seed(raw_value):
    value = (raw_value or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError("Seed must be blank or a whole number.") from exc


def _trellis_seed_value(state):
    parsed = _parse_optional_seed(getattr(state, "trellis_seed", ""))
    if parsed is not None:
        return parsed
    generated = int(time.time() * 1000) % 2147483647
    return generated if generated > 0 else 42


def _imagegen_seed_value(state, *, fallback_seed=None):
    seed = fallback_seed if fallback_seed is not None else _parse_optional_seed(state.imagegen_seed)
    if seed is not None:
        return seed, False
    generated = int(time.time() * 1000) % 2147483647
    if generated <= 0:
        generated = 1
    return generated, True


def _sanitize_name_fragment(value, fallback="result"):
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", (value or "").strip()).strip("-._")
    return cleaned[:48] or fallback


def _shape_output_dir():
    path = os.path.join(tempfile.gettempdir(), LOCAL_SHAPE_OUTPUT_DIRNAME)
    os.makedirs(path, exist_ok=True)
    return path


def _imagegen_output_dir():
    path = os.path.join(tempfile.gettempdir(), LOCAL_IMAGEGEN_OUTPUT_DIRNAME)
    os.makedirs(path, exist_ok=True)
    return path


def _shape_output_path(source_object_name=""):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    stem = _sanitize_name_fragment(source_object_name, fallback="shape")
    candidate = os.path.join(_shape_output_dir(), f"{stamp}-{stem}.glb")
    if not os.path.exists(candidate):
        return candidate
    return os.path.join(_shape_output_dir(), f"{stamp}-{stem}-{int(time.time() * 1000) % 100000}.glb")


def _imagegen_output_path(provider="image", suffix=".png"):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    stem = _sanitize_name_fragment(provider, fallback="image")
    suffix = suffix if str(suffix or "").startswith(".") else f".{suffix or 'png'}"
    candidate = os.path.join(_imagegen_output_dir(), f"{stamp}-{stem}{suffix}")
    if not os.path.exists(candidate):
        return candidate
    return os.path.join(_imagegen_output_dir(), f"{stamp}-{stem}-{int(time.time() * 1000) % 100000}{suffix}")


def _current_imagegen_folder(state):
    folder_path = (getattr(state, "imagegen_output_dir", "") or "").strip()
    if not folder_path:
        output_path = (getattr(state, "imagegen_output_path", "") or "").strip()
        if output_path:
            folder_path = os.path.dirname(output_path)
    if folder_path:
        return folder_path
    return _imagegen_output_dir()


def _clear_folder_contents(folder_path):
    target = (folder_path or "").strip()
    if not target:
        raise RuntimeError("No folder is available yet.")
    if not os.path.isdir(target):
        raise RuntimeError(f"Folder does not exist: {target}")
    for entry in os.scandir(target):
        if entry.is_dir(follow_symlinks=False):
            shutil.rmtree(entry.path)
        else:
            os.unlink(entry.path)


def _path_is_within(root_path, raw_path):
    root = os.path.abspath((root_path or "").strip())
    candidate = os.path.abspath((raw_path or "").strip())
    if not root or not candidate:
        return False
    try:
        return os.path.commonpath([root, candidate]) == root
    except Exception:
        return False


def _to_blender_accessible_path(state, raw_path):
    path = (raw_path or "").strip()
    if not path:
        return ""
    if os.name != "nt":
        return path
    if path.startswith("\\\\wsl.localhost\\"):
        return path
    if not path.startswith("/"):
        return path
    distro = _resolved_wsl_distro_name(state)
    windows_path = path.replace("/", "\\")
    return f"\\\\wsl.localhost\\{distro}{windows_path}"


def _blender_accessible_path_candidates(state, raw_path):
    path = _to_blender_accessible_path(state, raw_path)
    if not path:
        return []
    candidates = [path]
    if os.name == "nt" and path.startswith("\\\\wsl.localhost\\"):
        candidates.append(path.replace("\\\\wsl.localhost\\", "\\\\wsl$\\", 1))
    return candidates


def _blender_path_is_dir(state, raw_path):
    return any(os.path.isdir(path) for path in _blender_accessible_path_candidates(state, raw_path))


def _blender_path_exists(state, raw_path):
    return any((os.path.exists(path) or os.path.lexists(path)) for path in _blender_accessible_path_candidates(state, raw_path))


def _blender_path_is_file(state, raw_path):
    return any(
        (os.path.isfile(path) or ((os.path.exists(path) or os.path.lexists(path)) and not os.path.isdir(path)))
        for path in _blender_accessible_path_candidates(state, raw_path)
    )


def _build_imagegen_payload(state):
    prompt = _resolved_imagegen_prompt(state)
    return _build_imagegen_payload_for_prompt(state, prompt=prompt)


def _build_imagegen_payload_for_prompt(state, *, prompt, seed=None):
    if not prompt:
        raise RuntimeError("Enter an image-generation prompt first.")

    payload = {
        "mode": "txt2img",
        "prompt": prompt,
        "width": int(state.imagegen_width),
        "height": int(state.imagegen_height),
        "steps": int(state.imagegen_steps),
        "guidance_scale": float(state.imagegen_guidance_scale),
    }
    if seed is not None:
        payload["seed"] = seed
    return payload


def _build_mv_prompt(state, view_phrase):
    base_prompt = _resolved_imagegen_prompt(state)
    if not base_prompt:
        raise RuntimeError("Enter an image-generation prompt first.")
    prompt_parts = [
        base_prompt,
        "single subject reference",
        "centered subject",
        "clean silhouette",
        "whole subject visible",
        "simple plain light background",
        "soft even lighting",
        "minimal shadows",
        "no scenery",
        "consistent design",
        view_phrase,
    ]
    return ", ".join(part for part in prompt_parts if part)


def _gemini_model_id(state_or_snapshot):
    raw_value = ""
    if isinstance(state_or_snapshot, dict):
        raw_value = (state_or_snapshot.get("gemini_model") or "").strip()
    else:
        raw_value = (getattr(state_or_snapshot, "gemini_model", "") or "").strip()
    return GEMINI_MODEL_IDS.get(raw_value, raw_value or GEMINI_MODEL_IDS["gemini_2_5_flash_image"])


def _gemini_model_label(model_id):
    if model_id == "google/gemini-3.1-flash-image-preview":
        return "Gemini 3.1 Flash Image"
    if model_id == "google/gemini-3-pro-image-preview":
        return "Gemini 3 Pro Image"
    return "Gemini 2.5 Flash Image"


def _gemini_guide_image_path(state_or_snapshot):
    if isinstance(state_or_snapshot, dict):
        path = (state_or_snapshot.get("guide_image_path") or "").strip()
    else:
        enabled = bool(getattr(state_or_snapshot, "gemini_use_guide_image", False))
        path = (getattr(state_or_snapshot, "gemini_guide_image_path", "") or "").strip() if enabled else ""
    if not path:
        return ""
    return bpy.path.abspath(path).strip()


def _gemini_guide_image_data_url(raw_path):
    path = bpy.path.abspath((raw_path or "").strip())
    if not path:
        raise RuntimeError("Pick a guide image first.")
    if not os.path.isfile(path):
        raise RuntimeError(f"Guide image not found: {path}")

    mime_type, _encoding = mimetypes.guess_type(path)
    if mime_type not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
        suffix = os.path.splitext(path)[1].lower()
        if suffix in {".jpg", ".jpeg"}:
            mime_type = "image/jpeg"
        elif suffix == ".png":
            mime_type = "image/png"
        elif suffix == ".webp":
            mime_type = "image/webp"
        elif suffix == ".gif":
            mime_type = "image/gif"
        else:
            raise RuntimeError("Guide image must be a PNG, JPG, WEBP, or GIF file.")

    try:
        with open(path, "rb") as handle:
            encoded = base64.b64encode(handle.read()).decode("ascii")
    except Exception as exc:
        raise RuntimeError(f"Could not read guide image: {exc}") from exc
    return path, f"data:{mime_type};base64,{encoded}"


def _gemini_api_key(state):
    _ensure_openrouter_api_key_loaded(state)
    key = (getattr(state, "openrouter_api_key", "") or "").strip()
    if key:
        return key
    key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if key:
        return key
    raise RuntimeError("Enter an OpenRouter API key or set OPENROUTER_API_KEY before using Gemini image generation.")


def _gemini_snapshot(state):
    model_id = _gemini_model_id(state)
    snapshot = {
        "api_key": _gemini_api_key(state),
        "model_id": model_id,
        "model_label": _gemini_model_label(model_id),
        "aspect_ratio": (getattr(state, "gemini_aspect_ratio", "1:1") or "1:1").strip(),
        "image_size": (getattr(state, "gemini_image_size", "1K") or "1K").strip(),
        "guide_image_path": "",
        "guide_image_data_url": "",
    }
    if model_id not in GEMINI_IMAGE_SIZE_MODELS:
        snapshot["image_size"] = ""
    guide_image_path = _gemini_guide_image_path(state)
    if guide_image_path:
        snapshot["guide_image_path"], snapshot["guide_image_data_url"] = _gemini_guide_image_data_url(guide_image_path)
    return snapshot


def _part_planner_model_id(state_or_snapshot):
    if isinstance(state_or_snapshot, dict):
        raw_value = (state_or_snapshot.get("part_planner_model") or "").strip()
    else:
        raw_value = (getattr(state_or_snapshot, "part_planner_model", "") or "").strip()
    return GEMINI_PLANNER_MODEL_IDS.get(raw_value, raw_value or GEMINI_PLANNER_MODEL_IDS["gemini_2_5_flash"])


def _part_planner_model_label(model_id):
    if model_id == "google/gemini-3.1-pro-preview":
        return "Gemini 3.1 Pro"
    if model_id == "google/gemini-2.5-flash":
        return "Gemini 2.5 Flash"
    return _path_leaf(model_id) or model_id


def _part_extraction_snapshot(state, source_image_path):
    snapshot = _gemini_snapshot(state)
    guide_path, guide_data_url = _gemini_guide_image_data_url(source_image_path)
    snapshot["guide_image_path"] = guide_path
    snapshot["guide_image_data_url"] = guide_data_url
    snapshot["part_planner_model"] = _part_planner_model_id(state)
    snapshot["part_planner_label"] = _part_planner_model_label(snapshot["part_planner_model"])
    snapshot["part_extraction_guidance"] = _strip_part_guidance_markers(
        (getattr(state, "part_extraction_guidance", "") or "").strip()
    )
    if bool(getattr(state, "part_extraction_style_lock", True)):
        style_preset = _imagegen_style_preset_data(_sync_imagegen_style_preset(state))
        snapshot["part_extraction_style"] = (style_preset.get("style") or "").strip()
        snapshot["part_extraction_style_label"] = style_preset.get("label", "No Style")
    else:
        snapshot["part_extraction_style"] = ""
        snapshot["part_extraction_style_label"] = "No Style"
    snapshot["part_extraction_max_parts"] = max(1, int(getattr(state, "part_extraction_max_parts", 8)))
    snapshot["part_base_include_face"] = bool(getattr(state, "part_base_include_face", False))
    snapshot["part_base_include_eyes"] = bool(getattr(state, "part_base_include_face", False)) and bool(
        getattr(state, "part_base_include_eyes", False)
    )
    snapshot["part_include_eye_part"] = bool(getattr(state, "part_include_eye_part", False))
    return snapshot


def _openrouter_text_from_image(api_key, model_id, image_data_url, prompt, *, timeout=1800):
    prompt = (prompt or "").strip()
    if not prompt:
        raise RuntimeError("Enter a planning prompt first.")
    payload = {
        "model": model_id,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": prompt,
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": image_data_url,
                        },
                    },
                ],
            }
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
    }
    try:
        _, _, body = _http_call(
            "POST",
            f"{OPENROUTER_API_ROOT}/chat/completions",
            payload=payload,
            headers=headers,
            timeout=timeout,
        )
    except RuntimeError as exc:
        if "response_format" not in str(exc).lower():
            raise
        payload.pop("response_format", None)
        _, _, body = _http_call(
            "POST",
            f"{OPENROUTER_API_ROOT}/chat/completions",
            payload=payload,
            headers=headers,
            timeout=timeout,
        )
    try:
        detail = json.loads(body.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("OpenRouter returned invalid JSON.") from exc

    text_parts = []
    finish_reasons = []
    native_finish_reasons = []
    for choice in detail.get("choices", []) or []:
        finish_reason = (choice.get("finish_reason") or "").strip()
        if finish_reason:
            finish_reasons.append(finish_reason)
        native_finish_reason = (choice.get("native_finish_reason") or "").strip()
        if native_finish_reason:
            native_finish_reasons.append(native_finish_reason)
        message = choice.get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            text_parts.append(content.strip())
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_parts.append(str(item.get("text") or "").strip())

    text = "\n".join(part for part in text_parts if part).strip()
    if not text:
        message = "Planner did not return text."
        if native_finish_reasons:
            message += f" Provider finish reason: {', '.join(dict.fromkeys(native_finish_reasons))}."
        elif finish_reasons:
            message += f" Finish reason: {', '.join(dict.fromkeys(finish_reasons))}."
        raise RuntimeError(message)
    return text, detail


def _gemini_image_suffix(mime_type):
    value = (mime_type or "").lower()
    if "jpeg" in value or "jpg" in value:
        return ".jpg"
    if "webp" in value:
        return ".webp"
    return ".png"


def _gemini_safe_response_for_metadata(detail):
    safe_detail = json.loads(json.dumps(detail))
    for choice in safe_detail.get("choices", []) or []:
        message = choice.get("message") or {}
        for image in message.get("images", []) or []:
            image_url = image.get("image_url") or image.get("imageUrl") or {}
            if isinstance(image_url, dict) and image_url.get("url"):
                image_url["url"] = "<omitted>"
    return safe_detail


def _character_part_breakout_variant_prompts(base_prompt, variant_count):
    instructions = []
    for index in range(max(1, variant_count)):
        if index == 0:
            instructions.append(
                "Request target: render only the neutral anatomy base body in a clean A-pose or T-pose for a game asset base mesh reference. "
                "Use simplified non-explicit anatomy. "
                "Exactly one centered subject only. "
                "Critical exclusions: no second character, no clothing, no accessories, no weapons, no props, no staff, no separate items, and no side-by-side layout."
            )
        elif index == 1:
            instructions.append(
                "Request target: render only the hair or hairstyle asset from the character design. "
                "Exactly one centered subject only. "
                "Critical exclusions: no head, no face, no body, no mannequin, no clothing, no accessories, no second item, and no side-by-side layout."
            )
        else:
            instructions.append(
                "Request target: render only one different remaining item from the character design that has "
                "not been depicted yet. Prioritize clothing, armor pieces, accessories, weapons, and carried props. "
                "Exactly one centered subject only. "
                "Critical exclusions: do not render the body. Do not render hair unless hair is the chosen target. "
                "Do not include a full character, a second item, or a side-by-side layout."
            )
    return [f"{base_prompt.rstrip()}\n\n{instruction}" for instruction in instructions]


def _character_part_breakout_auto_prompt(base_prompt):
    return (
        f"{base_prompt.rstrip()}\n\n"
        "Request mode: automatic breakout set. "
        "Return the full character breakout as multiple separate images in one response. "
        "Generate as many images as needed to cover the anatomy base body, hair, and each major wearable or carried item. "
        "Start with the anatomy base body, then hair, then the remaining items. "
        "Exactly one centered subject per image. "
        "Critical exclusions: no side-by-side layouts, no combined items, and no full character plus prop in the same image."
    )


def _part_extraction_planning_prompt(
    guidance,
    *,
    base_include_face=False,
    base_include_eyes=False,
    include_eye_part=False,
):
    guidance = (guidance or DEFAULT_PART_EXTRACTION_GUIDANCE).strip()
    base_face_rule = (
        "- For anatomy_base, keep facial features on the base body and match the source character's face structure.\n"
        if base_include_face
        else "- For anatomy_base, keep the head feature-neutral with no finished face details, eyes, nose, mouth, or brows.\n"
    )
    base_eyes_rule = ""
    if base_include_face and base_include_eyes:
        base_eyes_rule = (
            "- For anatomy_base, include finished eyes on the base body and match their placement, shape, and stylization to the source.\n"
        )
    elif base_include_face:
        base_eyes_rule = (
            "- For anatomy_base, keep the face but do not include finished eyes, eyeballs, pupils, lashes, or painted eye detail.\n"
        )
    eye_part_rule = (
        "- Include one separate reusable Eyeball part with category face_feature. It must be exactly one isolated spherical eyeball asset only: sclera, iris, pupil, cornea highlight, and painted surface detail. Do not include eyelids, eyelashes, skin, brow, eye socket, tear duct, face crop, head, or surrounding flesh.\n"
        if include_eye_part
        else ""
    )
    return (
        "Look at the provided master character reference image and plan separate asset extractions for a 3D game asset workflow.\n\n"
        "Return JSON only. Do not include markdown fences or commentary.\n\n"
        "Schema:\n"
        "{\n"
        "  \"parts\": [\n"
        "    {\n"
        "      \"id\": \"short_stable_slug\",\n"
        "      \"display_name\": \"Human readable part name\",\n"
        "      \"category\": \"anatomy_base | hair | clothing | armor | accessory | weapon | prop | face_feature\",\n"
        "      \"priority\": 1,\n"
        "      \"normalized_bbox\": [0.0, 0.0, 1.0, 1.0],\n"
        "      \"extraction_prompt\": \"Specific instruction for isolating only this part from the master reference image\"\n"
        "    }\n"
        "  ]\n"
        "}\n\n"
        "Planning rules:\n"
        "- Include one anatomy_base part first when a body/base mesh reference is visible or inferable.\n"
        "- For anatomy_base, the extraction_prompt must ask for the body/base mesh only, not a dressed character.\n"
        "- For anatomy_base, include body-type hints in the extraction_prompt, such as short, squat, stocky, stout, bulky, tiny, chibi, elderly, broad, or slim when visible or inferable from the silhouette.\n"
        "- For anatomy_base, explicitly remove all hair, beard, cloak, robe, hood, hat, tunic, boots, belts, weapons, props, accessories, fabric, and costume details.\n"
        "- For anatomy_base, request a smooth non-explicit base mesh body reference with no explicit sexual detail and no censor bars, stickers, blur, fabric panels, or added coverings.\n"
        f"{base_face_rule}"
        f"{base_eyes_rule}"
        "- Do not create separate face-detail parts such as nose, mouth, cheeks, ears, brows, or generic face-detail sheets.\n"
        "- Facial structure belongs on anatomy_base, not as a separate extracted part.\n"
        "- Include hair and facial hair as their own separate part, never attached to clothing.\n"
        "- Include each major garment, armor piece, weapon, carried prop, pouch, belt, or accessory that would matter for 3D asset creation.\n"
        f"{eye_part_rule}"
        "- Do not include scenery, ground shadows, background decorations, labels, or duplicate variants.\n"
        "- Keep the list practical and cost-aware: prefer the most important 4 to 8 parts.\n"
        "- Use normalized_bbox values in x_min, y_min, x_max, y_max order, from 0 to 1, estimating the source-image location.\n"
        "- Each extraction_prompt must ask for exactly one isolated target item and must explicitly remove unrelated body parts, heads, mannequins, labels, extra objects, and background elements.\n\n"
        f"Global extraction guidance to incorporate: {guidance}\n\n"
        "Critical output constraints:\n"
        "- Output valid JSON only.\n"
        "- No markdown fences, no prose, no explanations.\n"
        "- No duplicate parts.\n"
        "- No combined multi-item parts.\n"
        "- Put the most important exclusions inside each extraction_prompt at the end."
    )


def _extract_json_payload(text):
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE).strip()
        raw = re.sub(r"\s*```$", "", raw).strip()
    try:
        return json.loads(raw)
    except Exception:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            return json.loads(raw[start:end + 1])
        raise


def _slugify_part_id(value, fallback):
    slug = re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower()).strip("_")
    return slug or fallback


def _normalized_bbox(value):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return []
    normalized = []
    for item in value:
        try:
            normalized.append(max(0.0, min(1.0, float(item))))
        except Exception:
            return []
    return normalized


def _normalize_part_plan(raw_plan, *, max_parts=8):
    if not isinstance(raw_plan, dict):
        raise RuntimeError("Planner JSON must be an object with a parts list.")
    raw_parts = raw_plan.get("parts")
    if not isinstance(raw_parts, list):
        raise RuntimeError("Planner JSON did not contain a parts list.")

    parts = []
    seen = set()
    for index, raw_part in enumerate(raw_parts, start=1):
        if not isinstance(raw_part, dict):
            continue
        display_name = str(raw_part.get("display_name") or raw_part.get("name") or "").strip()
        category = str(raw_part.get("category") or "part").strip().lower()
        fallback_id = f"part_{index:02d}"
        part_id = _slugify_part_id(raw_part.get("id") or display_name, fallback_id)
        if part_id in seen:
            part_id = f"{part_id}_{index:02d}"
        seen.add(part_id)
        if not display_name:
            display_name = part_id.replace("_", " ").title()
        extraction_prompt = str(raw_part.get("extraction_prompt") or raw_part.get("prompt") or "").strip()
        if not extraction_prompt:
            extraction_prompt = (
                f"Extract only the {display_name}. Remove all unrelated body parts, heads, mannequins, "
                "labels, extra objects, and background elements."
            )
        try:
            priority = int(raw_part.get("priority") or index)
        except Exception:
            priority = index
        parts.append(
            {
                "id": part_id,
                "display_name": display_name,
                "category": category,
                "priority": priority,
                "symmetry": bool(raw_part.get("symmetry", False)),
                "normalized_bbox": _normalized_bbox(raw_part.get("normalized_bbox")),
                "extraction_prompt": extraction_prompt,
            }
        )

    if not parts:
        raise RuntimeError("Planner did not identify any extractable parts.")

    parts.sort(key=lambda item: (item.get("priority", 999), item.get("id", "")))
    return {
        "parts": parts[: max(1, int(max_parts or 8))],
    }


def _part_plan_from_json_text(plan_json):
    try:
        return _normalize_part_plan(json.loads(plan_json or "{}"), max_parts=999)
    except Exception:
        return {"parts": []}


def _part_plan_summary(plan_json):
    parts = _part_plan_from_json_text(plan_json).get("parts", [])
    return [part.get("display_name") or part.get("id") or "Part" for part in parts]


def _part_mentions_eye(part):
    identifier = " ".join(
        str(part.get(key) or "").strip().lower()
        for key in ("id", "display_name", "category", "extraction_prompt")
    )
    return bool(re.search(r"\b(?:eye|eyes|eyeball|eyeballs|iris|pupil)\b", identifier))


def _part_is_face_detail(part):
    category = (part.get("category") or "").strip().lower()
    identifier = " ".join(
        str(part.get(key) or "").strip().lower()
        for key in ("id", "display_name", "extraction_prompt")
    )
    if any(word in identifier for word in ("beard", "moustache", "mustache", "facial hair", "hair")):
        return False
    if category == "face_feature":
        return True
    return any(
        word in identifier
        for word in (
            " face ",
            "facial",
            "face detail",
            "facial detail",
            "nose",
            "mouth",
            "lip",
            "eyebrow",
            "brow",
            "ear",
            "cheek",
            "jaw",
        )
    )


def _forced_anatomy_base_part_payload(*, priority=1):
    return {
        "id": "anatomy_base",
        "display_name": "Anatomy Base",
        "category": "anatomy_base",
        "priority": int(priority or 1),
        "symmetry": False,
        "normalized_bbox": [],
        "extraction_prompt": (
            "Extract the anatomy base body only as a clean reusable base mesh reference. "
            "Infer it from the source character's silhouette and costume volume. "
            "Keep the source body type and proportions. "
            "Remove all clothing, hair, beard, accessories, weapons, props, and costume remnants."
        ),
    }


def _single_eye_part_payload(*, priority=2, normalized_bbox=None, symmetry=False):
    return {
        "id": "eyeball",
        "display_name": "Eyeball",
        "category": "face_feature",
        "priority": int(priority or 2),
        "symmetry": bool(symmetry),
        "normalized_bbox": _normalized_bbox(normalized_bbox or []),
        "extraction_prompt": (
            "Create exactly one isolated spherical eyeball asset inferred from the source character. "
            "Show only the eyeball itself: sclera, iris, pupil, cornea highlight, and subtle painted surface detail. "
            "Match the source iris design, pupil treatment, sclera treatment, highlights, color, and rendering style. "
            "Do not include eyelids, eyelashes, skin, brow, eye socket, tear duct, surrounding flesh, makeup, face crop, head, or any anatomical tissue outside the eyeball. "
            "Center the single eyeball on a plain light background as a clean reusable 3D asset reference."
        ),
    }


def _ensure_required_part_plan_entries(plan, snapshot):
    parts = list((plan or {}).get("parts") or [])
    parts = [part for part in parts if not _part_is_face_detail(part)]
    anatomy_parts = [
        part for part in parts
        if (part.get("category") or "").strip().lower() == "anatomy_base"
        or "anatomy" in " ".join(str(part.get(key) or "").lower() for key in ("id", "display_name"))
        or "base body" in " ".join(str(part.get(key) or "").lower() for key in ("id", "display_name", "extraction_prompt"))
    ]
    if anatomy_parts:
        best_base = min(anatomy_parts, key=lambda item: int(item.get("priority") or 999))
        forced_base = _forced_anatomy_base_part_payload(priority=best_base.get("priority") or 1)
        parts = [part for part in parts if part not in anatomy_parts]
        parts.append(forced_base)
    else:
        parts.append(_forced_anatomy_base_part_payload(priority=1))

    if snapshot.get("part_include_eye_part"):
        eye_parts = [part for part in parts if _part_mentions_eye(part)]
        preserved_eye = None
        if eye_parts:
            best_eye = min(eye_parts, key=lambda item: int(item.get("priority") or 999))
            preserved_eye = _single_eye_part_payload(
                priority=best_eye.get("priority") or 2,
                normalized_bbox=best_eye.get("normalized_bbox") or [],
                symmetry=best_eye.get("symmetry", False),
            )
            parts = [part for part in parts if not _part_mentions_eye(part)]
        else:
            insert_priority = 2 if any((part.get("category") or "") == "anatomy_base" for part in parts) else 1
            preserved_eye = _single_eye_part_payload(priority=insert_priority)
        parts.append(preserved_eye)
    return _normalize_part_plan({"parts": parts}, max_parts=snapshot.get("part_extraction_max_parts", 8))


def _sync_part_extraction_items_from_plan(state):
    parts = _part_plan_from_json_text(getattr(state, "part_extraction_plan_json", "")).get("parts", [])
    try:
        state.part_extraction_parts.clear()
        for part in parts:
            item = state.part_extraction_parts.add()
            item.selected = True
            item.symmetry = bool(part.get("symmetry", False))
            item.part_id = str(part.get("id") or "").strip()
            item.display_name = str(part.get("display_name") or item.part_id or "Part").strip()
            item.category = str(part.get("category") or "").strip()
            item.extraction_prompt = str(part.get("extraction_prompt") or "").strip()
            bbox = part.get("normalized_bbox") or []
            item.normalized_bbox_json = json.dumps(bbox) if bbox else ""
    except Exception:
        pass


def _clear_part_extraction_plan_state(state):
    state.part_extraction_plan_json = ""
    state.part_extraction_plan_path = ""
    state.part_extraction_results_path = ""
    state.part_extraction_parts.clear()


def _set_part_extraction_source_image(state, source_image_path, status_text):
    old_path = _resolve_file_path(getattr(state, "part_extraction_source_path", "") or "")
    new_path = _resolve_file_path(source_image_path or "")
    has_existing_plan = bool((getattr(state, "part_extraction_plan_json", "") or "").strip())
    source_changed = bool(new_path and (old_path != new_path or (has_existing_plan and not old_path)))
    state.part_extraction_source_path = new_path
    state.image_path = new_path
    if source_changed:
        _clear_part_extraction_plan_state(state)
        state.imagegen_status_text = f"{status_text} Cleared old part plan."
    else:
        state.imagegen_status_text = status_text


def _part_extraction_parts_from_state(state):
    collection = getattr(state, "part_extraction_parts", None)
    if collection is None:
        return []
    if len(collection) == 0 and (getattr(state, "part_extraction_plan_json", "") or "").strip():
        _sync_part_extraction_items_from_plan(state)
    return list(collection)


def _selected_part_plan_from_state(state):
    all_items = _part_extraction_parts_from_state(state)
    selected_parts = []
    for index, item in enumerate(all_items, start=1):
        if not bool(getattr(item, "selected", True)):
            continue
        bbox = []
        bbox_json = (getattr(item, "normalized_bbox_json", "") or "").strip()
        if bbox_json:
            try:
                bbox = _normalized_bbox(json.loads(bbox_json))
            except Exception:
                bbox = []
        display_name = (getattr(item, "display_name", "") or getattr(item, "part_id", "") or f"Part {index}").strip()
        selected_parts.append(
            {
                "id": (getattr(item, "part_id", "") or _slugify_part_id(display_name, f"part_{index:02d}")).strip(),
                "display_name": display_name,
                "category": (getattr(item, "category", "") or "part").strip(),
                "priority": len(selected_parts) + 1,
                "symmetry": bool(getattr(item, "symmetry", False)),
                "normalized_bbox": bbox,
                "extraction_prompt": (getattr(item, "extraction_prompt", "") or "").strip(),
            }
        )
    return {"parts": selected_parts, "planned_count": len(all_items)}


def _part_extraction_prompt(
    part,
    guidance,
    style_text="",
    style_label="",
    *,
    base_include_face=False,
    base_include_eyes=False,
):
    display_name = part.get("display_name") or part.get("id") or "character part"
    extraction_prompt = (part.get("extraction_prompt") or "").strip()
    category = (part.get("category") or "").strip().lower()
    guidance = (guidance or DEFAULT_PART_EXTRACTION_GUIDANCE).strip()
    style_text = (style_text or "").strip()
    style_label = (style_label or "").strip()
    style_block = ""
    if style_text:
        style_block = (
            "\n\nStyle lock:\n"
            "The extracted part must match the master image's exact visual style. "
            f"Selected style preset: {style_label or 'Custom Style'}. "
            f"Apply this style strongly: {style_text.rstrip().rstrip('.')}. "
            "This style instruction is more important than generic asset cleanup. "
            "Do not change the medium, linework, paint handling, or rendering style while isolating the item."
        )
    bbox = part.get("normalized_bbox") or []
    bbox_hint = ""
    if bbox:
        bbox_hint = f"\nEstimated source location normalized bbox: {bbox}."
    body_type_block = ""
    symmetry_block = ""
    critical_exclusions = [
        "Do not create a parts sheet, grid, lineup, collage, catalog page, or multi-item layout.",
        "Do not include labels, text, scenery, shadows, or unrelated objects.",
        "Do not reinterpret the item as a product render, 3D render, realistic render, or glossy studio object.",
    ]
    identifier = f"{part.get('id', '')} {display_name} {category}".lower()
    if category == "anatomy_base" or "anatomy" in identifier or "base body" in identifier:
        face_rule = (
            "Keep the face on the base body and match the source character's facial structure."
            if base_include_face
            else "Keep the head feature-neutral with no eyes, nose, mouth, brows, beard detail, or finished facial features."
        )
        eyes_rule = ""
        if base_include_face and base_include_eyes:
            eyes_rule = " Include finished eyes on the base body and match their placement, shape, and stylization to the source."
        elif base_include_face:
            eyes_rule = (
                " Keep the full face structure, nose, mouth, cheeks, jaw, and brow shape, but do not include finished eyes. "
                "Leave the eye area blank, unrendered, or closed with no visible eyeballs, irises, pupils, sclera, lashes, or painted eye detail."
            )
        extraction_prompt = (
            f"{extraction_prompt} "
            "This is the body/base mesh target only. Remove every costume, garment, cloak, robe, hood, hat, boot, belt, prop, staff, hair, beard, vine, and accessory. "
            "Output only the inferred smooth non-explicit body silhouette for the same character proportions. "
            f"{face_rule}{eyes_rule}"
        ).strip()
        body_type_block = (
            "\n\nAnatomy base rules:\n"
            "The target is the base body only. Do not extract or redraw clothing. "
            "Infer the base body from the master character's visible silhouette and costume volume. "
            "Preserve the source character's body type, height impression, age impression, and stylized proportions. "
            "If the source character appears short, squat, stout, round, bulky, elderly, chibi, or dwarf-like, the anatomy base must keep those proportions. "
            "Do not output a generic slim young adult, fashion figure, athletic template, or unrelated anatomy chart. "
            "Create a smooth, non-explicit, feature-neutral base mesh body reference suitable for sculpting and shape generation. "
            "No cloak, robe, hood, hat, tunic, boots, belts, gloves, hair, beard, staff, bag, vines, accessories, props, labels, scenery, fabric panels, blur, censor bars, stickers, or added coverings. "
            "Do not add underwear, shorts, modesty cloth, or costume remnants; use a simplified non-explicit surface with no explicit sexual detail. "
            f"{face_rule}{eyes_rule}"
        )
        if base_include_face and not base_include_eyes:
            critical_exclusions.extend(
                [
                    "For the anatomy base, do not show finished eyes.",
                    "No eyeballs, irises, pupils, sclera, eyelashes, eyeliner, or painted eye detail.",
                    "Do not turn the face into a blank mannequin head with no nose or mouth.",
                ]
            )
        elif not base_include_face:
            critical_exclusions.extend(
                [
                    "For the anatomy base, no finished face details.",
                    "No eyes, nose, mouth, brows, beard detail, or portrait-style facial rendering.",
                ]
            )
    if category in {"clothing", "armor"} or any(
        word in identifier for word in ("boot", "glove", "sleeve", "cloak", "robe", "hood", "pauldron")
    ):
        critical_exclusions.append(
            "Do not include a head, face, hair, full body, mannequin, or hands unless structurally necessary for readability."
        )
    if not _part_mentions_eye(part):
        critical_exclusions.extend(
            [
                "Do not add isolated eyes or eyeballs.",
                "Do not add floating facial parts.",
            ]
        )
        if not (category == "anatomy_base" and base_include_face and base_include_eyes):
            critical_exclusions.append(
                "Do not include finished eyes unless this specific part explicitly requires them."
            )
    if category == "hair" or any(word in identifier for word in ("hair", "beard", "moustache", "mustache", "brow")):
        critical_exclusions.extend(
            [
                "Do not return an eyeball, eye crop, or face crop.",
                "Do not turn this request into a portrait or facial feature extraction.",
            ]
        )
    if _part_mentions_eye(part):
        extraction_prompt = (
            "Create exactly one isolated spherical eyeball asset from the source character. "
            "Show only the eyeball itself: sclera, iris, pupil, cornea highlight, and subtle painted surface detail. "
            "Match the exact iris design, pupil treatment, sclera treatment, highlights, color, and rendering style from the source. "
            "Center the single eyeball on a plain light background as a clean reusable 3D asset reference."
        )
        critical_exclusions.extend(
            [
                "Show one single spherical eyeball only.",
                "Do not include both eyes.",
                "Do not include eyelids, eyelashes, skin, brow, eye socket, tear duct, surrounding flesh, makeup, or any anatomical tissue outside the eyeball.",
                "Do not include a full face, a full head, hair, brows, nose, cheeks, ears, or forehead.",
                "Do not return an eye-region crop; return one clean isolated eyeball asset.",
            ]
        )
    if bool(part.get("symmetry", False)):
        symmetry_block = (
            "\n\nSymmetry lock:\n"
            "The output must be perfectly left-right symmetrical and front-readable for easy refinement. "
            "Match silhouette, seams, trim, folds, color placement, wear, and detail density evenly on both sides. "
            "Do not introduce asymmetrical damage, drift, missing details, uneven hems, or side-to-side design differences."
        )
        if category in {"clothing", "armor"} or any(
            word in identifier for word in ("boot", "glove", "sleeve", "cloak", "robe", "hood", "pauldron")
        ):
            symmetry_block += " Treat this wearable as a symmetry-critical asset."
    return (
        "Using the provided master character reference image, create one clean isolated asset reference image.\n\n"
        f"Target part: {display_name}\n\n"
        f"Extraction instruction: {extraction_prompt}\n\n"
        f"Global guidance: {guidance}{style_block}{body_type_block}{symmetry_block}{bbox_hint}\n\n"
        "Output rules:\n"
        "- Show exactly one centered target item.\n"
        "- Preserve the original design language, media style, material details, color palette, and scale relationship from the reference.\n"
        "- Keep the same lighting and media feel as the source image; use a simple plain background only to isolate the item.\n"
        "\nCritical exclusions:\n- "
        + "\n- ".join(critical_exclusions)
    )


def _parts_metadata_path(label):
    return os.path.splitext(_imagegen_output_path(label, ".json"))[0] + ".json"


def _decode_image_data_url(data_url):
    value = (data_url or "").strip()
    if not value:
        raise RuntimeError("OpenRouter returned an empty image URL.")
    mime_type = "image/png"
    encoded = value
    if value.startswith("data:"):
        header, separator, encoded = value.partition(",")
        if not separator:
            raise RuntimeError("OpenRouter returned a malformed image data URL.")
        mime_type = header[5:].split(";", 1)[0] or mime_type
    try:
        return mime_type, base64.b64decode(encoded)
    except Exception as exc:
        raise RuntimeError("OpenRouter returned image data that could not be decoded.") from exc


def _gemini_request_image(snapshot, prompt, output_label):
    prompt = (prompt or "").strip()
    if not prompt:
        raise RuntimeError("Enter an image-generation prompt first.")

    model_id = snapshot["model_id"]
    image_config = {
        "aspect_ratio": snapshot.get("aspect_ratio") or "1:1",
    }
    if model_id in GEMINI_IMAGE_SIZE_MODELS and snapshot.get("image_size"):
        image_config["image_size"] = snapshot["image_size"]

    message_content = prompt
    if snapshot.get("guide_image_data_url"):
        message_content = [
            {
                "type": "text",
                "text": prompt,
            },
            {
                "type": "image_url",
                "imageUrl": {
                    "url": snapshot["guide_image_data_url"],
                },
            },
        ]

    payload = {
        "model": model_id,
        "messages": [
            {
                "role": "user",
                "content": message_content,
            }
        ],
        "modalities": ["image", "text"],
        "stream": False,
        "image_config": image_config,
    }
    headers = {
        "Authorization": f"Bearer {snapshot['api_key']}",
    }
    _, _, body = _http_call(
        "POST",
        f"{OPENROUTER_API_ROOT}/chat/completions",
        payload=payload,
        headers=headers,
        timeout=1800,
    )
    try:
        detail = json.loads(body.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("Gemini returned invalid JSON.") from exc

    text_parts = []
    image_urls = []
    finish_reasons = []
    native_finish_reasons = []
    for choice in detail.get("choices", []) or []:
        finish_reason = (choice.get("finish_reason") or "").strip()
        if finish_reason:
            finish_reasons.append(finish_reason)
        native_finish_reason = (choice.get("native_finish_reason") or "").strip()
        if native_finish_reason:
            native_finish_reasons.append(native_finish_reason)
        message = choice.get("message") or {}
        if message.get("content"):
            text_parts.append(str(message["content"]).strip())
        for image in message.get("images", []) or []:
            image_url = image.get("image_url") or image.get("imageUrl") or {}
            if isinstance(image_url, dict) and image_url.get("url"):
                image_urls.append(image_url["url"])

    if not image_urls:
        reason = (detail.get("error") or {}).get("message", "")
        message = "Gemini Flash did not return an image."
        if reason:
            message += f" {reason}"
        if native_finish_reasons:
            unique_native = ", ".join(dict.fromkeys(native_finish_reasons))
            message += f" Provider finish reason: {unique_native}."
        elif finish_reasons:
            unique_finish = ", ".join(dict.fromkeys(finish_reasons))
            message += f" Finish reason: {unique_finish}."
        if text_parts:
            message += f" Response: {' '.join(text_parts)[:500]}"
        raise RuntimeError(message)

    saved_outputs = []
    total_images = len(image_urls)
    for image_index, image_url in enumerate(image_urls, start=1):
        mime_type, image_bytes = _decode_image_data_url(image_url)
        item_label = output_label if total_images == 1 else f"{output_label}-{image_index}"
        output_path = _imagegen_output_path(item_label, _gemini_image_suffix(mime_type))
        with open(output_path, "wb") as handle:
            handle.write(image_bytes)

        metadata = {
            "provider": "gemini",
            "model": model_id,
            "prompt": prompt,
            "aspect_ratio": snapshot.get("aspect_ratio") or "1:1",
            "image_size": snapshot.get("image_size") or "",
            "guide_image_path": snapshot.get("guide_image_path") or "",
            "mime_type": mime_type,
            "image_index": image_index,
            "image_count": total_images,
            "text": [part for part in text_parts if part],
            "response": _gemini_safe_response_for_metadata(detail),
        }
        metadata_path = os.path.splitext(output_path)[0] + ".json"
        with open(metadata_path, "w", encoding="utf-8") as handle:
            json.dump(metadata, handle, indent=2)
        saved_outputs.append((output_path, metadata_path))

    return saved_outputs


def _export_selected_mesh_file(context, export_format="glb", destination_path=""):
    selected_meshes = [obj for obj in context.selected_objects if obj.type == "MESH"]
    if not selected_meshes:
        raise RuntimeError("Select a mesh object first.")

    active_object = context.active_object
    source_object = active_object if active_object in selected_meshes else selected_meshes[0]
    export_format = (export_format or "glb").strip().lower()
    if export_format not in {"glb", "obj"}:
        raise RuntimeError(f"Unsupported mesh export format: {export_format}")
    if destination_path:
        export_path = destination_path
        os.makedirs(os.path.dirname(export_path), exist_ok=True)
    else:
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=f".{export_format}")
        temp_file.close()
        export_path = temp_file.name
    temp_mtl = os.path.splitext(export_path)[0] + ".mtl"
    view_layer = context.view_layer
    previous_active = view_layer.objects.active
    previous_selection = list(context.selected_objects)
    try:
        bpy.ops.object.select_all(action="DESELECT")
        source_object.select_set(True)
        view_layer.objects.active = source_object
        if export_format == "obj":
            if hasattr(bpy.ops.wm, "obj_export"):
                bpy.ops.wm.obj_export(
                    filepath=export_path,
                    export_selected_objects=True,
                    export_materials=False,
                )
            elif hasattr(bpy.ops.export_scene, "obj"):
                bpy.ops.export_scene.obj(
                    filepath=export_path,
                    use_selection=True,
                    use_materials=False,
                )
            else:
                raise RuntimeError("OBJ export is not available in this Blender build.")
        else:
            bpy.ops.export_scene.gltf(filepath=export_path, use_selection=True)
        source_root = _object_root(source_object) or source_object
        return export_path, source_root.name
    finally:
        bpy.ops.object.select_all(action="DESELECT")
        for obj in previous_selection:
            if obj and obj.name in bpy.data.objects:
                obj.select_set(True)
        if previous_active and previous_active.name in bpy.data.objects:
            view_layer.objects.active = previous_active
        if not destination_path and os.path.exists(export_path):
            os.unlink(export_path)
        if os.path.exists(temp_mtl):
            os.unlink(temp_mtl)


def _export_selected_mesh_base64(context, export_format="glb"):
    export_format = (export_format or "glb").strip().lower()
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=f".{export_format}")
    temp_file.close()
    try:
        export_path, source_name = _export_selected_mesh_file(
            context,
            export_format=export_format,
            destination_path=temp_file.name,
        )
        with open(export_path, "rb") as handle:
            return base64.b64encode(handle.read()).decode("utf-8"), source_name
    finally:
        if os.path.exists(temp_file.name):
            os.unlink(temp_file.name)


def _trellis_image_source_path(state, *, allow_front_fallback=False):
    if state.image_path.strip():
        return state.image_path
    if allow_front_fallback and state.mv_front.strip():
        return state.mv_front
    return ""


def _selected_texture_service_key(state):
    choice = (getattr(state, "texture_backend", "2MV") or "2MV").strip().upper()
    if choice == "TRELLIS":
        return "trellis"
    return "2mv"


def _build_shape_payload(state):
    if _selected_3d_service_key(state) == "trellis":
        if state.shape_workflow != "IMAGE":
            raise RuntimeError("TRELLIS shape generation currently uses one image only.")
        image_path = _trellis_image_source_path(state, allow_front_fallback=False)
        if not image_path.strip():
            raise RuntimeError("Select one source image for TRELLIS shape generation.")
        return {
            "image": _file_to_base64(image_path),
            "pipeline_type": getattr(state, "trellis_pipeline_type", "512"),
            "seed": _trellis_seed_value(state),
            "remove_background": state.auto_remove_background,
            "texture": state.shape_generate_texture,
            "max_num_tokens": int(getattr(state, "trellis_max_tokens", 49152)),
            "texture_size": int(getattr(state, "trellis_texture_size", "2048")),
            "decimation_target": int(getattr(state, "trellis_decimation_target", 500000)),
            "ss_sampling_steps": int(getattr(state, "trellis_ss_sampling_steps", 12)),
            "ss_guidance_strength": float(getattr(state, "trellis_ss_guidance_strength", 7.5)),
            "ss_guidance_rescale": float(getattr(state, "trellis_ss_guidance_rescale", 0.7)),
            "ss_guidance_interval_start": float(getattr(state, "trellis_ss_guidance_interval_start", 0.6)),
            "ss_guidance_interval_end": float(getattr(state, "trellis_ss_guidance_interval_end", 1.0)),
            "ss_rescale_t": float(getattr(state, "trellis_ss_rescale_t", 5.0)),
            "shape_sampling_steps": int(getattr(state, "trellis_shape_sampling_steps", 12)),
            "shape_guidance_strength": float(getattr(state, "trellis_shape_guidance_strength", 7.5)),
            "shape_guidance_rescale": float(getattr(state, "trellis_shape_guidance_rescale", 0.5)),
            "shape_guidance_interval_start": float(getattr(state, "trellis_shape_guidance_interval_start", 0.6)),
            "shape_guidance_interval_end": float(getattr(state, "trellis_shape_guidance_interval_end", 1.0)),
            "shape_rescale_t": float(getattr(state, "trellis_shape_rescale_t", 3.0)),
            "tex_sampling_steps": int(getattr(state, "trellis_tex_sampling_steps", 12)),
            "tex_guidance_strength": float(getattr(state, "trellis_tex_guidance_strength", 1.0)),
            "tex_guidance_rescale": float(getattr(state, "trellis_tex_guidance_rescale", 0.0)),
            "tex_guidance_interval_start": float(getattr(state, "trellis_tex_guidance_interval_start", 0.6)),
            "tex_guidance_interval_end": float(getattr(state, "trellis_tex_guidance_interval_end", 0.9)),
            "tex_rescale_t": float(getattr(state, "trellis_tex_rescale_t", 3.0)),
        }

    payload = {
        "octree_resolution": state.mesh_detail,
        "num_inference_steps": state.detail_passes,
        "guidance_scale": state.reference_strength,
        "remove_background": state.auto_remove_background,
        "texture": state.shape_generate_texture,
    }

    if state.shape_workflow == "TEXT":
        prompt = state.prompt.strip()
        if not prompt:
            raise RuntimeError("Enter a text prompt first.")
        payload["text"] = prompt
        return payload

    if state.shape_workflow == "IMAGE":
        payload["image"] = _file_to_base64(state.image_path)
        if state.prompt.strip():
            payload["text"] = state.prompt.strip()
        return payload

    if state.shape_workflow == "MULTIVIEW":
        views = {
            "mv_image_front": state.mv_front,
            "mv_image_back": state.mv_back,
            "mv_image_left": state.mv_left,
            "mv_image_right": state.mv_right,
        }
        encoded = {}
        for key, raw_path in views.items():
            if raw_path.strip():
                encoded[key] = _file_to_base64(raw_path)
        if "mv_image_front" not in encoded:
            raise RuntimeError("A front image is required for multiview mode.")
        payload.update(encoded)
        if state.prompt.strip():
            payload["text"] = state.prompt.strip()
        return payload

    raise RuntimeError(f"Unsupported shape workflow: {state.shape_workflow}")


def _build_texture_payload(state, mesh_b64, mesh_format="glb"):
    if not mesh_b64:
        raise RuntimeError("Selected mesh export failed.")

    payload = {
        "mesh": mesh_b64,
        "mesh_format": mesh_format,
        "texture": True,
        "remove_background": state.auto_remove_background,
    }
    payload.update(_texture_option_payload(state, include_use_remesh=True))

    texture_service_key = _selected_texture_service_key(state)
    texture_caps = _service_capabilities_from_summary(state, texture_service_key)
    if texture_service_key == "trellis":
        image_path = _trellis_image_source_path(state, allow_front_fallback=True)
        if not image_path.strip():
            raise RuntimeError("TRELLIS texturing needs one guidance image.")
        payload["image"] = _file_to_base64(image_path)
        return payload

    if not bool(texture_caps.get("multiview", False)):
        raise RuntimeError("The selected texture backend does not support multiview guidance.")
    views = {
        "mv_image_front": state.mv_front,
        "mv_image_back": state.mv_back,
        "mv_image_left": state.mv_left,
        "mv_image_right": state.mv_right,
    }
    encoded = {}
    for key, raw_path in views.items():
        if raw_path.strip():
            encoded[key] = _file_to_base64(raw_path)
    if "mv_image_front" not in encoded:
        raise RuntimeError("A front MV image is required for Hunyuan 2mv texturing.")
    payload.update(encoded)

    return payload


def _texture_option_payload(state, include_use_remesh=False):
    selected_service = _selected_3d_service_key(state)
    if selected_service == "trellis":
        return {
            "texture_resolution": int(getattr(state, "trellis_texture_resolution", 1024)),
            "texture_size": int(getattr(state, "trellis_texture_size", 2048)),
            "seed": _trellis_seed_value(state),
            "decimation_target": int(getattr(state, "trellis_decimation_target", 500000)),
            "tex_sampling_steps": int(getattr(state, "trellis_tex_sampling_steps", 12)),
            "tex_guidance_strength": float(getattr(state, "trellis_tex_guidance_strength", 1.0)),
            "tex_guidance_rescale": float(getattr(state, "trellis_tex_guidance_rescale", 0.0)),
            "tex_guidance_interval_start": float(getattr(state, "trellis_tex_guidance_interval_start", 0.6)),
            "tex_guidance_interval_end": float(getattr(state, "trellis_tex_guidance_interval_end", 0.9)),
            "tex_rescale_t": float(getattr(state, "trellis_tex_rescale_t", 3.0)),
        }

    payload = {
        "face_count": int(state.texture_face_limit),
    }

    payload["texture_resolution"] = int(state.texture_resolution_2mv)

    return payload


def _summarize_server_info(info):
    backend_name = str(info.get("backend", "")).strip()
    if backend_name == "Nymphs2D2" or "supported_modes" in info:
        configured_model_id = info.get("configured_model_id", "unknown")
        loaded_model_id = info.get("loaded_model_id") or "not loaded"
        device = info.get("device", "unknown")
        dtype = info.get("dtype", "unknown")
        supported_modes = info.get("supported_modes") or []
        modes_text = ",".join(str(mode) for mode in supported_modes) if supported_modes else "none"
        extra = info.get("extra") or {}
        runtime = extra.get("runtime") or extra.get("configured_runtime") or "standard"
        runtime_text = str(runtime)
        if runtime == "nunchaku":
            rank = extra.get("nunchaku_rank")
            precision = extra.get("nunchaku_precision") or "auto"
            runtime_text = f"nunchaku r{rank} {precision}" if rank else f"nunchaku {precision}"
        return (
            f"Z-Image | cfg={configured_model_id} | loaded={loaded_model_id} | "
            f"{device}/{dtype} | runtime={runtime_text} | modes={modes_text}"
        )

    status = info.get("status", "unknown")
    model_path = info.get("model_path", "unknown")
    subfolder = info.get("subfolder", "unknown")
    capabilities = _server_capabilities_from_info(info)
    return (
        f"{status} | {model_path} | {subfolder} | "
        f"shape={capabilities['shape']} | tex={capabilities['texture']} | "
        f"retexture={capabilities['retexture']} | mv={capabilities['multiview']} | "
        f"text={capabilities['text']}"
    )


def _backend_family(info):
    backend_name = str(info.get("backend", "")).strip().lower()
    if backend_name == "nymphs2d2":
        return "Nymphs2D2"
    if backend_name == "trellis.2":
        return "TRELLIS.2"
    blob = f"{info.get('model_path', '')} {info.get('subfolder', '')}".lower()
    if "trellis" in blob:
        return "TRELLIS.2"
    if "2mv" in blob or "mv" in blob:
        return "2mv"
    if "hunyuan3d-2" in blob:
        return "2.0"
    return "Unknown"


def _server_capabilities_from_info(info):
    family = _backend_family(info)
    texture_only = bool(info.get("texture_only", False))
    text_enabled = bool(info.get("enable_t23d", False))
    retexture_enabled = bool(info.get("mesh_retexture", False))

    if family in {"2mv", "2.0"}:
        texture_enabled = bool(info.get("enable_tex", False))
        if not retexture_enabled:
            retexture_enabled = texture_enabled
        return {
            "family": family,
            "shape": not texture_only,
            "texture": texture_enabled,
            "retexture": retexture_enabled,
            "multiview": True,
            "text": text_enabled,
            "texture_only": texture_only,
        }

    if family == "TRELLIS.2":
        texture_enabled = bool(info.get("enable_tex", True))
        return {
            "family": family,
            "shape": not texture_only,
            "texture": texture_enabled,
            "retexture": retexture_enabled,
            "multiview": False,
            "text": False,
            "texture_only": texture_only,
        }

    if family == "Nymphs2D2":
        return {
            "family": family,
            "shape": False,
            "texture": False,
            "retexture": False,
            "multiview": False,
            "text": False,
            "texture_only": False,
        }

    return {
        "family": family,
        "shape": not texture_only,
        "texture": False,
        "retexture": retexture_enabled,
        "multiview": False,
        "text": text_enabled,
        "texture_only": texture_only,
    }


def _fallback_server_capabilities(state):
    family = _launch_backend_label(state)
    if family == "TRELLIS.2":
        return {
            "family": family,
            "shape": True,
            "texture": True,
            "retexture": False,
            "multiview": False,
            "text": False,
            "texture_only": False,
        }
    if family == "Nymphs2D2":
        return {
            "family": family,
            "shape": False,
            "texture": False,
            "retexture": False,
            "multiview": False,
            "text": False,
            "texture_only": False,
        }
    return {
        "family": family,
        "shape": True,
        "texture": bool(state.launch_texture_support),
        "retexture": bool(state.launch_texture_support),
        "multiview": True,
        "text": False,
        "texture_only": False,
    }


def _service_capabilities_from_summary(state, service_key):
    summary = (_service_get(state, service_key, "backend_summary", "Unavailable") or "").strip()
    fallback = {
        "2mv": {
            "family": "2mv",
            "shape": True,
            "texture": bool(state.launch_texture_support),
            "retexture": bool(state.launch_texture_support),
            "multiview": True,
            "text": False,
            "texture_only": False,
        },
        "trellis": {
            "family": "TRELLIS.2",
            "shape": True,
            "texture": True,
            "retexture": False,
            "multiview": False,
            "text": False,
            "texture_only": False,
        },
    }.get(service_key, _fallback_server_capabilities(state))
    if not summary or summary in {"Unavailable", "Not checked"}:
        return fallback

    if "Z-Image |" in summary:
        return {
            "family": "Nymphs2D2",
            "shape": False,
            "texture": False,
            "retexture": False,
            "multiview": False,
            "text": False,
            "texture_only": False,
        }

    parsed = dict(fallback)
    for key, field in (
        ("shape", "shape"),
        ("tex", "texture"),
        ("retexture", "retexture"),
        ("mv", "multiview"),
        ("text", "text"),
    ):
        match = re.search(rf"(?:^|\|)\s*{key}=(true|false)", summary, flags=re.IGNORECASE)
        if match:
            parsed[field] = match.group(1).lower() == "true"
    if "trellis" in summary.lower():
        parsed["family"] = "TRELLIS.2"
    elif "2mv" in summary.lower():
        parsed["family"] = "2mv"
    return parsed


def _preferred_texture_mesh_format(state, caps):
    return "glb"


def _state_server_capabilities(state):
    capabilities = {
        "family": state.backend_family,
        "shape": bool(state.server_supports_shape),
        "texture": bool(state.server_supports_texture),
        "retexture": bool(state.server_supports_retexture),
        "multiview": bool(state.server_supports_multiview),
        "text": bool(state.server_supports_text),
        "texture_only": bool(state.server_texture_only),
    }
    if state.backend_summary in {"Unavailable", "Not checked", ""} and state.launch_state in {"Launching", "Running"}:
        return _fallback_server_capabilities(state)
    if capabilities["family"] == "Unknown" and state.launch_state in {"Launching", "Running"}:
        return _fallback_server_capabilities(state)
    return capabilities


def _normalized_panel_text(text):
    return " ".join((text or "").strip().lower().split())


def _scroll_text(text, window=42, step_seconds=0.6, gap="   "):
    cleaned = (text or "").strip()
    if len(cleaned) <= window:
        return cleaned
    cycle = cleaned + gap
    offset = int(time.monotonic() / step_seconds) % len(cycle)
    wrapped = cycle + cleaned + gap
    return wrapped[offset:offset + window]


def _path_leaf(raw_path):
    cleaned = (raw_path or "").strip().rstrip("\\/")
    if not cleaned:
        return ""
    leaf = ntpath.basename(cleaned) or os.path.basename(cleaned)
    if leaf:
        return leaf
    return cleaned.replace("\\", "/").rsplit("/", 1)[-1]


def _current_image_backend_label(state):
    backend = (getattr(state, "imagegen_backend", "Z_IMAGE") or "Z_IMAGE").strip()
    if backend == "GEMINI":
        return "Gemini Flash"
    return SERVICE_LABELS["n2d2"]


def _current_image_backend_detail(state):
    backend = (getattr(state, "imagegen_backend", "Z_IMAGE") or "Z_IMAGE").strip()
    if backend != "GEMINI":
        return ""
    parts = []
    model_label = _gemini_model_label(_gemini_model_id(state))
    if model_label:
        parts.append(model_label)
    aspect = (getattr(state, "gemini_aspect_ratio", "") or "").strip()
    if aspect:
        parts.append(f"Aspect {aspect}")
    if _gemini_model_id(state) in GEMINI_IMAGE_SIZE_MODELS:
        size = (getattr(state, "gemini_image_size", "") or "").strip()
        if size:
            parts.append(f"Size {size}")
    if bool(getattr(state, "gemini_use_guide_image", False)):
        guide_path = _gemini_guide_image_path(state)
        parts.append(f"Guide {_path_leaf(guide_path) or 'selected'}" if guide_path else "Guide On")
    else:
        parts.append("Guide Off")
    return " | ".join(parts)


def _format_elapsed_time(started_at):
    try:
        started = float(started_at or 0.0)
    except Exception:
        return ""
    if started <= 0.0:
        return ""
    elapsed = max(0, int(time.time() - started))
    hours, remainder = divmod(elapsed, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _format_clock_time(stamp):
    try:
        value = float(stamp or 0.0)
    except Exception:
        return ""
    if value <= 0.0:
        return ""
    return time.strftime("%H:%M:%S", time.localtime(value))


def _is_hidden_stage(stage):
    normalized = (stage or "").strip().lower()
    return normalized in {
        "",
        "completed",
        "job complete",
        "server boot complete",
        "server ready",
        "starting server",
        "launch requested",
    }


def _progress_lines(state):
    active_status = (state.task_status or "").lower()
    has_live_task = active_status in {"processing", "queued"}
    if not state.is_busy and not has_live_task:
        return "", "", ""
    if state.waiting_for_backend_progress:
        return (
            "Request Sent",
            "Waiting for backend progress...",
            "",
        )
    if active_status not in {"processing", "queued"}:
        if state.is_busy:
            return (
                "Request Sent",
                "Waiting for backend progress...",
                "",
            )
        return "", "", ""
    stage = (state.task_stage or "").strip()
    detail = (state.task_detail or state.task_message or "").strip()
    progress = (state.task_progress or "").strip()
    if _is_hidden_stage(stage):
        return (
            "Request Sent",
            "Waiting for backend progress...",
            "",
        )
    return (
        stage or "Working",
        detail,
        progress,
    )


def _service_runtime_hint(state, service_key):
    hint = (_service_get(state, service_key, "launch_detail", "") or "").strip()
    if not hint:
        return ""
    if hint in {"Server ready"}:
        return ""
    lowered = hint.lower()
    if lowered.startswith("starting "):
        return ""
    if lowered.startswith("waiting for /server_info"):
        return ""
    return hint


def _imagegen_progress_lines(state):
    active_status = (state.imagegen_task_status or "").lower()
    has_live_task = active_status in {"processing", "queued"}
    elapsed = _format_elapsed_time(getattr(state, "imagegen_started_at", 0.0))
    backend_label = _current_image_backend_label(state)
    if not state.imagegen_is_busy and not has_live_task:
        return "", "", ""
    stage = (state.imagegen_task_stage or "").strip()
    detail = (state.imagegen_task_detail or state.imagegen_status_text or "").strip()
    progress = (state.imagegen_task_progress or "").strip()
    runtime_hint = _service_runtime_hint(state, "n2d2") if backend_label == SERVICE_LABELS["n2d2"] else ""
    backend_detail = _current_image_backend_detail(state)
    generic_detail = {
        "",
        "Generating image...",
        "Waiting for image backend progress...",
        "Running image model inference...",
    }
    if runtime_hint and detail in generic_detail:
        detail = runtime_hint
    elif backend_detail and detail in generic_detail:
        detail = backend_detail
    if active_status not in {"processing", "queued"}:
        if state.imagegen_is_busy:
            progress = f"Elapsed {elapsed}" if elapsed else ""
            return "Running Inference", detail or "Waiting for image backend progress...", progress
        return "", "", ""
    if _is_hidden_stage(stage):
        progress = f"Elapsed {elapsed}" if elapsed else progress
        return "Running Inference", detail or "Waiting for image backend progress...", progress

    stage_key = stage.strip().lower()
    if stage_key in {"generating image", "generating_image"}:
        progress = f"Elapsed {elapsed}" if elapsed else ""
        return "Running Inference", detail or f"Running {backend_label} image model inference...", progress
    return stage or "Generating Image", detail, progress


def _primary_3d_runtime_is_active(state):
    service_key = _selected_3d_service_key(state)
    launch_state = _service_get(state, service_key, "launch_state", "Stopped")
    summary = (_service_get(state, service_key, "backend_summary", "Unavailable") or "").strip()
    if launch_state in {"Launching", "Running", "Stopping"}:
        return True
    if _service_summary_is_ready(summary):
        return True
    return _backend_is_alive(service_key)


def _server_panel_job_lines(state):
    if state.launch_state == "Launching":
        return (
            state.task_stage or "Launch Requested",
            state.launch_detail or "Waiting for backend startup...",
            state.task_progress or "",
        )
    if state.launch_state == "Stopping":
        return ("Stopping", state.launch_detail or "Stopping backend...", "")
    if state.waiting_for_backend_progress:
        return "Request Sent", "Waiting for backend progress...", ""
    lines = _progress_lines(state)
    if any(lines):
        return lines
    if state.is_busy:
        return "Request Sent", "Waiting for backend progress...", ""
    image_lines = _imagegen_progress_lines(state)
    if any(image_lines):
        return image_lines
    if _primary_3d_runtime_is_active(state):
        return "", "", ""
    return "", "", ""


def _server_panel_detail_text(state, progress_detail):
    if progress_detail:
        return progress_detail

    active_services = _active_service_entries(state)
    if not active_services:
        return ""

    details = []
    for service_key, service_status in active_services:
        launch_detail = (_service_get(state, service_key, "launch_detail", "") or "").strip()
        if launch_detail in {
            "",
            "Server ready",
            "Reusing existing server",
            f"{_service_display_name(service_key)} is not running.",
        }:
            continue
        if launch_detail.lower().startswith("starting "):
            details.append(f"{_service_display_name(service_key)} starting")
        elif launch_detail.lower().startswith("waiting for /server_info"):
            details.append(f"{_service_display_name(service_key)} waiting for server info")
        elif service_status in {"Launching", "Stopping"}:
            details.append(f"{_service_display_name(service_key)} {service_status.lower()}")
        else:
            details.append(f"{_service_display_name(service_key)}: {launch_detail}")
    return " | ".join(details)


def _server_status_line(state):
    backend_label = _backend_display_name(state.backend_family)
    if backend_label == "Unknown":
        backend_label = _backend_display_name(_launch_backend_label(state))
    active_status = (state.task_status or "").lower()
    image_status = (state.imagegen_task_status or "").lower()
    image_backend_label = _current_image_backend_label(state)
    active_services = _active_service_entries(state)
    if state.launch_state == "Launching":
        return f"{backend_label} Launching"
    if state.launch_state == "Stopping":
        return "Stopping"
    if active_status == "processing":
        return f"{backend_label} Busy"
    if active_status == "queued" or state.waiting_for_backend_progress:
        return f"{backend_label} Submitting"
    if image_status == "processing":
        return f"{image_backend_label} Busy"
    if image_status == "queued" or state.imagegen_is_busy:
        return f"{image_backend_label} Submitting"
    if len(active_services) > 1:
        service_states = {service_status for _service_key, service_status in active_services}
        if service_states == {"Ready"}:
            return f"{len(active_services)} Runtimes Ready"
        if service_states <= {"Ready", "Running"}:
            return f"{len(active_services)} Runtimes Active"
        if "Launching" in service_states:
            return "Runtimes Launching"
        if "Stopping" in service_states:
            return "Runtimes Stopping"
        return f"{len(active_services)} Runtimes Active"
    if len(active_services) == 1:
        service_key, service_status = active_services[0]
        return f"{_service_display_name(service_key)} {service_status}"
    return "Server stopped"


def _fallback_running_service(state):
    primary_key = _selected_3d_service_key(state)
    for service_key in SERVICE_ORDER:
        if service_key == primary_key:
            continue
        service_status = _service_status_line(state, service_key)
        summary = (_service_get(state, service_key, "backend_summary", "Unavailable") or "").strip()
        detail = (_service_get(state, service_key, "launch_detail", "") or "").strip()
        if service_status == "Stopped" and not _service_summary_is_ready(summary) and not _backend_is_alive(service_key):
            continue
        detail_text = detail or summary or service_status
        return service_key, service_status, detail_text
    return None


def _active_service_entries(state):
    entries = []
    for service_key in SERVICE_ORDER:
        launch_state = _service_get(state, service_key, "launch_state", "Stopped")
        summary = (_service_get(state, service_key, "backend_summary", "Unavailable") or "").strip()
        if launch_state == "Launching":
            service_status = "Launching"
        elif launch_state == "Stopping":
            service_status = "Stopping"
        elif _service_summary_is_ready(summary):
            service_status = "Ready"
        elif _backend_is_alive(service_key):
            service_status = "Running"
        else:
            service_status = "Stopped"
        if service_status == "Stopped" and not _service_summary_is_ready(summary) and not _backend_is_alive(service_key):
            continue
        entries.append((service_key, service_status))
    return entries


def _active_service_summaries(state):
    return [f"{_service_display_name(service_key)} {service_status}" for service_key, service_status in _active_service_entries(state)]


def _runtime_vram_estimate(state, service_key):
    flashvdm = bool(state.launch_flashvdm)
    turbo = bool(state.launch_turbo)
    if state.shape_workflow == "TEXT" and state.prompt.strip():
        input_mode = "text_prompt"
    elif state.shape_workflow == "MULTIVIEW":
        input_mode = "multiview"
    else:
        input_mode = "single_image"

    if service_key == "n2d2":
        model_id = (state.n2d2_model_id or DEFAULT_N2D2_MODEL_ID).strip()
        family = _n2d2_model_family(model_id)
        if family == "zimage":
            rank = getattr(state, "n2d2_nunchaku_rank", DEFAULT_N2D2_NUNCHAKU_RANK)
            estimate = "Low active VRAM"
            basis = f"Runtime: Nunchaku r{rank}"
            mode = "2D image generation"
            caveat = "Local r32 test on RTX 4080 SUPER used about 1.3 GiB during denoise. Keep r128 as the higher-quality option."
            return estimate, basis, mode, caveat
        estimate = "Varies by model"
        basis = f"Model: {model_id}"
        mode = "2D image generation"
        caveat = "2D image guidance depends on the configured model family."
        return estimate, basis, mode, caveat

    if service_key == "trellis":
        pipeline_type = getattr(state, "trellis_pipeline_type", "512")
        if state.shape_generate_texture:
            if pipeline_type == "512":
                return (
                    "16 GB verified",
                    "Basis: local test",
                    "Shape + texture",
                    "The official TRELLIS 512 full path completed locally on a 16 GB card in this branch.",
                )
            return (
                "16 GB uncertain",
                "Basis: local + inferred",
                "Shape + texture",
                "1024 cascade shape is proven locally, but higher TRELLIS texture modes still need more validation on 16 GB.",
            )
        if pipeline_type == "1024_cascade":
            return (
                "16 GB verified",
                "Basis: local test",
                "Shape only",
                "The TRELLIS 1024 cascade shape path completed locally on a 16 GB card in this branch.",
            )
        return (
            "16 GB verified",
            "Basis: local test",
            "Shape only",
            "The TRELLIS 512 shape path completed locally on a 16 GB card in this branch.",
        )

    texture = bool(state.launch_texture_support or state.shape_generate_texture)
    if texture:
        estimate = "16-24.5 GB total"
        basis = "Basis: official + inferred"
        mode = "Shape + texture"
        caveat = (
            "FlashVDM helps throughput, but texture mode still needs the larger VRAM budget."
            if flashvdm
            else "Texture mode is the main VRAM driver on 2mv."
        )
        return estimate, basis, mode, caveat
    if input_mode == "text_prompt":
        estimate = "Not supported"
        basis = "Basis: product decision"
        mode = "Text prompt disabled"
        caveat = "Use Z-Image for prompt-to-image, then feed image or MV guidance into the 3D backends."
        return estimate, basis, mode, caveat
    estimate = "9-12 GB"
    basis = "Basis: inferred"
    mode = "Shape only"
    caveat = "Turbo improves latency more than VRAM footprint." if turbo else "Standard 2mv shape mode."
    return estimate, basis, mode, caveat


def _runtime_guidance_entries(state):
    entries = []
    for service_key in SERVICE_ORDER:
        if not (_service_enabled(state, service_key) or _backend_is_alive(service_key)):
            continue
        estimate, basis, mode, caveat = _runtime_vram_estimate(state, service_key)
        entries.append(
            (
                service_key,
                _service_display_name(service_key),
                _service_api_root(state, service_key),
                estimate,
                basis,
                mode,
                caveat,
            )
        )
    return entries


def _active_task_snapshot(api_root):
    try:
        status_code, _, body = _http_call("GET", f"{api_root}/active_task", timeout=1.5)
        if status_code == 404:
            return {
                "status": "unsupported",
                "stage": "",
                "detail": "",
                "progress_text": "",
                "message": "",
            }
        data = json.loads(body.decode("utf-8"))
    except Exception:
        return {
            "status": "unavailable",
            "stage": "",
            "detail": "",
            "progress_text": "",
            "message": "",
        }

    stage = data.get("stage") or ""
    detail = data.get("detail") or data.get("message") or ""
    message = data.get("message") or ""

    return {
        "status": data.get("status", "idle"),
        "stage": _stage_label(stage),
        "detail": detail,
        "progress_text": _format_progress_text(
            current=data.get("progress_current"),
            total=data.get("progress_total"),
            percent=data.get("progress_percent"),
        ),
        "message": message,
    }


def _server_info_snapshot(api_root):
    try:
        return _json_call("GET", f"{api_root}/server_info", timeout=2)
    except Exception:
        return None


def _managed_backend(service_key=None):
    with PROCESS_LOCK:
        if service_key is None:
            return dict(MANAGED_BACKENDS)
        return MANAGED_BACKENDS.get(service_key)


def _managed_backend_target(service_key=None):
    with PROCESS_LOCK:
        if service_key is None:
            return dict(MANAGED_BACKEND_TARGETS)
        return MANAGED_BACKEND_TARGETS.get(service_key)


def _remember_backend(service_key, proc, target=None):
    with PROCESS_LOCK:
        MANAGED_BACKENDS[service_key] = proc
        MANAGED_BACKEND_TARGETS[service_key] = dict(target or {})


def _forget_backend(service_key=None, proc=None):
    with PROCESS_LOCK:
        if service_key is not None:
            current = MANAGED_BACKENDS.get(service_key)
            if proc is None or current is proc:
                MANAGED_BACKENDS.pop(service_key, None)
                MANAGED_BACKEND_TARGETS.pop(service_key, None)
            return

        if proc is None:
            MANAGED_BACKENDS.clear()
            MANAGED_BACKEND_TARGETS.clear()
            return

        doomed = [key for key, current in MANAGED_BACKENDS.items() if current is proc]
        for key in doomed:
            MANAGED_BACKENDS.pop(key, None)
            MANAGED_BACKEND_TARGETS.pop(key, None)


def _backend_is_alive(service_key=None):
    if service_key is None:
        return any(proc.poll() is None for proc in _managed_backend().values())
    proc = _managed_backend(service_key)
    return proc is not None and proc.poll() is None


def _shutdown_managed_backends():
    backends = _managed_backend()
    targets = _managed_backend_target()
    for service_key, proc in list(backends.items()):
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        _forget_backend(service_key=service_key, proc=proc)

        target = targets.get(service_key, {})
        stop_port = (target or {}).get("port") or "8080"
        _best_effort_stop_local(stop_port, target, service_key=service_key)


def _sync_local_api(state):
    state.api_root = _service_api_root(state, _selected_3d_service_key(state))


def _probe_gpu_host():
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.used,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
        row = result.stdout.strip().splitlines()[0]
        name, mem_total, mem_used, util = [part.strip() for part in row.split(",")]
        return {
            "name": name,
            "memory": f"{mem_used} / {mem_total} MB",
            "utilization": f"{util}%",
            "status": "Connected",
        }
    except Exception as exc:
        return {
            "name": "Unavailable",
            "memory": "Unavailable",
            "utilization": "Unavailable",
            "status": str(exc),
        }


def _shell_repo_path(raw_path):
    path = (raw_path or "").strip()
    if not path:
        return "~"
    if path.startswith("~/"):
        suffix = path[2:]
        if not suffix:
            return "$HOME"
        return f"$HOME/{shlex.quote(suffix)}"
    return shlex.quote(path)


def _resolved_user_path(raw_path, state=None):
    path = (raw_path or "").strip()
    if not path:
        return f"/home/{_resolved_wsl_user_name(state)}"
    if path == "~":
        return f"/home/{_resolved_wsl_user_name(state)}"
    if path.startswith("~/"):
        suffix = path[2:]
        if not suffix:
            return f"/home/{_resolved_wsl_user_name(state)}"
        return f"/home/{_resolved_wsl_user_name(state)}/{suffix}"
    return path


def _normalized_repo_path(raw_path, default_path, state=None, *, service_key=None):
    path = (raw_path or "").strip() or default_path
    if path not in {"", "/", "~"}:
        path = path.rstrip("/")

    if service_key != "n2d2":
        return path

    if path == "~/Nymphs2D2":
        return DEFAULT_REPO_N2D2_PATH

    if re.fullmatch(r"/home/[^/]+/Nymphs2D2", path):
        return f"/home/{_resolved_wsl_user_name(state)}/Z-Image"

    return path


def _normalize_state_repo_paths(state):
    if state is None:
        return

    normalized_n2d2 = _normalized_repo_path(
        getattr(state, "repo_n2d2_path", ""),
        DEFAULT_REPO_N2D2_PATH,
        state,
        service_key="n2d2",
    )
    if normalized_n2d2 != (getattr(state, "repo_n2d2_path", "") or "").strip():
        state.repo_n2d2_path = normalized_n2d2


def _installed_wsl_distro_items():
    cached = _transient_cache_get("wsl_distro_items", "installed", WSL_DISTRO_CACHE_TTL_SECONDS)
    if cached is not CACHE_MISS:
        WSL_DISTRO_ITEMS[:] = list(cached)
        return WSL_DISTRO_ITEMS

    names = []
    try:
        result = subprocess.run(
            ["wsl", "-l", "-q"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                name = line.replace("\0", "").strip()
                if name:
                    names.append(name)
    except Exception:
        pass

    ordered = []
    seen = set()
    for candidate in [DEFAULT_WSL_DISTRO, *names]:
        name = (candidate or "").strip()
        if not name:
            continue
        lowered = name.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        description = (
            "Managed installer distro"
            if name == DEFAULT_WSL_DISTRO
            else f"Use installed WSL distro '{name}'"
        )
        ordered.append((name, name, description))

    if not ordered:
        ordered.append((DEFAULT_WSL_DISTRO, DEFAULT_WSL_DISTRO, "Managed installer distro"))

    _transient_cache_set("wsl_distro_items", "installed", tuple(ordered), WSL_DISTRO_CACHE_TTL_SECONDS)
    WSL_DISTRO_ITEMS[:] = ordered
    return WSL_DISTRO_ITEMS


def _wsl_distro_items(_self, _context):
    return _installed_wsl_distro_items()


def _resolved_wsl_distro_name(state=None):
    if isinstance(state, dict):
        value = (state.get("wsl_distro_name", DEFAULT_WSL_DISTRO) or "").strip()
        return value or DEFAULT_WSL_DISTRO
    if state is None:
        return DEFAULT_WSL_DISTRO
    value = (getattr(state, "wsl_distro_name", DEFAULT_WSL_DISTRO) or "").strip()
    return value or DEFAULT_WSL_DISTRO


def _resolved_wsl_user_name(state=None):
    if isinstance(state, dict):
        value = (state.get("wsl_user_name", DEFAULT_WSL_USER) or "").strip()
        return value or DEFAULT_WSL_USER
    if state is None:
        return DEFAULT_WSL_USER
    value = (getattr(state, "wsl_user_name", DEFAULT_WSL_USER) or "").strip()
    return value or DEFAULT_WSL_USER


def _compose_wsl_invocation(shell, state=None):
    command = ["wsl", "-d", _resolved_wsl_distro_name(state)]
    user_name = _resolved_wsl_user_name(state)
    if user_name:
        command.extend(["-u", user_name])
    command.extend(["--", "bash", "-lc", shell])
    return command


def _compose_wsl_launch(state, service_key):
    port = _service_port(state, service_key)
    exports = (
        "export CUDA_HOME=/usr/local/cuda-13.0; "
        'export PATH="$CUDA_HOME/bin:$PATH"; '
        'export LD_LIBRARY_PATH="$CUDA_HOME/lib64:$LD_LIBRARY_PATH"; '
    )

    if service_key == "n2d2":
        repo_path = _normalized_repo_path(
            state.repo_n2d2_path,
            DEFAULT_REPO_N2D2_PATH,
            state,
            service_key="n2d2",
        )
        python_path = _resolved_user_path(state.n2d2_python_path.strip() or DEFAULT_N2D2_PYTHON_PATH, state)
        n2d2_model_id = state.n2d2_model_id.strip() or DEFAULT_N2D2_MODEL_ID
        n2d2_rank = getattr(state, "n2d2_nunchaku_rank", DEFAULT_N2D2_NUNCHAKU_RANK)
        n2d2_dtype = _n2d2_default_dtype(n2d2_model_id)
        n2d2_home = f"/home/{_resolved_wsl_user_name(state)}"
        hf_home = f"{n2d2_home}/.cache/huggingface"
        hf_cache = f"{hf_home}/hub"
        exports += (
            f"export HF_HOME={shlex.quote(hf_home)}; "
            f"export HF_HUB_CACHE={shlex.quote(hf_cache)}; "
            'export HF_HUB_DISABLE_XET=1; '
            f"export NYMPHS3D_HF_CACHE_DIR={shlex.quote(hf_cache)}; "
            'export Z_IMAGE_DEVICE="cuda"; '
            f"export Z_IMAGE_DTYPE={shlex.quote(n2d2_dtype)}; "
            f"export Z_IMAGE_MODEL_ID={shlex.quote(n2d2_model_id)}; "
            "export Z_IMAGE_RUNTIME='nunchaku'; "
            f"export Z_IMAGE_PORT={shlex.quote(port)}; "
            f"export Z_IMAGE_NUNCHAKU_RANK={shlex.quote(str(n2d2_rank))}; "
            "export Z_IMAGE_MODEL_VARIANT=''; "
        )
        parts = [
            python_path,
            "api_server.py",
            "--host",
            "0.0.0.0",
            "--port",
            port,
        ]
    elif service_key == "2mv":
        repo_path = state.repo_2mv_path.strip() or DEFAULT_REPO_2MV_PATH
        python_path = _resolved_user_path(state.python_2mv_path.strip() or DEFAULT_2MV_PYTHON_PATH, state)
        subfolder = "hunyuan3d-dit-v2-mv-turbo" if state.launch_turbo else "hunyuan3d-dit-v2-mv"
        parts = [
            python_path,
            "-u",
            "api_server_mv.py",
            "--host",
            "0.0.0.0",
            "--port",
            port,
            "--model_path",
            "tencent/Hunyuan3D-2mv",
            "--subfolder",
            subfolder,
            "--tex_model_path",
            "tencent/Hunyuan3D-2",
        ]
        if state.launch_texture_support:
            parts.append("--enable_tex")
        if state.launch_flashvdm:
            parts.append("--enable_flashvdm")
    else:
        repo_path = state.repo_trellis_path.strip() or DEFAULT_REPO_TRELLIS_PATH
        python_path = _resolved_user_path(state.trellis_python_path.strip() or DEFAULT_TRELLIS_PYTHON_PATH, state)
        parts = [
            python_path,
            "scripts/api_server_trellis.py",
            "--host",
            "0.0.0.0",
            "--port",
            port,
            "--python-path",
            python_path,
        ]

    cmd = " ".join(shlex.quote(part) for part in parts)
    if service_key == "n2d2":
        return f"{exports}cd {_shell_repo_path(repo_path)}; {cmd}"
    if service_key == "trellis":
        return f"{exports}cd {_shell_repo_path(repo_path)}; {cmd}"
    return f"{exports}cd {_shell_repo_path(repo_path)}; {cmd}"


def _stop_shell_for_service(service_key, port):
    common = f'(command -v fuser >/dev/null 2>&1 && fuser -k {port}/tcp >/dev/null 2>&1) || true; '
    if service_key == "2mv":
        return (
            common +
            f'pkill -f "api_server_mv.py --host 0.0.0.0 --port {port}" >/dev/null 2>&1 || true; '
            'pkill -f "python -u api_server_mv.py" >/dev/null 2>&1 || true; '
            'pkill -f "python api_server_mv.py" >/dev/null 2>&1 || true'
        )
    if service_key == "trellis":
        return (
            common +
            f'pkill -f "scripts/api_server_trellis.py --host 0.0.0.0 --port {port}" >/dev/null 2>&1 || true; '
            'pkill -f "python scripts/api_server_trellis.py" >/dev/null 2>&1 || true; '
            'pkill -f "python3 scripts/api_server_trellis.py" >/dev/null 2>&1 || true'
        )
    return (
        common +
        f'pkill -f "api_server.py --host 0.0.0.0 --port {port}" >/dev/null 2>&1 || true; '
        'pkill -f "Z-Image/api_server.py" >/dev/null 2>&1 || true; '
        'pkill -f "Nymphs2D2/api_server.py" >/dev/null 2>&1 || true'
    )


def _best_effort_stop_local(port=None, state=None, service_key=None):
    if service_key is None:
        for key in SERVICE_ORDER:
            _best_effort_stop_local(_service_port(state, key) if state is not None else None, state, service_key=key)
        return

    port = (port or _service_port(state, service_key) or "8080").strip() or "8080"
    shell = _stop_shell_for_service(service_key, port)
    try:
        subprocess.run(
            _compose_wsl_invocation(shell, state),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return False
    return True


def _startup_hint(line):
    cleaned = (line or "").replace("\r", "").strip()
    if not cleaned:
        return None
    cleaned = LOG_PREFIX_RE.sub("", cleaned).strip()
    if "[trellis-api]" in cleaned:
        lowered = cleaned.lower()
        if "listening on http://" in lowered:
            return "Server ready"
        if "model_root=" in lowered:
            return "Loading TRELLIS models"
        if "repo=" in lowered:
            return "Preparing TRELLIS adapter"
        if "python=" in lowered:
            return "Preparing TRELLIS runtime"
        if "\"get /server_info" in lowered:
            return None
    if cleaned.startswith("[SPARSE]"):
        return "Preparing sparse kernels"
    if "Uvicorn running on http://" in cleaned or "Application startup complete." in cleaned:
        return "Server ready"

    stage_match = STAGE_LINE.search(cleaned)
    if stage_match:
        return stage_match.group(1).replace("_", " ").title()

    progress_match = PROGRESS_LINE.search(cleaned)
    if progress_match:
        label = progress_match.group(1).strip()
        return f"{label} {progress_match.group(2)}/{progress_match.group(3)}"

    for token in ("Loading", "FlashVDM", "Texture", "Diffusion Sampling", "Volume Decoding"):
        if token in cleaned:
            return cleaned[:120]
    return None


def _backend_stdout_pump(scene_name, proc, service_key):
    stream = proc.stdout
    if stream is None:
        return
    try:
        for raw_line in stream:
            if _managed_backend(service_key) is not proc:
                break
            hint = _startup_hint(raw_line)
            if not hint:
                continue
            if hint == "Server ready":
                _emit_service_status(
                    scene_name,
                    service_key,
                    launch_state="Running",
                    launch_detail="Server ready",
                )
                state = _active_state(scene_name)
                if state is not None and service_key == _selected_3d_service_key(state):
                    _emit_status(scene_name, status_text="Local backend is ready.")
            else:
                _emit_service_status(scene_name, service_key, launch_detail=hint)
    except Exception:
        return


def _backend_lifecycle(scene_name, proc, service_key):
    exit_code = proc.wait()
    state_ref = _active_state(scene_name)
    api_root = None
    if state_ref is not None:
        try:
            api_root = _normalize_api_root(_service_api_root(state_ref, service_key))
        except Exception:
            api_root = None
    _forget_backend(service_key=service_key, proc=proc)
    if exit_code not in (0, -15) and api_root:
        info = _server_info_snapshot(api_root)
        if info is not None:
            capabilities = _server_capabilities_from_info(info)
            _emit_service_status(
                scene_name,
                service_key,
                backend_summary=_summarize_server_info(info),
                backend_family=capabilities["family"],
                server_supports_shape=capabilities["shape"],
                server_supports_texture=capabilities["texture"],
                server_supports_retexture=capabilities["retexture"],
                server_supports_multiview=capabilities["multiview"],
                server_supports_text=capabilities["text"],
                server_texture_only=capabilities["texture_only"],
                launch_state="Running",
                launch_detail="Reusing existing server",
            )
            return
    launch_state = "Stopped"
    message = "Server stopped."
    if exit_code not in (0, -15):
        message = f"Server exited with code {exit_code}."
    changes = {
        "launch_state": launch_state,
        "launch_detail": message,
        "backend_summary": "Unavailable",
    }
    if service_key in {"2mv", "trellis"}:
        changes.update(
            {
                "backend_family": "Unknown",
                "server_supports_shape": False,
                "server_supports_texture": False,
                "server_supports_retexture": False,
                "server_supports_multiview": False,
                "server_supports_text": False,
                "server_texture_only": False,
            }
        )
    _emit_service_status(scene_name, service_key, **changes)


def _background_primary_probe(scene_name):
    state = _active_state(scene_name)
    if state is None:
        return
    api_root = _normalize_api_root(state.api_root)
    try:
        info = _json_call("GET", f"{api_root}/server_info", timeout=2)
        capabilities = _server_capabilities_from_info(info)
        _emit_status(
            scene_name,
            backend_summary=_summarize_server_info(info),
            backend_family=capabilities["family"],
            server_supports_shape=capabilities["shape"],
            server_supports_texture=capabilities["texture"],
            server_supports_retexture=capabilities["retexture"],
            server_supports_multiview=capabilities["multiview"],
            server_supports_text=capabilities["text"],
            server_texture_only=capabilities["texture_only"],
            launch_state="Running" if state.launch_state != "Stopping" else state.launch_state,
            launch_detail="Server ready",
        )
    except Exception as exc:
        changes = {
            "backend_summary": "Unavailable",
            "backend_family": "Unknown",
            "server_supports_shape": False,
            "server_supports_texture": False,
            "server_supports_retexture": False,
            "server_supports_multiview": False,
            "server_supports_text": False,
            "server_texture_only": False,
        }
        if state.launch_state == "Launching" and _backend_is_alive(_selected_3d_service_key(state)):
            changes["launch_detail"] = f"Waiting for /server_info at {api_root}"
            changes["status_text"] = f"Launch probe: {exc}"
        _emit_status(scene_name, **changes)


def _background_server_probe(scene_name, service_key):
    state = _active_state(scene_name)
    if state is None:
        return
    api_root = _normalize_api_root(_service_api_root(state, service_key))
    try:
        info = _json_call("GET", f"{api_root}/server_info", timeout=2)
        capabilities = _server_capabilities_from_info(info)
        _emit_service_status(
            scene_name,
            service_key,
            backend_summary=_summarize_server_info(info),
            backend_family=capabilities["family"],
            server_supports_shape=capabilities["shape"],
            server_supports_texture=capabilities["texture"],
            server_supports_retexture=capabilities["retexture"],
            server_supports_multiview=capabilities["multiview"],
            server_supports_text=capabilities["text"],
            server_texture_only=capabilities["texture_only"],
            launch_state=(
                "Running"
                if _service_get(state, service_key, "launch_state", "Stopped") != "Stopping"
                else "Stopping"
            ),
            launch_detail="Server ready",
        )
    except Exception as exc:
        changes = {
            "backend_summary": "Unavailable",
        }
        if service_key == _selected_3d_service_key(state):
            changes.update(
                {
                    "backend_family": "Unknown",
                    "server_supports_shape": False,
                    "server_supports_texture": False,
                    "server_supports_retexture": False,
                    "server_supports_multiview": False,
                    "server_supports_text": False,
                    "server_texture_only": False,
                }
            )
        if _service_get(state, service_key, "launch_state", "Stopped") == "Launching" and _backend_is_alive(service_key):
            changes["launch_detail"] = f"Waiting for /server_info at {api_root}"
            if service_key == _selected_3d_service_key(state):
                changes["status_text"] = f"Launch probe: {exc}"
        _emit_service_status(scene_name, service_key, **changes)


def _background_active_probe(scene_name):
    state = _active_state(scene_name)
    if state is None:
        return
    api_root = _normalize_api_root(state.api_root)
    snapshot = _active_task_snapshot(api_root)
    status = (snapshot.get("status") or "").lower()
    stage = snapshot.get("stage") or ""
    detail = snapshot.get("detail") or ""
    progress_text = snapshot.get("progress_text") or ""
    message = snapshot.get("message") or ""

    if status == "completed":
        changes = {
            "task_status": "idle",
            "task_stage": "",
            "task_detail": "",
            "task_progress": "",
            "task_message": "",
        }
        if not state.is_busy:
            changes["waiting_for_backend_progress"] = False
            changes["saw_real_backend_progress"] = False
        _emit_status(scene_name, **changes)
        return

    if state.is_busy and state.waiting_for_backend_progress:
        real_progress_arrived = (
            status == "processing"
            and not _is_hidden_stage(stage)
            and (bool(stage) or bool(detail) or bool(progress_text))
        )
        if not real_progress_arrived:
            return
        _emit_status(
            scene_name,
            waiting_for_backend_progress=False,
            saw_real_backend_progress=True,
        )

    if status not in {"processing", "queued"} or _is_hidden_stage(stage):
        if not state.is_busy:
            _emit_status(
                scene_name,
                task_status="idle",
                task_stage="",
                task_detail="",
                task_progress="",
                task_message="",
            )
        return

    _emit_status(
        scene_name,
        task_status=status,
        task_stage=stage,
        task_detail=detail,
        task_progress=progress_text,
        task_message=message,
    )


def _background_imagegen_probe(scene_name):
    state = _active_state(scene_name)
    if state is None:
        return
    api_root = _normalize_api_root(_service_api_root(state, "n2d2"))
    snapshot = _active_task_snapshot(api_root)
    status = (snapshot.get("status") or "").lower()
    stage = snapshot.get("stage") or ""
    detail = snapshot.get("detail") or ""
    progress_text = snapshot.get("progress_text") or ""

    if status in {"completed", "idle"}:
        if not state.imagegen_is_busy:
            _emit_status(
                scene_name,
                imagegen_task_status="idle",
                imagegen_task_stage="",
                imagegen_task_detail="",
                imagegen_task_progress="",
            )
        return

    if status not in {"processing", "queued"}:
        if not state.imagegen_is_busy:
            _emit_status(
                scene_name,
                imagegen_task_status="idle",
                imagegen_task_stage="",
                imagegen_task_detail="",
                imagegen_task_progress="",
            )
        return

    _emit_status(
        scene_name,
        imagegen_task_status=status,
        imagegen_task_stage=stage,
        imagegen_task_detail=detail,
        imagegen_task_progress=progress_text,
    )


def _server_probe_timer():
    interval = SERVER_POLL_SECONDS
    for scene in bpy.data.scenes:
        state = getattr(scene, "nymphs_state", None)
        if state is None:
            continue
        threading.Thread(target=_background_primary_probe, args=(scene.name,), daemon=True).start()
        for service_key in SERVICE_ORDER:
            if _service_enabled(state, service_key) or _backend_is_alive(service_key):
                threading.Thread(
                    target=_background_server_probe,
                    args=(scene.name, service_key),
                    daemon=True,
                ).start()
            if _service_get(state, service_key, "launch_state", "Stopped") in {"Launching", "Stopping"}:
                interval = SERVER_POLL_FAST_SECONDS
        break
    return interval


def _active_probe_timer():
    interval = ACTIVE_POLL_IDLE_SECONDS
    for scene in bpy.data.scenes:
        state = getattr(scene, "nymphs_state", None)
        if state is None:
            continue
        threading.Thread(target=_background_active_probe, args=(scene.name,), daemon=True).start()
        if state.imagegen_is_busy or _backend_is_alive("n2d2"):
            threading.Thread(target=_background_imagegen_probe, args=(scene.name,), daemon=True).start()
        status = (state.task_status or "").lower()
        image_status = (state.imagegen_task_status or "").lower()
        if state.is_busy:
            interval = ACTIVE_POLL_DIRECT_JOB_SECONDS
        elif state.imagegen_is_busy:
            interval = ACTIVE_POLL_DIRECT_JOB_SECONDS
        elif status in {"processing", "queued"}:
            interval = ACTIVE_POLL_BUSY_SECONDS
        elif image_status in {"processing", "queued"}:
            interval = ACTIVE_POLL_BUSY_SECONDS
        elif status in {"unsupported", "unavailable"}:
            interval = ACTIVE_POLL_UNAVAILABLE_SECONDS
        break
    return interval


def _gpu_probe_timer():
    for scene in bpy.data.scenes:
        state = getattr(scene, "nymphs_state", None)
        if state is None:
            continue
        stats = _probe_gpu_host()
        state.gpu_name = stats["name"]
        state.gpu_memory = stats["memory"]
        state.gpu_utilization = stats["utilization"]
        state.gpu_status_message = stats["status"]
        break
    return GPU_REFRESH_SECONDS


def _import_result(event):
    before_names = set(bpy.data.objects.keys())
    bpy.ops.import_scene.gltf(filepath=event.mesh_path)
    imported_names = [name for name in bpy.data.objects.keys() if name not in before_names]
    imported_objects = [bpy.data.objects.get(name) for name in imported_names]
    imported_objects = [obj for obj in imported_objects if obj is not None]
    imported_roots = [obj for obj in imported_objects if obj.parent not in imported_objects]
    latest_roots = imported_roots if imported_roots else imported_objects

    for obj in latest_roots:
        _show_object_tree(obj)

    state = _active_state(event.scene_name)
    if event.hide_source and event.source_object_name:
        imported_object = bpy.data.objects.get(imported_names[0]) if imported_names else None
        source_object = bpy.data.objects.get(event.source_object_name)
        if source_object is not None and imported_object is not None:
            imported_object.location = source_object.location
            imported_object.rotation_euler = source_object.rotation_euler
            imported_object.scale = source_object.scale
        if source_object is not None:
            _hide_object_tree(source_object)

    if state is not None:
        state.is_busy = False
        state.task_status = "idle"
        state.task_stage = ""
        state.task_detail = ""
        state.task_progress = ""
        state.task_message = ""
        state.waiting_for_backend_progress = False
        state.saw_real_backend_progress = False
        state.generation_request_finished_locally = True
        state.status_text = "Mesh imported into Blender."
        if event.update_shape_result:
            state.last_result = "Imported latest result"
            state.shape_output_path = event.mesh_path
            state.shape_output_dir = os.path.dirname(event.mesh_path) if event.mesh_path else ""


def _job_worker(scene_name, api_root, payload, source_object_name):
    try:
        _, content_type, body = _http_call(
            "POST",
            f"{api_root}/generate",
            payload=payload,
            timeout=1800,
        )
        if "json" in (content_type or "").lower():
            try:
                detail = json.loads(body.decode("utf-8"))
            except Exception:
                detail = body.decode("utf-8", errors="replace")
            raise RuntimeError(f"Expected mesh bytes, got JSON: {detail}")

        output_path = _shape_output_path(source_object_name)
        with open(output_path, "wb") as handle:
            handle.write(body)

        _emit_status(scene_name, status_text="Generation finished. Importing result...")
        _emit_import(scene_name, output_path, source_object_name)
    except Exception as exc:
        _emit_status(
            scene_name,
            is_busy=False,
            task_status="idle",
            task_stage="",
            task_detail="",
            task_progress="",
            task_message="",
            waiting_for_backend_progress=False,
            saw_real_backend_progress=False,
            generation_request_finished_locally=False,
            status_text=str(exc),
        )


def _imagegen_worker(scene_name, api_root, payload, assign_first_output=False):
    try:
        payloads = list(payload) if isinstance(payload, (list, tuple)) else [payload]
        total = len(payloads)
        first_output_path = ""
        first_metadata_path = ""
        last_output_path = ""
        last_metadata_path = ""
        output_dir = ""

        for index, request_payload in enumerate(payloads, start=1):
            if total > 1:
                current_seed = request_payload.get("seed")
                seed_text = f" (seed {current_seed})" if current_seed is not None else ""
                _emit_status(
                    scene_name,
                    imagegen_task_status="processing",
                    imagegen_task_stage="Generating Variants",
                    imagegen_task_detail=f"Generating variant {index} of {total}{seed_text}...",
                    imagegen_task_progress=f"{index}/{total}",
                    imagegen_status_text=f"Generating variant {index} of {total}{seed_text}...",
                )

            detail = _json_call(
                "POST",
                f"{api_root}/generate",
                payload=request_payload,
                timeout=1800,
            )
            output_path = (detail.get("output_path") or "").strip()
            if not output_path:
                raise RuntimeError("Z-Image did not return an output path.")

            metadata_path = (detail.get("metadata_path") or "").strip()
            state = _active_state(scene_name)
            blender_output_path = _to_blender_accessible_path(state, output_path)
            blender_metadata_path = _to_blender_accessible_path(state, metadata_path)
            if not first_output_path:
                first_output_path = blender_output_path
                first_metadata_path = blender_metadata_path
            last_output_path = blender_output_path
            last_metadata_path = blender_metadata_path
            output_dir = os.path.dirname(blender_output_path) if blender_output_path else output_dir

        assigned_output_path = first_output_path if assign_first_output and first_output_path else last_output_path
        assigned_metadata_path = first_metadata_path if assign_first_output and first_metadata_path else last_metadata_path
        final_status = "Image generated and assigned to Image."
        if total > 1:
            final_status = (
                f"Generated {total} image variants. First breakout image assigned to Image."
                if assign_first_output
                else f"Generated {total} image variants. Last variant assigned to Image."
            )

        _emit_status(
            scene_name,
            imagegen_is_busy=False,
            imagegen_started_at=0.0,
            imagegen_status_text=final_status,
            imagegen_task_status="idle",
            imagegen_task_stage="",
            imagegen_task_detail="",
            imagegen_task_progress="",
            imagegen_output_path=assigned_output_path,
            imagegen_output_dir=output_dir,
            imagegen_metadata_path=assigned_metadata_path,
            image_path=assigned_output_path,
        )
    except Exception as exc:
        _emit_status(
            scene_name,
            imagegen_is_busy=False,
            imagegen_started_at=0.0,
            imagegen_status_text=str(exc),
            imagegen_task_status="idle",
            imagegen_task_stage="",
            imagegen_task_detail="",
            imagegen_task_progress="",
        )


def _gemini_imagegen_worker(scene_name, snapshot, prompts, assign_first_output=False):
    try:
        prompts = list(prompts) if isinstance(prompts, (list, tuple)) else [prompts]
        total = len(prompts)
        generated_images = 0
        first_output_path = ""
        first_metadata_path = ""
        last_output_path = ""
        last_metadata_path = ""
        output_dir = ""
        label = snapshot.get("model_label") or "Gemini Flash"

        for index, prompt in enumerate(prompts, start=1):
            progress = f"{index}/{total}" if total > 1 else ""
            detail = f"Generating variant {index} of {total}..." if total > 1 else "Generating image..."
            _emit_status(
                scene_name,
                imagegen_task_status="processing",
                imagegen_task_stage=label,
                imagegen_task_detail=detail,
                imagegen_task_progress=progress,
                imagegen_status_text=detail,
            )
            saved_outputs = _gemini_request_image(snapshot, prompt, f"gemini-{index}")
            for output_path, metadata_path in saved_outputs:
                generated_images += 1
                if not first_output_path:
                    first_output_path = output_path
                    first_metadata_path = metadata_path
                last_output_path = output_path
                last_metadata_path = metadata_path
                output_dir = os.path.dirname(output_path)

        assigned_output_path = first_output_path if assign_first_output and first_output_path else last_output_path
        assigned_metadata_path = first_metadata_path if assign_first_output and first_metadata_path else last_metadata_path
        final_status = "Gemini Flash image generated and assigned to Image."
        if generated_images > 1:
            final_status = (
                f"Generated {generated_images} Gemini Flash images. First breakout image assigned to Image."
                if assign_first_output
                else f"Generated {generated_images} Gemini Flash images. Last image assigned to Image."
            )
        _emit_status(
            scene_name,
            imagegen_is_busy=False,
            imagegen_started_at=0.0,
            imagegen_status_text=final_status,
            imagegen_task_status="idle",
            imagegen_task_stage="",
            imagegen_task_detail="",
            imagegen_task_progress="",
            imagegen_output_path=assigned_output_path,
            imagegen_output_dir=output_dir,
            imagegen_metadata_path=assigned_metadata_path,
            image_path=assigned_output_path,
        )
    except Exception as exc:
        _emit_status(
            scene_name,
            imagegen_is_busy=False,
            imagegen_started_at=0.0,
            imagegen_status_text=str(exc),
            imagegen_task_status="idle",
            imagegen_task_stage="",
            imagegen_task_detail="",
            imagegen_task_progress="",
        )


def _part_planning_worker(scene_name, snapshot):
    try:
        source_image_path = snapshot.get("guide_image_path") or ""
        guidance = snapshot.get("part_extraction_guidance") or DEFAULT_PART_EXTRACTION_GUIDANCE
        model_id = snapshot.get("part_planner_model") or GEMINI_PLANNER_MODEL_IDS["gemini_2_5_flash"]
        model_label = snapshot.get("part_planner_label") or _part_planner_model_label(model_id)
        prompt = _part_extraction_planning_prompt(
            guidance,
            base_include_face=bool(snapshot.get("part_base_include_face")),
            base_include_eyes=bool(snapshot.get("part_base_include_eyes")),
            include_eye_part=bool(snapshot.get("part_include_eye_part")),
        )
        _emit_status(
            scene_name,
            imagegen_task_status="processing",
            imagegen_task_stage="Planning Character Parts",
            imagegen_task_detail=f"{model_label} is identifying extractable assets...",
            imagegen_task_progress="",
            imagegen_status_text="Planning character parts...",
        )
        response_text, detail = _openrouter_text_from_image(
            snapshot["api_key"],
            model_id,
            snapshot["guide_image_data_url"],
            prompt,
        )
        raw_plan = _extract_json_payload(response_text)
        plan = _normalize_part_plan(raw_plan, max_parts=snapshot.get("part_extraction_max_parts", 8))
        plan = _ensure_required_part_plan_entries(plan, snapshot)
        plan_payload = {
            "source_image_path": source_image_path,
            "planner_model": model_id,
            "guidance": guidance,
            "parts": plan["parts"],
            "response_text": response_text,
            "response": _gemini_safe_response_for_metadata(detail),
        }
        plan_path = _parts_metadata_path("parts-plan")
        with open(plan_path, "w", encoding="utf-8") as handle:
            json.dump(plan_payload, handle, indent=2)
            handle.write("\n")
        plan_json = json.dumps({"parts": plan["parts"]}, indent=2)
        count = len(plan["parts"])
        _emit_status(
            scene_name,
            imagegen_is_busy=False,
            imagegen_started_at=0.0,
            imagegen_task_status="idle",
            imagegen_task_stage="",
            imagegen_task_detail="",
            imagegen_task_progress="",
            imagegen_status_text=f"Planned {count} character parts. Review, then Extract Parts.",
            imagegen_output_dir=os.path.dirname(plan_path),
            part_extraction_source_path=source_image_path,
            part_extraction_plan_json=plan_json,
            part_extraction_plan_path=plan_path,
            part_extraction_results_path="",
        )
    except Exception as exc:
        _emit_status(
            scene_name,
            imagegen_is_busy=False,
            imagegen_started_at=0.0,
            imagegen_task_status="idle",
            imagegen_task_stage="",
            imagegen_task_detail="",
            imagegen_task_progress="",
            imagegen_status_text=str(exc),
        )


def _part_extraction_worker(scene_name, snapshot, plan_json):
    try:
        plan = _part_plan_from_json_text(plan_json)
        parts = plan.get("parts", [])
        if not parts:
            raise RuntimeError("Plan parts before extracting.")
        guidance = snapshot.get("part_extraction_guidance") or DEFAULT_PART_EXTRACTION_GUIDANCE
        style_text = snapshot.get("part_extraction_style") or ""
        style_label = snapshot.get("part_extraction_style_label") or ""
        total = len(parts)
        outputs = []
        first_output_path = ""
        first_metadata_path = ""
        selected_output_path = ""
        selected_metadata_path = ""
        output_dir = ""

        for index, part in enumerate(parts, start=1):
            display_name = part.get("display_name") or part.get("id") or f"Part {index}"
            part_id = _sanitize_name_fragment(part.get("id") or display_name, fallback=f"part-{index:02d}")
            _emit_status(
                scene_name,
                imagegen_task_status="processing",
                imagegen_task_stage="Extracting Character Parts",
                imagegen_task_detail=f"Extracting {index}/{total}: {display_name}",
                imagegen_task_progress=f"{index}/{total}",
                imagegen_status_text=f"Extracting {display_name}...",
            )
            prompt = _part_extraction_prompt(
                part,
                guidance,
                style_text,
                style_label,
                base_include_face=bool(snapshot.get("part_base_include_face")),
                base_include_eyes=bool(snapshot.get("part_base_include_eyes")),
            )
            saved_outputs = _gemini_request_image(snapshot, prompt, f"part-{index:02d}-{part_id}")
            if not saved_outputs:
                raise RuntimeError(f"Gemini Flash did not return an image for {display_name}.")
            output_path, metadata_path = saved_outputs[0]
            output_dir = os.path.dirname(output_path)
            if not first_output_path:
                first_output_path = output_path
                first_metadata_path = metadata_path
            category = (part.get("category") or "").lower()
            identifier = f"{part.get('id', '')} {display_name}".lower()
            if not selected_output_path and (
                category == "anatomy_base" or "anatomy" in identifier or "base" in identifier or "body" in identifier
            ):
                selected_output_path = output_path
                selected_metadata_path = metadata_path
            outputs.append(
                {
                    "part": part,
                    "output_path": output_path,
                    "metadata_path": metadata_path,
                    "extra_output_count": max(0, len(saved_outputs) - 1),
                }
            )

        assigned_output_path = selected_output_path or first_output_path
        assigned_metadata_path = selected_metadata_path or first_metadata_path
        results_payload = {
            "source_image_path": snapshot.get("guide_image_path") or "",
            "extractor_model": snapshot.get("model_id") or "",
            "guidance": guidance,
            "style": style_text,
            "style_label": style_label,
            "outputs": outputs,
        }
        results_path = _parts_metadata_path("parts-results")
        with open(results_path, "w", encoding="utf-8") as handle:
            json.dump(results_payload, handle, indent=2)
            handle.write("\n")

        _emit_status(
            scene_name,
            imagegen_is_busy=False,
            imagegen_started_at=0.0,
            imagegen_task_status="idle",
            imagegen_task_stage="",
            imagegen_task_detail="",
            imagegen_task_progress="",
            imagegen_status_text=f"Extracted {len(outputs)} character parts. Base/body assigned to Image.",
            imagegen_output_path=assigned_output_path,
            imagegen_output_dir=output_dir,
            imagegen_metadata_path=assigned_metadata_path,
            image_path=assigned_output_path,
            part_extraction_results_path=results_path,
        )
    except Exception as exc:
        _emit_status(
            scene_name,
            imagegen_is_busy=False,
            imagegen_started_at=0.0,
            imagegen_status_text=str(exc),
            imagegen_task_status="idle",
            imagegen_task_stage="",
            imagegen_task_detail="",
            imagegen_task_progress="",
        )


def _gemini_mv_worker(scene_name, snapshot, prompts):
    try:
        assigned = {}
        metadata_paths = {}
        folder_path = ""
        label = snapshot.get("model_label") or "Gemini Flash"
        for index, (view_key, view_label, prompt) in enumerate(prompts, start=1):
            progress_text = f"{index}/4"
            _emit_status(
                scene_name,
                imagegen_task_status="processing",
                imagegen_task_stage=f"{label} {view_label} View",
                imagegen_task_detail=f"Generating {view_label.lower()} view...",
                imagegen_task_progress=progress_text,
                imagegen_status_text=f"Generating {view_label.lower()} view...",
            )
            saved_outputs = _gemini_request_image(snapshot, prompt, f"gemini-{view_key}")
            if not saved_outputs:
                raise RuntimeError(f"Gemini Flash did not return a {view_label.lower()} view image.")
            output_path, metadata_path = saved_outputs[0]
            assigned[view_key] = output_path
            metadata_paths[view_key] = metadata_path
            folder_path = os.path.dirname(output_path)

        front_path = assigned.get("front", "")
        _emit_status(
            scene_name,
            imagegen_is_busy=False,
            imagegen_started_at=0.0,
            imagegen_status_text="Gemini Flash MV set generated and assigned to multiview slots.",
            imagegen_task_status="idle",
            imagegen_task_stage="",
            imagegen_task_detail="",
            imagegen_task_progress="",
            imagegen_output_path=front_path,
            imagegen_output_dir=folder_path,
            imagegen_metadata_path=metadata_paths.get("front", ""),
            imagegen_mv_received_at=time.time(),
            imagegen_mv_received_source=label,
            image_path=front_path,
            mv_front=assigned.get("front", ""),
            mv_back=assigned.get("back", ""),
            mv_left=assigned.get("left", ""),
            mv_right=assigned.get("right", ""),
            shape_workflow="MULTIVIEW",
        )
    except Exception as exc:
        _emit_status(
            scene_name,
            imagegen_is_busy=False,
            imagegen_started_at=0.0,
            imagegen_status_text=str(exc),
            imagegen_task_status="idle",
            imagegen_task_stage="",
            imagegen_task_detail="",
            imagegen_task_progress="",
        )


def _imagegen_mv_worker(scene_name, api_root, seed):
    try:
        state = _active_state(scene_name)
        if state is None:
            raise RuntimeError("Scene state is no longer available.")

        folder_path = ""
        assigned = {}
        metadata_paths = {}
        for index, (view_key, view_label, view_phrase) in enumerate(IMAGEGEN_MV_VIEW_SPECS, start=1):
            payload = _build_imagegen_payload_for_prompt(
                state,
                prompt=_build_mv_prompt(state, view_phrase),
                seed=seed,
            )
            progress_text = f"{index}/4"
            _emit_status(
                scene_name,
                imagegen_task_status="processing",
                imagegen_task_stage=f"{view_label} View",
                imagegen_task_detail=f"Generating {view_label.lower()} view...",
                imagegen_task_progress=progress_text,
                imagegen_status_text=f"Generating {view_label.lower()} view...",
            )
            detail = _json_call(
                "POST",
                f"{api_root}/generate",
                payload=payload,
                timeout=1800,
            )
            output_path = (detail.get("output_path") or "").strip()
            if not output_path:
                raise RuntimeError(f"Z-Image did not return an output path for the {view_label.lower()} view.")
            metadata_path = (detail.get("metadata_path") or "").strip()
            state = _active_state(scene_name)
            blender_output_path = _to_blender_accessible_path(state, output_path)
            blender_metadata_path = _to_blender_accessible_path(state, metadata_path)
            assigned[view_key] = blender_output_path
            metadata_paths[view_key] = blender_metadata_path
            if blender_output_path:
                folder_path = os.path.dirname(blender_output_path)

        front_path = assigned.get("front", "")
        _emit_status(
            scene_name,
            imagegen_is_busy=False,
            imagegen_started_at=0.0,
            imagegen_status_text="MV set generated and assigned to multiview slots.",
            imagegen_task_status="idle",
            imagegen_task_stage="",
            imagegen_task_detail="",
            imagegen_task_progress="",
            imagegen_output_path=front_path,
            imagegen_output_dir=folder_path,
            imagegen_metadata_path=metadata_paths.get("front", ""),
            imagegen_mv_received_at=time.time(),
            imagegen_mv_received_source=SERVICE_LABELS["n2d2"],
            image_path=front_path,
            mv_front=assigned.get("front", ""),
            mv_back=assigned.get("back", ""),
            mv_left=assigned.get("left", ""),
            mv_right=assigned.get("right", ""),
            shape_workflow="MULTIVIEW",
            imagegen_seed=str(seed),
        )
    except Exception as exc:
        _emit_status(
            scene_name,
            imagegen_is_busy=False,
            imagegen_started_at=0.0,
            imagegen_status_text=str(exc),
            imagegen_task_status="idle",
            imagegen_task_stage="",
            imagegen_task_detail="",
            imagegen_task_progress="",
        )


class NymphsPartPlanItem(bpy.types.PropertyGroup):
    selected: BoolProperty(
        name="Selected",
        description="Include this part when Extract runs",
        default=True,
    )
    symmetry: BoolProperty(
        name="Symmetry",
        description="Force this extracted part to stay perfectly left-right symmetrical",
        default=False,
    )
    part_id: StringProperty(default="")
    display_name: StringProperty(default="")
    category: StringProperty(default="")
    extraction_prompt: StringProperty(default="")
    normalized_bbox_json: StringProperty(default="")


class NymphsV2State(bpy.types.PropertyGroup):
    api_root: StringProperty(
        name="API URL",
        description="URL of the Nymphs3D backend API",
        default="http://127.0.0.1:8080",
    )
    launch_backend: EnumProperty(
        name="Backend",
        items=(
            ("BACKEND_TRELLIS", "TRELLIS.2", "Launch the official TRELLIS adapter backend"),
            ("BACKEND_2MV", "Hunyuan 2mv", "Launch the multiview Hunyuan shape backend"),
        ),
        default="BACKEND_TRELLIS",
    )
    service_2mv_enabled: BoolProperty(name="Enable Hunyuan 2mv", default=False)
    service_2mv_port: StringProperty(
        name="Hunyuan 2mv Port",
        description="Port used to reach Hunyuan3D-2mv. Stop and restart this runtime after changing it.",
        default="8080",
    )
    service_2mv_show: BoolProperty(default=False)
    service_2mv_launch_state: StringProperty(default="Stopped")
    service_2mv_launch_detail: StringProperty(default="Hunyuan 2mv server is not running.")
    service_2mv_backend_summary: StringProperty(default="Unavailable")
    service_n2d2_enabled: BoolProperty(name="Enable Z-Image", default=False)
    service_n2d2_port: StringProperty(
        name="Z-Image Port",
        description="Port used to reach Z-Image. Stop and restart this runtime after changing it.",
        default="8090",
    )
    service_n2d2_show: BoolProperty(default=False)
    service_n2d2_launch_state: StringProperty(default="Stopped")
    service_n2d2_launch_detail: StringProperty(default="Z-Image server is not running.")
    service_n2d2_backend_summary: StringProperty(default="Unavailable")
    service_trellis_enabled: BoolProperty(name="Enable TRELLIS.2", default=False)
    service_trellis_port: StringProperty(
        name="TRELLIS.2 Port",
        description="Port used to reach TRELLIS.2. Stop and restart this runtime after changing it.",
        default="8094",
    )
    service_trellis_show: BoolProperty(default=False)
    service_trellis_launch_state: StringProperty(default="Stopped")
    service_trellis_launch_detail: StringProperty(default="TRELLIS.2 server is not running.")
    service_trellis_backend_summary: StringProperty(default="Unavailable")
    launch_shape_support: BoolProperty(
        name="Shape",
        description="Start the Hunyuan3D-2mv shape backend for image or multiview image-to-3D requests.",
        default=True,
    )
    launch_texture_support: BoolProperty(
        name="Texture",
        description="Also load Hunyuan3D-Paint texture support. Needed for shape+texture and retexture requests, but it increases startup time and VRAM use a lot.",
        default=True,
    )
    launch_turbo: BoolProperty(
        name="Turbo",
        description="Use the distilled Hunyuan3D-2mv Turbo model folder for faster multiview shape generation. Turn off only if you want the standard non-turbo model.",
        default=True,
    )
    launch_flashvdm: BoolProperty(
        name="FlashVDM",
        description="Enable Hunyuan FlashVDM acceleration for the shape pipeline. Faster shape decoding/generation path; best paired with Turbo, but it can change performance and results.",
        default=True,
    )
    launch_open_terminal: BoolProperty(
        name="Open Terminal Window",
        default=False,
    )
    launch_port: StringProperty(
        name="Port",
        default="8080",
    )
    wsl_distro_name: EnumProperty(
        name="WSL Distro",
        description="Which installed WSL distro to target for local backend launch",
        items=_wsl_distro_items,
    )
    wsl_user_name: StringProperty(
        name="WSL User",
        description="Which Linux user to use inside the selected WSL distro",
        default=DEFAULT_WSL_USER,
    )
    repo_2mv_path: StringProperty(
        name="2mv Path",
        default=DEFAULT_REPO_2MV_PATH,
    )
    python_2mv_path: StringProperty(
        name="Hunyuan Python",
        description="Python executable used to launch Hunyuan3D-2mv. Change this only if Hunyuan uses a different virtual environment.",
        default=DEFAULT_2MV_PYTHON_PATH,
    )
    repo_n2d2_path: StringProperty(
        name="Z-Image Repo Path",
        default=DEFAULT_REPO_N2D2_PATH,
    )
    n2d2_python_path: StringProperty(
        name="Z-Image Python",
        description="Python executable used to launch Z-Image. Change this only if Z-Image uses a different virtual environment.",
        default=DEFAULT_N2D2_PYTHON_PATH,
    )
    repo_trellis_path: StringProperty(
        name="TRELLIS Path",
        default=DEFAULT_REPO_TRELLIS_PATH,
    )
    trellis_python_path: StringProperty(
        name="TRELLIS Python",
        description="Python executable used to launch TRELLIS. Change this only if TRELLIS uses a different virtual environment.",
        default=DEFAULT_TRELLIS_PYTHON_PATH,
    )
    n2d2_model_preset: EnumProperty(
        name="Model Choice",
        description="Ready-to-test Z-Image setup",
        items=(
            ("zimage_nunchaku_r32", "Z-Image Nunchaku r32", "Fastest and lightest Nunchaku test"),
            ("zimage_nunchaku_r128", "Z-Image Nunchaku r128", "Higher-quality Nunchaku test"),
            ("zimage_nunchaku_r256", "Z-Image Nunchaku r256", "Highest-quality INT4 Nunchaku test"),
            ("custom", "Custom", "Manual runtime and model settings"),
        ),
        default=DEFAULT_N2D2_MODEL_PRESET,
        update=_on_n2d2_model_preset_changed,
    )
    n2d2_model_id: StringProperty(
        name="Model ID",
        description="Hugging Face model id for Z-Image",
        default=DEFAULT_N2D2_MODEL_ID,
        update=_on_n2d2_model_config_changed,
    )
    n2d2_nunchaku_rank: EnumProperty(
        name="Nunchaku Rank",
        description="Quantized rank for the experimental Nunchaku runtime",
        items=(
            ("32", "r32", "Fastest and lightest"),
            ("128", "r128", "Better quality, still practical"),
            ("256", "r256", "Slowest and INT4-only"),
        ),
        default=DEFAULT_N2D2_NUNCHAKU_RANK,
        update=_on_n2d2_model_config_changed,
    )
    n2d2_model_variant: StringProperty(
        name="Model Variant",
        description="Optional model variant such as fp16",
        default=DEFAULT_N2D2_MODEL_VARIANT,
        update=_on_n2d2_model_config_changed,
    )
    shape_workflow: EnumProperty(
        name="Shape Workflow",
        items=(
            ("IMAGE", "Image to 3D", "Generate shape from one image"),
            ("MULTIVIEW", "Multiview to 3D", "Generate shape from front/back/left/right images"),
            ("TEXT", "Text to 3D", "Generate shape from a text prompt"),
        ),
        default="IMAGE",
    )
    prompt: StringProperty(
        name="Prompt",
        description="Optional text prompt or text-only request",
        default="",
    )
    image_path: StringProperty(
        name="Image",
        description="Source image for image or retexture workflows",
        subtype="FILE_PATH",
        default="",
    )
    texture_backend: EnumProperty(
        name="Texture Backend",
        description="Choose which backend handles mesh texturing.",
        items=(
            ("TRELLIS", "TRELLIS.2", "Use TRELLIS.2 for single-image mesh texturing"),
            ("2MV", "Hunyuan 2mv", "Use Hunyuan 2mv for image or multiview-guided mesh texturing"),
        ),
        default="TRELLIS",
    )
    mv_front: StringProperty(name="Front", subtype="FILE_PATH", default="")
    mv_back: StringProperty(name="Back", subtype="FILE_PATH", default="")
    mv_left: StringProperty(name="Left", subtype="FILE_PATH", default="")
    mv_right: StringProperty(name="Right", subtype="FILE_PATH", default="")
    auto_remove_background: BoolProperty(
        name="Auto Remove Background",
        description="Remove plain backgrounds before sending the image to the 3D backend",
        default=True,
    )
    mesh_detail: IntProperty(
        name="Mesh Detail",
        description="Target shape detail for Hunyuan 2mv. Higher values can keep more form but cost more time and memory.",
        default=256,
        min=128,
        max=512,
    )
    detail_passes: IntProperty(
        name="Detail Passes",
        description="Number of Hunyuan 2mv refinement passes. Higher values are slower and can improve stability.",
        default=20,
        min=10,
        max=100,
    )
    reference_strength: FloatProperty(
        name="Reference Strength",
        description="How strongly Hunyuan 2mv follows the input guide. Higher values keep closer to the reference.",
        default=5.5,
        min=1.0,
        max=12.0,
    )
    shape_generate_texture: BoolProperty(
        name="Also Generate Texture",
        description="Request texture generation in the same shape job when the selected backend supports it",
        default=True,
    )
    texture_face_limit: IntProperty(
        name="Face Limit",
        description="Target face count for Hunyuan retexture output. Lower values are lighter, higher values preserve more geometry.",
        default=40000,
        min=1000,
        max=100000,
    )
    texture_max_views: IntProperty(
        name="Max Views",
        default=6,
        min=6,
        max=12,
    )
    texture_resolution_2mv: EnumProperty(
        name="Texture Size",
        description="Output texture size for Hunyuan texturing. Larger sizes keep more texture detail but cost more time and memory.",
        items=(
            ("256", "256 px", "Fastest 2mv texture bake, lowest quality"),
            ("512", "512 px", "Lowest VRAM and fastest 2mv texturing"),
            ("768", "768 px", "Balanced 2mv texture bake"),
            ("1024", "1024 px", "Lower VRAM and faster 2mv texturing"),
            ("1536", "1536 px", "Higher detail with a smaller jump than 2048"),
            ("2048", "2048 px", "Higher quality but heavier 2mv texturing"),
        ),
        default="2048",
    )
    texture_use_remesh: BoolProperty(
        name="Remesh Uploaded Mesh",
        description="Experimental. Rebuild the uploaded mesh before texturing. Can help some meshes but may damage others.",
        default=False,
    )
    trellis_shape_preset: EnumProperty(
        name="TRELLIS Preset",
        description="Load a TRELLIS shape preset. This updates the pipeline type, token budget, and sampler settings.",
        items=_trellis_shape_preset_items,
    )
    trellis_pipeline_type: EnumProperty(
        name="Resolution",
        items=(
            ("512", "512", "Fastest verified TRELLIS shape/texturing lane"),
            ("1024", "1024", "Direct 1024 TRELLIS lane"),
            ("1024_cascade", "1024 Cascade", "Official default higher-detail TRELLIS shape lane"),
            ("1536_cascade", "1536 Cascade", "Experimental higher-detail TRELLIS shape lane"),
        ),
        default="1024_cascade",
    )
    trellis_texture_resolution: EnumProperty(
        name="Resolution",
        description="Internal TRELLIS texture working resolution. Higher values can improve texture quality but cost more VRAM and time.",
        items=(
            ("512", "512 px", "Lightest TRELLIS texture mode"),
            ("1024", "1024 px", "Higher-detail TRELLIS texture mode"),
            ("1536", "1536 experimental", "Higher operating resolution using the 1024 texture model path"),
        ),
        default="1024",
    )
    trellis_texture_size: EnumProperty(
        name="Texture Size",
        description="Final exported texture-map size for TRELLIS. Larger maps preserve more texture detail but make the result heavier.",
        items=(
            ("1024", "1024 px", "Lower VRAM and faster export"),
            ("2048", "2048 px", "Balanced default"),
            ("4096", "4096 px", "Heavier export with more map detail"),
        ),
        default="2048",
    )
    trellis_seed: StringProperty(
        name="Seed (optional)",
        description="Leave blank for a random shape. Enter a number to repeat a result or stay closer to an earlier shape.",
        default="",
    )
    trellis_max_tokens: IntProperty(
        name="Max Tokens",
        description="How much detail TRELLIS is allowed to keep while building the scene. Higher values can preserve more structure, but cost more VRAM and time.",
        default=49152,
        min=4096,
        max=131072,
    )
    trellis_decimation_target: IntProperty(
        name="Decimation Target",
        description="Target face count after simplification. Lower values export lighter meshes, higher values keep more geometry.",
        default=500000,
        min=10000,
        max=2000000,
    )
    show_trellis_advanced_sampling: BoolProperty(
        name="Advanced TRELLIS Controls",
        description="Show the expert TRELLIS guidance knobs. Most users can leave these alone and rely on presets plus Steps and Guidance.",
        default=False,
    )
    show_trellis_stage_overrides: BoolProperty(
        name="Expert Stage Overrides",
        description="Show separate controls for TRELLIS's early coarse pass. Leave this off unless you want to tune the early structure pass separately from the main shape pass.",
        default=False,
    )
    trellis_ss_sampling_steps: IntProperty(
        name="Sparse Steps",
        description="How long TRELLIS spends on the early coarse structure pass before the main shape refinement begins. Usually only adjust this if you are using Expert Stage Overrides.",
        default=12,
        min=1,
        max=50,
    )
    trellis_ss_guidance_strength: FloatProperty(
        name="Sparse Guidance",
        description="How strongly the early coarse structure pass should follow the source image. Higher values force the rough layout to stick closer to the image.",
        default=7.5,
        min=0.0,
        max=15.0,
    )
    trellis_ss_guidance_rescale: FloatProperty(
        name="Sparse Guidance Rescale",
        description="Softens overly strong guidance in the early coarse structure pass. Leave this alone unless guidance feels too harsh or unstable.",
        default=0.7,
        min=0.0,
        max=2.0,
    )
    trellis_ss_guidance_interval_start: FloatProperty(
        name="Sparse Guidance Start",
        description="When TRELLIS starts strongly following the source image during the early coarse structure pass. Usually left alone.",
        default=0.6,
        min=0.0,
        max=1.0,
    )
    trellis_ss_guidance_interval_end: FloatProperty(
        name="Sparse Guidance End",
        description="When TRELLIS stops strongly following the source image during the early coarse structure pass. Usually left alone.",
        default=1.0,
        min=0.0,
        max=1.0,
    )
    trellis_ss_rescale_t: FloatProperty(
        name="Sparse Rescale T",
        description="Expert timing control for the early coarse structure pass. Most users should leave this at the preset value.",
        default=5.0,
        min=0.0,
        max=20.0,
    )
    trellis_shape_sampling_steps: IntProperty(
        name="Shape Steps",
        description="How long TRELLIS spends refining the main 3D shape. This is one of the main quality knobs: higher is slower and can improve stability.",
        default=12,
        min=1,
        max=50,
    )
    trellis_shape_guidance_strength: FloatProperty(
        name="Shape Guidance",
        description="How strongly the main 3D shape pass follows the source image. Higher values can improve likeness, but may make results brittle or over-constrained.",
        default=7.5,
        min=0.0,
        max=15.0,
    )
    trellis_shape_guidance_rescale: FloatProperty(
        name="Shape Guidance Rescale",
        description="Softens overly strong guidance in the main shape pass. Leave this alone unless you are deliberately tuning guidance behavior.",
        default=0.5,
        min=0.0,
        max=2.0,
    )
    trellis_shape_guidance_interval_start: FloatProperty(
        name="Shape Guidance Start",
        description="When TRELLIS starts strongly following the source image during the main shape pass. Usually left alone.",
        default=0.6,
        min=0.0,
        max=1.0,
    )
    trellis_shape_guidance_interval_end: FloatProperty(
        name="Shape Guidance End",
        description="When TRELLIS stops strongly following the source image during the main shape pass. Usually left alone.",
        default=1.0,
        min=0.0,
        max=1.0,
    )
    trellis_shape_rescale_t: FloatProperty(
        name="Shape Rescale T",
        description="Expert timing control for the main shape pass. Most users should leave this at the preset value.",
        default=3.0,
        min=0.0,
        max=20.0,
    )
    trellis_tex_sampling_steps: IntProperty(
        name="Texture Steps",
        description="How long TRELLIS spends refining textures. Higher values are slower but can improve texture stability.",
        default=12,
        min=1,
        max=50,
    )
    trellis_tex_guidance_strength: FloatProperty(
        name="Texture Guidance",
        description="How strongly the texture pass follows the source image. Higher values stick closer to the guide, lower values allow more variation.",
        default=1.0,
        min=0.0,
        max=15.0,
    )
    trellis_tex_guidance_rescale: FloatProperty(
        name="Texture Guidance Rescale",
        description="Softens overly strong guidance in the texture pass. Most users should leave this alone.",
        default=0.0,
        min=0.0,
        max=2.0,
    )
    trellis_tex_guidance_interval_start: FloatProperty(
        name="Texture Guidance Start",
        description="When TRELLIS starts strongly following the source image during the texture pass. Usually left alone.",
        default=0.6,
        min=0.0,
        max=1.0,
    )
    trellis_tex_guidance_interval_end: FloatProperty(
        name="Texture Guidance End",
        description="When TRELLIS stops strongly following the source image during the texture pass. Usually left alone.",
        default=0.9,
        min=0.0,
        max=1.0,
    )
    trellis_tex_rescale_t: FloatProperty(
        name="Texture Rescale T",
        description="Expert timing control for the texture pass. Most users should leave this at the preset value.",
        default=3.0,
        min=0.0,
        max=20.0,
    )
    show_server: BoolProperty(default=False)
    show_startup: BoolProperty(default=False)
    show_texture_settings: BoolProperty(default=False)
    show_image_generation: BoolProperty(default=False)
    show_part_extraction: BoolProperty(default=False)
    show_advanced: BoolProperty(default=False)
    show_shape: BoolProperty(default=False)
    show_texture: BoolProperty(default=False)
    imagegen_prompt_text_name: StringProperty(default="")
    part_extraction_guidance_text_name: StringProperty(default="")
    imagegen_backend: EnumProperty(
        name="Image Backend",
        description="Choose whether image prompts run on the local Z-Image server or Gemini Flash image generation through OpenRouter.",
        items=(
            ("Z_IMAGE", "Z-Image", "Use the local Z-Image backend"),
            ("GEMINI", "Gemini Flash", "Use Gemini image generation through OpenRouter"),
        ),
        default="Z_IMAGE",
    )
    openrouter_api_key: StringProperty(
        name="API",
        description="OpenRouter API key for Gemini Flash image generation. Leave blank to use OPENROUTER_API_KEY from the environment instead.",
        subtype="PASSWORD",
        default="",
        update=_on_openrouter_api_key_changed,
    )
    gemini_model: EnumProperty(
        name="Model",
        description="Gemini Flash image model to call through OpenRouter.",
        items=(
            ("gemini_2_5_flash_image", "Gemini 2.5 Flash Image", "Fast 1024px image generation through OpenRouter."),
            ("gemini_3_1_flash_image_preview", "Gemini 3.1 Flash Image", "Newer Flash image model with optional larger output sizes."),
            ("gemini_3_pro_image_preview", "Gemini 3 Pro Image", "Pro preview for more complex image instructions."),
        ),
        default="gemini_2_5_flash_image",
    )
    gemini_use_guide_image: BoolProperty(
        name="Guide Image",
        description="Send one picked image along with the prompt so Gemini can edit from or stay closer to an existing design.",
        default=False,
    )
    gemini_guide_image_path: StringProperty(
        name="Guide",
        description="Image file to send with the Gemini prompt. Use Pick to choose from the current generated-image folder.",
        subtype="FILE_PATH",
        default="",
    )
    gemini_aspect_ratio: EnumProperty(
        name="Aspect",
        description="Gemini output aspect ratio.",
        items=(
            ("1:1", "1:1", "Square"),
            ("2:3", "2:3", "Portrait"),
            ("3:2", "3:2", "Landscape"),
            ("3:4", "3:4", "Portrait"),
            ("4:3", "4:3", "Landscape"),
            ("4:5", "4:5", "Portrait"),
            ("5:4", "5:4", "Landscape"),
            ("9:16", "9:16", "Tall"),
            ("16:9", "16:9", "Wide"),
            ("21:9", "21:9", "Ultrawide"),
        ),
        default="1:1",
    )
    gemini_image_size: EnumProperty(
        name="Size",
        description="Output size for Gemini 3.1 Flash Image or Gemini 3 Pro Image. Gemini 2.5 Flash Image always uses its fixed size for the selected aspect ratio.",
        items=(
            ("1K", "1K", "Default image size"),
            ("2K", "2K", "Larger image size"),
            ("4K", "4K", "Largest image size"),
        ),
        default="1K",
    )
    imagegen_prompt: StringProperty(
        name="Prompt",
        description="What the selected image backend should create. Keep it direct and editable; use Edit for longer text.",
        default="",
    )
    imagegen_prompt_preset: EnumProperty(
        name="Subject",
        description="Managed subject prompt block shown in the visible prompt.",
        items=_imagegen_subject_preset_items,
        update=_on_imagegen_managed_prompt_changed,
    )
    imagegen_style: StringProperty(
        name="Style",
        description="Optional reusable art-direction fragment that gets injected into the generated prompt without replacing your main description.",
        default="",
    )
    imagegen_style_preset: EnumProperty(
        name="Style",
        description="Managed style prompt block shown in the visible prompt.",
        items=_imagegen_style_preset_items,
        update=_on_imagegen_managed_prompt_changed,
    )
    imagegen_saved_prompt_preset: EnumProperty(
        name="Saved Prompt",
        description="Saved full prompt to load into the visible prompt.",
        items=_imagegen_saved_prompt_items,
        update=_on_imagegen_saved_prompt_changed,
    )
    imagegen_settings_preset: EnumProperty(
        name="Generation Profile",
        description="Load a reusable Z-Image profile. Built-in profiles can switch Nunchaku rank, image size, steps, and variants.",
        items=_imagegen_settings_preset_items,
    )
    imagegen_width: IntProperty(
        name="Width",
        description="Generated image width in pixels",
        default=1024,
        min=256,
        max=1536,
    )
    imagegen_height: IntProperty(
        name="Height",
        description="Generated image height in pixels",
        default=1024,
        min=256,
        max=1536,
    )
    imagegen_steps: IntProperty(
        name="Steps",
        description="How many image-generation refinement steps to run. More steps are slower.",
        default=9,
        min=1,
        max=100,
    )
    imagegen_guidance_scale: FloatProperty(
        name="Guide",
        description="How strongly Z-Image follows the prompt. Z-Image Turbo usually works best near the default.",
        default=0.0,
        min=0.0,
        max=20.0,
    )
    imagegen_seed: StringProperty(
        name="Seed (optional)",
        description="Leave blank for a random image. Enter a number to repeat a result or generate related variants.",
        default="",
    )
    imagegen_variant_count: IntProperty(
        name="Variants",
        description="How many single-image variants Generate Image should make. Generate 4-View MV ignores this and always makes front, left, right, and back views.",
        default=1,
        min=1,
        max=8,
    )
    imagegen_generate_mv: BoolProperty(
        name="4-View MV",
        description="Generate front, left, right, and back views instead of a single image or variants.",
        default=False,
    )
    imagegen_seed_step: IntProperty(
        name="Seed Step",
        description="Seed increment for single-image variants. Generate 4-View MV uses one seed across its four view prompts.",
        default=1,
        min=1,
        max=1000000,
    )
    imagegen_started_at: FloatProperty(default=0.0)
    imagegen_is_busy: BoolProperty(default=False)
    imagegen_status_text: StringProperty(
        name="Image Generation Status",
        default="Idle",
    )
    imagegen_task_status: StringProperty(default="")
    imagegen_task_stage: StringProperty(default="")
    imagegen_task_detail: StringProperty(default="")
    imagegen_task_progress: StringProperty(default="")
    imagegen_output_path: StringProperty(
        name="Last Generated Image",
        default="",
    )
    imagegen_output_dir: StringProperty(
        name="Generated Image Folder",
        default="",
    )
    imagegen_metadata_path: StringProperty(
        name="Last Image Metadata",
        default="",
    )
    part_planner_model: EnumProperty(
        name="Model",
        description="Vision model used to identify extractable character parts before spending on image edits.",
        items=(
            ("gemini_2_5_flash", "Gemini 2.5 Flash", "Lower-cost vision planning through OpenRouter"),
            ("gemini_3_1_pro_preview", "Gemini 3.1 Pro", "Higher-quality vision planning for harder character references"),
        ),
        default="gemini_2_5_flash",
    )
    part_extraction_max_parts: IntProperty(
        name="Max Parts",
        description="Maximum parts to include in one extraction plan. This controls cost before image edits run.",
        default=8,
        min=1,
        max=16,
    )
    part_extraction_guidance: StringProperty(
        name="Guidance",
        description="Extra guidance applied to every character-part extraction request.",
        default=DEFAULT_PART_EXTRACTION_GUIDANCE,
    )
    part_extraction_style_lock: BoolProperty(
        name="Style Lock",
        description="Apply the active Style field strongly during guided part extraction.",
        default=True,
    )
    part_base_include_face: BoolProperty(
        name="Face",
        description="Keep facial features on the anatomy base extraction instead of leaving the head feature-neutral.",
        default=False,
        update=_on_part_extraction_option_changed,
    )
    part_base_include_eyes: BoolProperty(
        name="Eyes In Base",
        description="When Face is on, keep finished eyes on the anatomy base instead of leaving the eye area blank.",
        default=False,
        update=_on_part_extraction_option_changed,
    )
    part_include_eye_part: BoolProperty(
        name="Add Eyeball Part",
        description="Add one separate reusable eyeball-only extraction target.",
        default=False,
        update=_on_part_extraction_option_changed,
    )
    part_extraction_source_path: StringProperty(
        name="Source",
        default="",
    )
    part_extraction_plan_json: StringProperty(
        name="Part Plan",
        default="",
    )
    part_extraction_plan_path: StringProperty(
        name="Part Plan Metadata",
        default="",
    )
    part_extraction_results_path: StringProperty(
        name="Part Extraction Metadata",
        default="",
    )
    part_extraction_parts: CollectionProperty(type=NymphsPartPlanItem)
    imagegen_mv_received_at: FloatProperty(default=0.0)
    imagegen_mv_received_source: StringProperty(default="")
    shape_output_path: StringProperty(
        name="Last Generated Mesh",
        default="",
    )
    shape_output_dir: StringProperty(
        name="Generated Mesh Folder",
        default="",
    )
    is_busy: BoolProperty(default=False)
    status_text: StringProperty(
        name="Status",
        default="Idle",
    )
    backend_summary: StringProperty(
        name="Backend Summary",
        default="Not checked",
    )
    gpu_summary: StringProperty(
        name="GPU Summary",
        default="Not checked",
    )
    gpu_name: StringProperty(name="GPU Name", default="Unknown")
    gpu_memory: StringProperty(name="GPU Memory", default="Unavailable")
    gpu_utilization: StringProperty(name="GPU Utilization", default="Unavailable")
    gpu_status_message: StringProperty(name="GPU Status", default="Not checked")
    launch_state: StringProperty(
        name="Launch State",
        default="Stopped",
    )
    launch_detail: StringProperty(
        name="Launch Detail",
        default="No local backend launched from Blender.",
    )
    backend_family: StringProperty(default="Unknown")
    server_supports_shape: BoolProperty(default=False)
    server_supports_texture: BoolProperty(default=False)
    server_supports_retexture: BoolProperty(default=False)
    server_supports_multiview: BoolProperty(default=False)
    server_supports_text: BoolProperty(default=False)
    server_texture_only: BoolProperty(default=False)
    task_status: StringProperty(default="idle")
    task_stage: StringProperty(default="")
    task_detail: StringProperty(default="")
    task_progress: StringProperty(default="")
    task_message: StringProperty(default="")
    waiting_for_backend_progress: BoolProperty(default=False)
    saw_real_backend_progress: BoolProperty(default=False)
    generation_request_finished_locally: BoolProperty(default=False)
    last_result: StringProperty(
        name="Last Result",
        default="",
    )


def _service_display_name(service_key):
    return SERVICE_LABELS[service_key]


def _runtime_card_name(state, service_key):
    if service_key == "n2d2":
        return f"Z-Image Turbo / {_short_n2d2_runtime_label(state)}"
    if service_key == "trellis":
        return "TRELLIS.2"
    if service_key == "2mv":
        return "Hunyuan3D-2mv"
    return _service_display_name(service_key)


def _backend_display_name(label):
    mapping = {
        "2mv": SERVICE_LABELS["2mv"],
        "2.0": SERVICE_LABELS["2mv"],
        "Nymphs2D2": SERVICE_LABELS["n2d2"],
        "TRELLIS.2": SERVICE_LABELS["trellis"],
    }
    return mapping.get(label, label)


def _apply_service_state(state, service_key, **changes):
    for attr_name, value in _service_changes(service_key, **changes).items():
        setattr(state, attr_name, value)

    if service_key == _selected_3d_service_key(state):
        if "launch_state" in changes:
            state.launch_state = changes["launch_state"]
        if "launch_detail" in changes:
            state.launch_detail = changes["launch_detail"]


def _start_service_process(context, state, service_key):
    if _backend_is_alive(service_key):
        return False, f"{_service_display_name(service_key)} is already running."

    api_root = _normalize_api_root(_service_api_root(state, service_key))
    info = _server_info_snapshot(api_root)
    if info is not None:
        capabilities = _server_capabilities_from_info(info)
        _emit_service_status(
            context.scene.name,
            service_key,
            backend_summary=_summarize_server_info(info),
            backend_family=capabilities["family"],
            server_supports_shape=capabilities["shape"],
            server_supports_texture=capabilities["texture"],
            server_supports_retexture=capabilities["retexture"],
            server_supports_multiview=capabilities["multiview"],
            server_supports_text=capabilities["text"],
            server_texture_only=capabilities["texture_only"],
            launch_state="Running",
            launch_detail="Reusing existing server",
        )
        return True, f"{_service_display_name(service_key)} is already available."

    _normalize_state_repo_paths(state)
    shell = _compose_wsl_launch(state, service_key)
    port = _service_port(state, service_key)

    try:
        _best_effort_stop_local(port, state, service_key=service_key)
        popen_kwargs = {
            "text": True,
            "bufsize": 1,
        }
        if os.name == "nt":
            if state.launch_open_terminal:
                popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
                popen_kwargs["stdout"] = None
                popen_kwargs["stderr"] = None
            else:
                popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                popen_kwargs["stdout"] = subprocess.PIPE
                popen_kwargs["stderr"] = subprocess.STDOUT
        else:
            popen_kwargs["stdout"] = subprocess.PIPE
            popen_kwargs["stderr"] = subprocess.STDOUT

        proc = subprocess.Popen(_compose_wsl_invocation(shell, state), **popen_kwargs)
    except Exception as exc:
        changes = {
            "launch_state": "Stopped",
            "launch_detail": str(exc),
            "backend_summary": "Unavailable",
        }
        if service_key in {"2mv", "trellis"}:
            changes.update(
                {
                    "backend_family": "Unknown",
                    "server_supports_shape": False,
                    "server_supports_texture": False,
                    "server_supports_retexture": False,
                    "server_supports_multiview": False,
                    "server_supports_text": False,
                    "server_texture_only": False,
                }
            )
        _apply_service_state(state, service_key, **changes)
        return False, f"Launch failed: {exc}"

    _remember_backend(
        service_key,
        proc,
        {
            "service_key": service_key,
            "port": port,
            "wsl_distro_name": _resolved_wsl_distro_name(state),
            "wsl_user_name": _resolved_wsl_user_name(state),
        },
    )
    if service_key in {"2mv", "trellis"}:
        _set_3d_target_from_service(state, service_key)
    _apply_service_state(
        state,
        service_key,
        launch_state="Launching",
        launch_detail=f"Starting {_service_display_name(service_key)}...",
        backend_summary="Waiting for /server_info...",
    )

    if proc.stdout is not None:
        threading.Thread(
            target=_backend_stdout_pump,
            args=(context.scene.name, proc, service_key),
            daemon=True,
        ).start()
    threading.Thread(
        target=_backend_lifecycle,
        args=(context.scene.name, proc, service_key),
        daemon=True,
    ).start()
    return True, f"{_service_display_name(service_key)} launch requested."


def _stop_service_process(state, service_key):
    proc = _managed_backend(service_key)
    if proc is None or proc.poll() is not None:
        _best_effort_stop_local(_service_port(state, service_key), state, service_key=service_key)
        changes = {
            "launch_state": "Stopped",
            "launch_detail": f"{_service_display_name(service_key)} is not running.",
            "backend_summary": "Unavailable",
        }
        if service_key in {"2mv", "trellis"}:
            changes.update(
                {
                    "backend_family": "Unknown",
                    "server_supports_shape": False,
                    "server_supports_texture": False,
                    "server_supports_retexture": False,
                    "server_supports_multiview": False,
                    "server_supports_text": False,
                    "server_texture_only": False,
                }
            )
        _apply_service_state(state, service_key, **changes)
        return False, f"{_service_display_name(service_key)} is not running."

    try:
        proc.terminate()
    except Exception as exc:
        return False, f"Stop failed: {exc}"

    _best_effort_stop_local(_service_port(state, service_key), state, service_key=service_key)
    _apply_service_state(
        state,
        service_key,
        launch_state="Stopping",
        launch_detail=f"Stopping {_service_display_name(service_key)}...",
    )
    return True, f"{_service_display_name(service_key)} stop requested."


def _restart_n2d2_after_model_change(context, state):
    global N2D2_AUTORESTART_GUARD
    if N2D2_AUTORESTART_GUARD:
        return
    if context is None or getattr(context, "scene", None) is None:
        return
    if getattr(state, "is_busy", False) or getattr(state, "imagegen_is_busy", False):
        state.imagegen_status_text = "Model changed. Restart Z-Image after the current job finishes."
        state.status_text = state.imagegen_status_text
        return
    if not _service_runtime_is_available(state, "n2d2"):
        state.imagegen_status_text = "Model changed. Start Z-Image to use it."
        state.status_text = state.imagegen_status_text
        return
    if not _backend_is_alive("n2d2"):
        state.imagegen_status_text = "Model changed. Restart Z-Image to use it."
        state.status_text = state.imagegen_status_text
        return

    N2D2_AUTORESTART_GUARD = True
    try:
        proc = _managed_backend("n2d2")
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        _forget_backend(service_key="n2d2", proc=proc)
        _best_effort_stop_local(_service_port(state, "n2d2"), state, service_key="n2d2")
        _apply_service_state(
            state,
            "n2d2",
            launch_state="Stopped",
            launch_detail="Restarting Z-Image for model change...",
            backend_summary="Unavailable",
        )
        ok, message = _start_service_process(context, state, "n2d2")
        if ok:
            message = f"Restarting Z-Image for {_short_n2d2_runtime_label(state)}."
        state.imagegen_status_text = message
        state.status_text = message
        _schedule_event_loop()
    finally:
        N2D2_AUTORESTART_GUARD = False


def _set_3d_target_from_service(state, service_key):
    if service_key == "trellis":
        state.launch_backend = "BACKEND_TRELLIS"
        state.shape_workflow = "IMAGE"
    else:
        state.launch_backend = "BACKEND_2MV"
    _sync_local_api(state)


class NYMPHSV2_OT_probe_backend(bpy.types.Operator):
    bl_idname = "nymphsv2.probe_backend"
    bl_label = "Refresh Backends"
    bl_description = "Fetch /server_info from the local managed backends"

    def execute(self, context):
        state = context.scene.nymphs_state
        threading.Thread(target=_background_primary_probe, args=(context.scene.name,), daemon=True).start()
        for service_key in SERVICE_ORDER:
            if _service_enabled(state, service_key) or _backend_is_alive(service_key):
                threading.Thread(
                    target=_background_server_probe,
                    args=(context.scene.name, service_key),
                    daemon=True,
                ).start()
        state.status_text = "Backend refresh requested."
        _schedule_event_loop()
        return {"FINISHED"}


class NYMPHSV2_OT_probe_gpu(bpy.types.Operator):
    bl_idname = "nymphsv2.probe_gpu"
    bl_label = "Refresh GPU"
    bl_description = "Probe host GPU usage with nvidia-smi"

    def execute(self, context):
        state = context.scene.nymphs_state
        stats = _probe_gpu_host()
        state.gpu_name = stats["name"]
        state.gpu_memory = stats["memory"]
        state.gpu_utilization = stats["utilization"]
        state.gpu_status_message = stats["status"]
        state.status_text = "GPU probe finished."
        return {"FINISHED"}


class NYMPHSV2_OT_stop_backend(bpy.types.Operator):
    bl_idname = "nymphsv2.stop_backend"
    bl_label = "Stop All"
    bl_description = "Stop all managed local WSL backends"

    def execute(self, context):
        state = context.scene.nymphs_state
        stopped_any = False
        for service_key in SERVICE_ORDER:
            ok, _message = _stop_service_process(state, service_key)
            stopped_any = stopped_any or ok
        if not stopped_any:
            _best_effort_stop_local(state=state)
            state.launch_state = "Stopped"
            state.launch_detail = "No managed backends were running."
            state.status_text = "No managed backends were running."
            return {"CANCELLED"}
        state.status_text = "Stop requested."
        _schedule_event_loop()
        return {"FINISHED"}


class NYMPHSV2_OT_start_service(bpy.types.Operator):
    bl_idname = "nymphsv2.start_service"
    bl_label = "Start Service"
    bl_description = "Launch one managed local WSL backend"

    service_key: StringProperty()

    def execute(self, context):
        state = context.scene.nymphs_state
        ok, message = _start_service_process(context, state, self.service_key)
        state.status_text = message
        _schedule_event_loop()
        return {"FINISHED"} if ok else {"CANCELLED"}


class NYMPHSV2_OT_stop_service(bpy.types.Operator):
    bl_idname = "nymphsv2.stop_service"
    bl_label = "Stop Service"
    bl_description = "Stop one managed local WSL backend"

    service_key: StringProperty()

    def execute(self, context):
        state = context.scene.nymphs_state
        ok, message = _stop_service_process(state, self.service_key)
        state.status_text = message
        _schedule_event_loop()
        return {"FINISHED"} if ok else {"CANCELLED"}


class NYMPHSV2_OT_set_3d_target(bpy.types.Operator):
    bl_idname = "nymphsv2.set_3d_target"
    bl_label = "Set 3D Target"
    bl_description = "Use this runtime for 3D requests"

    service_key: StringProperty()

    def execute(self, context):
        state = context.scene.nymphs_state
        _set_3d_target_from_service(state, self.service_key)
        state.status_text = f"3D target set to {_service_display_name(_selected_3d_service_key(state))}."
        _touch_ui()
        return {"FINISHED"}


def _submit_request(context, state, payload, source_name, status_text):
    api_root = _normalize_api_root(state.api_root)
    state.is_busy = True
    state.status_text = status_text
    state.last_result = ""
    state.generation_request_finished_locally = False
    state.task_status = "queued"
    state.task_stage = "Request Sent"
    state.task_detail = "Waiting for backend progress..."
    state.task_progress = ""
    state.task_message = ""
    state.waiting_for_backend_progress = True
    state.saw_real_backend_progress = False
    _touch_ui()

    worker = threading.Thread(
        target=_job_worker,
        args=(context.scene.name, api_root, payload, source_name),
        daemon=True,
    )
    worker.start()
    _schedule_event_loop()


class NYMPHSV2_OT_run_shape_request(bpy.types.Operator):
    bl_idname = "nymphsv2.run_shape_request"
    bl_label = "Generate Shape"
    bl_description = "Send the current shape request to the backend and import the result"

    def execute(self, context):
        state = context.scene.nymphs_state
        if state.is_busy or state.imagegen_is_busy:
            self.report({"WARNING"}, "A request is already running.")
            return {"CANCELLED"}

        caps = _state_server_capabilities(state)
        texture_requested = bool(state.shape_generate_texture and caps["texture"])
        if not caps["shape"] or caps["texture_only"]:
            self.report({"ERROR"}, "The current server is not exposing shape generation.")
            return {"CANCELLED"}
        if state.shape_workflow == "MULTIVIEW" and not caps["multiview"]:
            self.report({"ERROR"}, "The current server does not support multiview shape generation.")
            return {"CANCELLED"}
        if state.shape_workflow == "TEXT" and not caps["text"]:
            self.report({"ERROR"}, "The current server was not started with text support.")
            return {"CANCELLED"}

        try:
            api_root = _normalize_api_root(state.api_root)
            _require_network_access(api_root)
            payload = _build_shape_payload(state)
            payload["texture"] = texture_requested
            if texture_requested:
                payload.update(_texture_option_payload(state))
        except Exception as exc:
            state.status_text = str(exc)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        if state.shape_workflow == "MULTIVIEW":
            status_text = "Submitting multiview shape request..."
            if texture_requested:
                status_text = "Submitting multiview shape + texture request..."
        elif state.shape_workflow == "TEXT":
            status_text = "Submitting text-to-3D request..."
            if texture_requested:
                status_text = "Submitting text-to-3D + texture request..."
        else:
            status_text = "Submitting image-to-3D request..."
            if texture_requested:
                status_text = "Submitting image-to-3D + texture request..."

        _submit_request(context, state, payload, "", status_text)
        return {"FINISHED"}


class NYMPHSV2_OT_run_texture_request(bpy.types.Operator):
    bl_idname = "nymphsv2.run_texture_request"
    bl_label = "Retexture Selected Mesh"
    bl_description = "Send the selected mesh to the backend texture path and import the result"

    def execute(self, context):
        state = context.scene.nymphs_state
        if state.is_busy or state.imagegen_is_busy:
            self.report({"WARNING"}, "A request is already running.")
            return {"CANCELLED"}

        texture_service_key = _selected_texture_service_key(state)
        caps = _service_capabilities_from_summary(state, texture_service_key)
        if not caps["retexture"]:
            self.report({"ERROR"}, "The current server does not expose mesh retexturing.")
            return {"CANCELLED"}

        try:
            api_root = _normalize_api_root(_service_api_root(state, texture_service_key))
            _require_network_access(api_root)
            mesh_format = _preferred_texture_mesh_format(state, caps)
            mesh_b64, source_name = _export_selected_mesh_base64(context, export_format=mesh_format)
            payload = _build_texture_payload(state, mesh_b64=mesh_b64, mesh_format=mesh_format)
        except Exception as exc:
            state.status_text = str(exc)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        _submit_request(context, state, payload, source_name, "Texturing selected mesh...")
        return {"FINISHED"}


class NYMPHSV2_OT_generate_image(bpy.types.Operator):
    bl_idname = "nymphsv2.generate_image"
    bl_label = "Generate Image"
    bl_description = "Generate one image through the selected image backend and assign it to Image"

    def execute(self, context):
        state = context.scene.nymphs_state
        if state.is_busy or state.imagegen_is_busy:
            self.report({"WARNING"}, "Another request is already running.")
            return {"CANCELLED"}
        if bool(getattr(state, "imagegen_generate_mv", False)):
            return bpy.ops.nymphsv2.generate_mv_set()

        backend = getattr(state, "imagegen_backend", "Z_IMAGE")
        breakout_auto_mode = False
        try:
            variant_count = max(1, int(getattr(state, "imagegen_variant_count", 1)))
            assign_first_output = False
            if backend == "GEMINI":
                _require_network_access(OPENROUTER_API_ROOT)
                prompt = _resolved_imagegen_prompt(state)
                if not prompt:
                    raise RuntimeError("Enter an image-generation prompt first.")
                snapshot = _gemini_snapshot(state)
                preset_key = _sync_imagegen_prompt_preset(state)
                _preset_kind, preset_id = _split_prompt_preset_key(preset_key)
                if preset_id == "character_part_breakout":
                    payload = [_character_part_breakout_auto_prompt(prompt)]
                    assign_first_output = True
                    breakout_auto_mode = True
                else:
                    payload = [prompt for _index in range(variant_count)]
                worker_target = _gemini_imagegen_worker
                worker_args = (context.scene.name, snapshot, payload, assign_first_output)
                generated = False
                base_seed = None
            else:
                api_root = _normalize_api_root(_service_api_root(state, "n2d2"))
                _require_network_access(api_root)
                seed_step = max(1, int(getattr(state, "imagegen_seed_step", 1)))
                preset_key = _sync_imagegen_prompt_preset(state)
                _preset_kind, preset_id = _split_prompt_preset_key(preset_key)
                if preset_id == "character_part_breakout":
                    prompt_sequence = _character_part_breakout_variant_prompts(_resolved_imagegen_prompt(state), variant_count)
                    if variant_count > 1:
                        base_seed, generated = _imagegen_seed_value(state)
                        payload = [
                            _build_imagegen_payload_for_prompt(
                                state,
                                prompt=prompt_sequence[index],
                                seed=base_seed + (index * seed_step),
                            )
                            for index in range(variant_count)
                        ]
                    else:
                        payload = _build_imagegen_payload_for_prompt(state, prompt=prompt_sequence[0])
                        generated = False
                        base_seed = None
                    assign_first_output = variant_count > 1
                elif variant_count > 1:
                    base_seed, generated = _imagegen_seed_value(state)
                    prompt_sequence = [_resolved_imagegen_prompt(state) for _ in range(variant_count)]
                    payload = [
                        _build_imagegen_payload_for_prompt(
                            state,
                            prompt=prompt_sequence[index],
                            seed=base_seed + (index * seed_step),
                        )
                        for index in range(variant_count)
                    ]
                else:
                    payload = _build_imagegen_payload(state)
                    generated = False
                    base_seed = None
                worker_target = _imagegen_worker
                worker_args = (context.scene.name, api_root, payload, assign_first_output)
        except Exception as exc:
            state.imagegen_status_text = str(exc)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        state.imagegen_is_busy = True
        state.imagegen_started_at = time.time()
        image_backend_label = _current_image_backend_label(state)
        image_backend_detail = _current_image_backend_detail(state)
        if breakout_auto_mode:
            state.imagegen_status_text = f"Generating {image_backend_label} breakout set..."
            state.imagegen_task_stage = "Generating Breakout Set"
            state.imagegen_task_detail = image_backend_detail or "Requesting the full breakout set..."
            state.imagegen_task_progress = ""
        elif variant_count > 1:
            state.imagegen_status_text = f"Generating {variant_count} {image_backend_label} variants..."
            state.imagegen_task_stage = "Generating Variants"
            state.imagegen_task_detail = image_backend_detail or "Waiting for image backend progress..."
            state.imagegen_task_progress = f"0/{variant_count}"
            if generated and base_seed is not None:
                state.imagegen_seed = str(base_seed)
        else:
            state.imagegen_status_text = f"Generating {image_backend_label} image..."
            state.imagegen_task_stage = f"{image_backend_label} Image"
            state.imagegen_task_detail = image_backend_detail or "Waiting for image backend progress..."
            state.imagegen_task_progress = ""
        state.imagegen_task_status = "queued"
        state.imagegen_output_path = ""
        state.imagegen_metadata_path = ""
        _touch_ui()

        worker = threading.Thread(
            target=worker_target,
            args=worker_args,
            daemon=True,
        )
        worker.start()
        _schedule_event_loop()
        return {"FINISHED"}


class NYMPHSV2_OT_generate_mv_set(bpy.types.Operator):
    bl_idname = "nymphsv2.generate_mv_set"
    bl_label = "Generate MV Set"
    bl_description = "Generate front, left, right, and back images through the selected image backend and assign them to the multiview slots"

    def execute(self, context):
        state = context.scene.nymphs_state
        if state.is_busy or state.imagegen_is_busy:
            self.report({"WARNING"}, "Another request is already running.")
            return {"CANCELLED"}

        backend = getattr(state, "imagegen_backend", "Z_IMAGE")
        try:
            if not state.imagegen_prompt.strip():
                raise RuntimeError("Enter an image-generation prompt first.")
            if backend == "GEMINI":
                _require_network_access(OPENROUTER_API_ROOT)
                snapshot = _gemini_snapshot(state)
                prompts = [
                    (view_key, view_label, _build_mv_prompt(state, view_phrase))
                    for view_key, view_label, view_phrase in IMAGEGEN_MV_VIEW_SPECS
                ]
                worker_target = _gemini_mv_worker
                worker_args = (context.scene.name, snapshot, prompts)
                generated = False
                seed = None
            else:
                api_root = _normalize_api_root(_service_api_root(state, "n2d2"))
                _require_network_access(api_root)
                seed, generated = _imagegen_seed_value(state)
                worker_target = _imagegen_mv_worker
                worker_args = (context.scene.name, api_root, seed)
        except Exception as exc:
            state.imagegen_status_text = str(exc)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        state.imagegen_is_busy = True
        state.imagegen_started_at = time.time()
        image_backend_label = _current_image_backend_label(state)
        image_backend_detail = _current_image_backend_detail(state)
        state.imagegen_status_text = f"Generating {image_backend_label} MV set..."
        state.imagegen_task_status = "queued"
        state.imagegen_task_stage = "Generating MV Set"
        state.imagegen_task_detail = image_backend_detail or "Preparing front, left, right, and back prompts..."
        state.imagegen_task_progress = "0/4"
        state.imagegen_output_path = ""
        state.imagegen_metadata_path = ""
        if generated and seed is not None:
            state.imagegen_seed = str(seed)
        _touch_ui()

        worker = threading.Thread(
            target=worker_target,
            args=worker_args,
            daemon=True,
        )
        worker.start()
        _schedule_event_loop()
        return {"FINISHED"}


class NYMPHSV2_OT_clear_managed_prompt_presets(bpy.types.Operator):
    bl_idname = "nymphsv2.clear_managed_prompt_presets"
    bl_label = "Clear Prompt Blocks"
    bl_description = "Clear the managed Subject and Style prompt blocks from the visible prompt"

    def execute(self, context):
        state = context.scene.nymphs_state
        _clear_imagegen_managed_prompt_blocks(state, reset_dropdowns=True)
        state.imagegen_status_text = "Cleared Subject and Style prompt blocks."
        _touch_ui()
        return {"FINISHED"}


class NYMPHSV2_OT_insert_saved_prompt(bpy.types.Operator):
    bl_idname = "nymphsv2.insert_saved_prompt"
    bl_label = "Insert Saved Prompt"
    bl_description = "Insert the selected saved full prompt into the visible prompt"

    def execute(self, context):
        state = context.scene.nymphs_state
        if not _load_selected_saved_prompt_into_prompt(state):
            self.report({"ERROR"}, "Choose a saved prompt first.")
            return {"CANCELLED"}
        preset = _imagegen_prompt_preset_data(
            _sync_imagegen_prompt_preset(state, "imagegen_saved_prompt_preset", PROMPT_KIND_SAVED),
            PROMPT_KIND_SAVED,
        )
        state.imagegen_status_text = f"Loaded saved prompt: {preset.get('label', 'Saved Prompt')}"
        _touch_ui()
        return {"FINISHED"}


class NYMPHSV2_OT_save_current_prompt(bpy.types.Operator):
    bl_idname = "nymphsv2.save_current_prompt"
    bl_label = "Save Current Prompt"
    bl_description = "Save the current visible prompt as a reusable saved prompt"

    name: StringProperty(
        name="Saved Prompt Name",
        default="",
    )

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=420)

    def execute(self, context):
        state = context.scene.nymphs_state
        _pull_imagegen_text_from_block(state, "prompt")
        name = (self.name or "").strip()
        if not name:
            self.report({"ERROR"}, "Enter a saved prompt name.")
            return {"CANCELLED"}
        prompt = _resolved_imagegen_prompt(state)
        if not prompt:
            self.report({"ERROR"}, "Enter a prompt before saving.")
            return {"CANCELLED"}
        key = _prompt_preset_key(PROMPT_KIND_SAVED, name)
        payload = _prompt_preset_payload(
            name,
            PROMPT_KIND_SAVED,
            prompt,
            f"Saved from Blender on {time.strftime('%Y-%m-%d %H:%M:%S')}",
        )
        with open(_imagegen_preset_file(key), "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        _clear_prompt_preset_cache()
        state.imagegen_saved_prompt_preset = key
        _sync_imagegen_prompt_preset(state, "imagegen_saved_prompt_preset", PROMPT_KIND_SAVED)
        state.imagegen_status_text = f"Saved prompt: {name}"
        _touch_ui()
        return {"FINISHED"}


class NYMPHSV2_OT_load_prompt_preset(bpy.types.Operator):
    bl_idname = "nymphsv2.load_prompt_preset"
    bl_label = "Load Preset"
    bl_description = "Load the selected prompt preset without changing image settings"

    def execute(self, context):
        state = context.scene.nymphs_state
        preset = _imagegen_prompt_preset_data(_sync_imagegen_prompt_preset(state))
        _set_imagegen_prompt_value(state, "prompt", preset["prompt"])
        state.imagegen_status_text = f"Loaded preset: {preset['label']}"
        _touch_ui()
        return {"FINISHED"}


class NYMPHSV2_OT_load_imagegen_settings_preset(bpy.types.Operator):
    bl_idname = "nymphsv2.load_imagegen_settings_preset"
    bl_label = "Apply Generation Profile"
    bl_description = "Apply the selected Z-Image profile. Built-in profiles can change model rank, image settings, and variants without changing prompt text."

    def execute(self, context):
        state = context.scene.nymphs_state
        preset = _imagegen_settings_preset_data(_sync_imagegen_settings_preset(state))
        _apply_imagegen_settings_preset(state, preset.get("values", {}))
        state.imagegen_status_text = f"Loaded profile: {preset['label']}"
        _touch_ui()
        return {"FINISHED"}


class NYMPHSV2_OT_save_imagegen_settings_preset(bpy.types.Operator):
    bl_idname = "nymphsv2.save_imagegen_settings_preset"
    bl_label = "Save Generation Profile"
    bl_description = "Save the current Z-Image generation profile as editable JSON"

    name: StringProperty(
        name="Preset Name",
        default="",
    )

    def invoke(self, context, event):
        state = context.scene.nymphs_state
        current = _imagegen_settings_preset_data(_sync_imagegen_settings_preset(state))
        self.name = current.get("label", "") if current.get("values") else ""
        return context.window_manager.invoke_props_dialog(self, width=420)

    def execute(self, context):
        state = context.scene.nymphs_state
        name = (self.name or "").strip()
        if not name:
            self.report({"ERROR"}, "Enter a profile name.")
            return {"CANCELLED"}
        key = _imagegen_settings_preset_slug(name)
        payload = {
            "name": name,
            "description": f"Saved from Blender on {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "values": _current_imagegen_settings_values(state),
        }
        with open(_imagegen_settings_preset_file(key), "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        state.imagegen_settings_preset = key
        _sync_imagegen_settings_preset(state)
        state.imagegen_status_text = f"Saved profile: {name}"
        _touch_ui()
        return {"FINISHED"}


class NYMPHSV2_OT_delete_imagegen_settings_preset(bpy.types.Operator):
    bl_idname = "nymphsv2.delete_imagegen_settings_preset"
    bl_label = "Delete Generation Profile"
    bl_description = "Delete the selected Z-Image generation profile JSON file"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        state = context.scene.nymphs_state
        key = (state.imagegen_settings_preset or "").strip()
        path = _imagegen_settings_preset_file(key)
        if not key or key == "__none__" or not os.path.exists(path):
            self.report({"ERROR"}, "No generation profile file is selected.")
            return {"CANCELLED"}
        os.remove(path)
        _sync_imagegen_settings_preset(state)
        state.imagegen_status_text = "Deleted generation profile."
        _touch_ui()
        return {"FINISHED"}


class NYMPHSV2_OT_open_imagegen_settings_presets_folder(bpy.types.Operator):
    bl_idname = "nymphsv2.open_imagegen_settings_presets_folder"
    bl_label = "Open Generation Profiles Folder"
    bl_description = "Open the folder containing editable Z-Image generation profile JSON files"

    def execute(self, context):
        _seed_imagegen_settings_presets()
        try:
            bpy.ops.wm.path_open(filepath=_imagegen_settings_preset_dir())
        except Exception as exc:
            self.report({"ERROR"}, f"Could not open generation profiles folder: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class NYMPHSV2_OT_save_prompt_preset(bpy.types.Operator):
    bl_idname = "nymphsv2.save_prompt_preset"
    bl_label = "Save Preset"
    bl_description = "Save the current prompt as an editable JSON preset"

    name: StringProperty(
        name="Preset Name",
        default="",
    )

    def invoke(self, context, event):
        state = context.scene.nymphs_state
        current = _imagegen_prompt_preset_data(_sync_imagegen_prompt_preset(state))
        self.name = current.get("label", "") if current.get("prompt") else ""
        return context.window_manager.invoke_props_dialog(self, width=420)

    def execute(self, context):
        state = context.scene.nymphs_state
        name = (self.name or "").strip()
        if not name:
            self.report({"ERROR"}, "Enter a preset name.")
            return {"CANCELLED"}
        prompt = (state.imagegen_prompt or "").strip()
        if not prompt:
            self.report({"ERROR"}, "Enter a prompt before saving a preset.")
            return {"CANCELLED"}
        key = _imagegen_preset_slug(name)
        payload = {
            "name": name,
            "prompt": prompt,
        }
        with open(_imagegen_preset_file(key), "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        state.imagegen_prompt_preset = key
        _sync_imagegen_prompt_preset(state)
        state.imagegen_status_text = f"Saved preset: {name}"
        _touch_ui()
        return {"FINISHED"}


class NYMPHSV2_OT_delete_prompt_preset(bpy.types.Operator):
    bl_idname = "nymphsv2.delete_prompt_preset"
    bl_label = "Delete Preset"
    bl_description = "Delete the selected prompt preset JSON file"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        state = context.scene.nymphs_state
        key = (state.imagegen_prompt_preset or "").strip()
        path = _imagegen_preset_file(key)
        if not key or key == "__none__" or not os.path.exists(path):
            self.report({"ERROR"}, "No preset file is selected.")
            return {"CANCELLED"}
        os.remove(path)
        _sync_imagegen_prompt_preset(state)
        state.imagegen_status_text = "Deleted prompt preset."
        _touch_ui()
        return {"FINISHED"}


class NYMPHSV2_OT_open_prompt_presets_folder(bpy.types.Operator):
    bl_idname = "nymphsv2.open_prompt_presets_folder"
    bl_label = "Open Folder"
    bl_description = "Open the folder containing editable JSON prompt presets"

    def execute(self, context):
        _seed_imagegen_prompt_presets()
        try:
            bpy.ops.wm.path_open(filepath=_imagegen_preset_dir())
        except Exception as exc:
            self.report({"ERROR"}, f"Could not open presets folder: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class NYMPHSV2_OT_load_style_preset(bpy.types.Operator):
    bl_idname = "nymphsv2.load_style_preset"
    bl_label = "Load Style"
    bl_description = "Load the selected style preset into the separate style field"

    def execute(self, context):
        state = context.scene.nymphs_state
        preset = _imagegen_style_preset_data(_sync_imagegen_style_preset(state))
        state.imagegen_style = preset.get("style", "")
        state.imagegen_status_text = f"Loaded style: {preset.get('label', 'No Style')}"
        _touch_ui()
        return {"FINISHED"}


class NYMPHSV2_OT_save_style_preset(bpy.types.Operator):
    bl_idname = "nymphsv2.save_style_preset"
    bl_label = "Save Style"
    bl_description = "Save the current style fragment as an editable JSON preset"

    name: StringProperty(
        name="Style Name",
        default="",
    )

    def invoke(self, context, event):
        state = context.scene.nymphs_state
        current = _imagegen_style_preset_data(_sync_imagegen_style_preset(state))
        self.name = current.get("label", "") if current.get("style") else ""
        return context.window_manager.invoke_props_dialog(self, width=420)

    def execute(self, context):
        state = context.scene.nymphs_state
        name = (self.name or "").strip()
        if not name:
            self.report({"ERROR"}, "Enter a style name.")
            return {"CANCELLED"}
        style = (state.imagegen_style or "").strip()
        if not style:
            self.report({"ERROR"}, "Enter a style before saving a preset.")
            return {"CANCELLED"}
        key = _imagegen_style_preset_slug(name)
        payload = {
            "name": name,
            "description": f"Saved from Blender on {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "style": style,
        }
        with open(_imagegen_style_preset_file(key), "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        state.imagegen_style_preset = key
        _sync_imagegen_style_preset(state)
        state.imagegen_status_text = f"Saved style: {name}"
        _touch_ui()
        return {"FINISHED"}


class NYMPHSV2_OT_delete_style_preset(bpy.types.Operator):
    bl_idname = "nymphsv2.delete_style_preset"
    bl_label = "Delete Style"
    bl_description = "Delete the selected style preset JSON file"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        state = context.scene.nymphs_state
        key = (state.imagegen_style_preset or "").strip()
        path = _imagegen_style_preset_file(key)
        if not key or key == DEFAULT_IMAGEGEN_STYLE_PRESET or not os.path.exists(path):
            self.report({"ERROR"}, "No style preset file is selected.")
            return {"CANCELLED"}
        os.remove(path)
        state.imagegen_style_preset = DEFAULT_IMAGEGEN_STYLE_PRESET
        _sync_imagegen_style_preset(state)
        state.imagegen_status_text = "Deleted style preset."
        _touch_ui()
        return {"FINISHED"}


class NYMPHSV2_OT_open_style_presets_folder(bpy.types.Operator):
    bl_idname = "nymphsv2.open_style_presets_folder"
    bl_label = "Open Folder"
    bl_description = "Open the folder containing editable JSON style presets"

    def execute(self, context):
        _seed_imagegen_style_presets()
        try:
            bpy.ops.wm.path_open(filepath=_imagegen_style_preset_dir())
        except Exception as exc:
            self.report({"ERROR"}, f"Could not open style presets folder: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class NYMPHSV2_OT_clear_image_style_field(bpy.types.Operator):
    bl_idname = "nymphsv2.clear_image_style_field"
    bl_label = "Clear Style Field"
    bl_description = "Clear the current style fragment without changing the main prompt"

    def execute(self, context):
        state = context.scene.nymphs_state
        state.imagegen_style = ""
        state.imagegen_status_text = "Cleared style fragment."
        _touch_ui()
        return {"FINISHED"}


class NYMPHSV2_OT_load_trellis_shape_preset(bpy.types.Operator):
    bl_idname = "nymphsv2.load_trellis_shape_preset"
    bl_label = "Apply TRELLIS Preset"
    bl_description = "Apply the selected TRELLIS shape preset"

    def execute(self, context):
        state = context.scene.nymphs_state
        preset = _trellis_shape_preset_data(_sync_trellis_shape_preset(state))
        _apply_trellis_shape_preset(state, preset.get("values", {}))
        state.status_text = (
            f"Loaded TRELLIS preset: {preset['label']} "
            f"({state.trellis_pipeline_type}, tokens {state.trellis_max_tokens})"
        )
        _touch_ui()
        return {"FINISHED"}


class NYMPHSV2_OT_save_trellis_shape_preset(bpy.types.Operator):
    bl_idname = "nymphsv2.save_trellis_shape_preset"
    bl_label = "Save TRELLIS Preset"
    bl_description = "Save the current TRELLIS shape settings as an editable JSON preset"

    name: StringProperty(
        name="Preset Name",
        default="",
    )

    def invoke(self, context, event):
        state = context.scene.nymphs_state
        current = _trellis_shape_preset_data(_sync_trellis_shape_preset(state))
        self.name = current.get("label", "") if current.get("values") else ""
        return context.window_manager.invoke_props_dialog(self, width=420)

    def execute(self, context):
        state = context.scene.nymphs_state
        name = (self.name or "").strip()
        if not name:
            self.report({"ERROR"}, "Enter a preset name.")
            return {"CANCELLED"}
        key = _trellis_shape_preset_slug(name)
        payload = {
            "name": name,
            "description": f"Saved from Blender on {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "values": _current_trellis_shape_preset_values(state),
        }
        with open(_trellis_shape_preset_file(key), "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        state.trellis_shape_preset = key
        _sync_trellis_shape_preset(state)
        state.status_text = f"Saved TRELLIS preset: {name}"
        _touch_ui()
        return {"FINISHED"}


class NYMPHSV2_OT_delete_trellis_shape_preset(bpy.types.Operator):
    bl_idname = "nymphsv2.delete_trellis_shape_preset"
    bl_label = "Delete TRELLIS Preset"
    bl_description = "Delete the selected TRELLIS shape preset JSON file"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        state = context.scene.nymphs_state
        key = (state.trellis_shape_preset or "").strip()
        path = _trellis_shape_preset_file(key)
        if not key or key == "__none__" or not os.path.exists(path):
            self.report({"ERROR"}, "No preset file is selected.")
            return {"CANCELLED"}
        os.remove(path)
        _sync_trellis_shape_preset(state)
        state.status_text = "Deleted TRELLIS preset."
        _touch_ui()
        return {"FINISHED"}


class NYMPHSV2_OT_open_trellis_shape_presets_folder(bpy.types.Operator):
    bl_idname = "nymphsv2.open_trellis_shape_presets_folder"
    bl_label = "Open TRELLIS Presets Folder"
    bl_description = "Open the folder containing editable TRELLIS shape preset JSON files"

    def execute(self, context):
        _seed_trellis_shape_presets()
        try:
            bpy.ops.wm.path_open(filepath=_trellis_shape_preset_dir())
        except Exception as exc:
            self.report({"ERROR"}, f"Could not open TRELLIS presets folder: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class NYMPHSV2_OT_clear_image_prompt_field(bpy.types.Operator):
    bl_idname = "nymphsv2.clear_image_prompt_field"
    bl_label = "Clear Prompt Field"
    bl_description = "Clear the selected image prompt field"

    target: EnumProperty(
        name="Target",
        items=(
            ("prompt", "Prompt", "Clear the main prompt"),
            ("guidance", "Guidance", "Clear part extraction guidance"),
        ),
        default="prompt",
    )

    def execute(self, context):
        state = context.scene.nymphs_state
        _set_imagegen_prompt_value(state, self.target, "")
        state.imagegen_status_text = "Cleared guidance." if self.target == "guidance" else "Cleared image prompt."
        _touch_ui()
        return {"FINISHED"}


class NYMPHSV2_OT_open_image_prompt_text_block(bpy.types.Operator):
    bl_idname = "nymphsv2.open_image_prompt_text_block"
    bl_label = "Open Prompt Text Block"
    bl_description = "Open the prompt in Blender's Text Editor for multiline editing"

    target: EnumProperty(
        name="Target",
        items=(
            ("prompt", "Prompt", "Open the main prompt in a Blender text block"),
            ("guidance", "Guidance", "Open part extraction guidance in a Blender text block"),
        ),
        default="prompt",
    )

    def execute(self, context):
        state = context.scene.nymphs_state
        text = _ensure_imagegen_text(state, self.target)
        opened = _open_text_in_editor(context, text)
        label = "guidance" if self.target == "guidance" else "prompt"
        if opened:
            state.status_text = f"Editing {label} in Text Editor. Return here and click Apply."
        else:
            state.status_text = f"Prepared {label} text block: {text.name}"
            self.report({"INFO"}, f"Text block ready: {text.name}. Open a Blender Text Editor area to edit it.")
        _touch_ui()
        return {"FINISHED"}


class NYMPHSV2_OT_pull_image_prompt_text_block(bpy.types.Operator):
    bl_idname = "nymphsv2.pull_image_prompt_text_block"
    bl_label = "Use Prompt Text Block"
    bl_description = "Copy the linked Blender text block back into the addon prompt field"

    target: EnumProperty(
        name="Target",
        items=(
            ("prompt", "Prompt", "Use the linked prompt text block"),
            ("guidance", "Guidance", "Use the linked guidance text block"),
        ),
        default="prompt",
    )

    def execute(self, context):
        state = context.scene.nymphs_state
        applied = []
        if _pull_imagegen_text_from_block(state, self.target):
            applied.append("guidance" if self.target == "guidance" else "prompt")
        if not applied:
            self.report({"WARNING"}, "No linked text block was found.")
            return {"CANCELLED"}
        state.imagegen_status_text = "Applied text from Blender Text Editor."
        _touch_ui()
        return {"FINISHED"}


class NYMPHSV2_OT_edit_image_prompts(bpy.types.Operator):
    bl_idname = "nymphsv2.edit_image_prompts"
    bl_label = "Edit Image Prompt"
    bl_description = "Open a wider editor for one Z-Image prompt field"

    target: EnumProperty(
        name="Target",
        items=(
            ("prompt", "Prompt", "Edit the main prompt"),
            ("guidance", "Guidance", "Edit part extraction guidance"),
        ),
        default="prompt",
    )

    prompt: StringProperty(
        name="Prompt",
        description="Prompt text",
        default="",
    )

    def invoke(self, context, event):
        state = context.scene.nymphs_state
        text = _linked_imagegen_text(state, self.target)
        self.prompt = _text_to_prompt_value(text) if text is not None else getattr(
            state,
            _imagegen_text_field_name(self.target),
        )
        return context.window_manager.invoke_props_dialog(self, width=980)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = False
        layout.use_property_decorate = False

        body = layout.column(align=True)
        body.label(text="Guidance" if self.target == "guidance" else "Prompt")
        prompt_row = body.row()
        prompt_row.scale_y = 3.0
        prompt_row.prop(self, "prompt", text="")
        body.label(text="For long prompts, use Text Editor in the panel.")

    def execute(self, context):
        state = context.scene.nymphs_state
        _set_imagegen_prompt_value(state, self.target, self.prompt)
        _touch_ui()
        return {"FINISHED"}


class NYMPHSV2_OT_preview_image_prompt(bpy.types.Operator):
    bl_idname = "nymphsv2.preview_image_prompt"
    bl_label = "Prompt Preview"
    bl_description = "Preview the current visible prompt"

    target: EnumProperty(
        name="Target",
        items=(
            ("prompt", "Prompt", "Preview the main prompt"),
            ("guidance", "Guidance", "Preview part extraction guidance"),
        ),
        default="prompt",
    )

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=760)

    def draw(self, context):
        state = context.scene.nymphs_state
        layout = self.layout
        layout.use_property_split = False
        layout.use_property_decorate = False
        value = (getattr(state, _imagegen_text_field_name(self.target), "") or "").strip()
        title = "Guidance" if self.target == "guidance" else "Current Prompt"
        if not value:
            layout.label(text="No prompt text yet.")
            return
        body = layout.column(align=True)
        body.label(text=title)
        for line in _wrap_panel_text(value, width=96, max_lines=24):
            body.label(text=line)

    def execute(self, context):
        return {"FINISHED"}


class NYMPHSV2_OT_pick_gemini_guide_image(bpy.types.Operator):
    bl_idname = "nymphsv2.pick_gemini_guide_image"
    bl_label = "Pick Guide Image"
    bl_description = "Choose a guide image for Gemini Flash from the current generated-image folder"

    filepath: StringProperty(subtype="FILE_PATH")
    filter_glob: StringProperty(default="*.png;*.jpg;*.jpeg;*.webp;*.gif", options={"HIDDEN"})

    def invoke(self, context, event):
        state = context.scene.nymphs_state
        current_path = _gemini_guide_image_path(state)
        if current_path and os.path.isfile(current_path):
            self.filepath = current_path
        else:
            self.filepath = os.path.join(_current_imagegen_folder(state), "")
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        state = context.scene.nymphs_state
        selected_path = bpy.path.abspath((self.filepath or "").strip())
        if not selected_path:
            self.report({"ERROR"}, "Pick a guide image first.")
            return {"CANCELLED"}
        try:
            normalized_path, _data_url = _gemini_guide_image_data_url(selected_path)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        state.gemini_guide_image_path = normalized_path
        state.gemini_use_guide_image = True
        _touch_ui()
        return {"FINISHED"}


class NYMPHSV2_OT_clear_gemini_guide_image(bpy.types.Operator):
    bl_idname = "nymphsv2.clear_gemini_guide_image"
    bl_label = "Clear Guide"
    bl_description = "Clear the Gemini Flash guide image path"

    def execute(self, context):
        state = context.scene.nymphs_state
        state.gemini_guide_image_path = ""
        state.gemini_use_guide_image = False
        _touch_ui()
        return {"FINISHED"}


class NYMPHSV2_OT_pick_part_master_image(bpy.types.Operator):
    bl_idname = "nymphsv2.pick_part_master_image"
    bl_label = "Pick Master"
    bl_description = "Choose the master character image for guided part extraction"

    filepath: StringProperty(subtype="FILE_PATH")
    filter_glob: StringProperty(default="*.png;*.jpg;*.jpeg;*.webp;*.gif", options={"HIDDEN"})

    def invoke(self, context, event):
        state = context.scene.nymphs_state
        current_path = (getattr(state, "part_extraction_source_path", "") or getattr(state, "image_path", "") or "").strip()
        if current_path and os.path.isfile(_resolve_file_path(current_path)):
            self.filepath = _resolve_file_path(current_path)
        else:
            self.filepath = os.path.join(_current_imagegen_folder(state), "")
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        state = context.scene.nymphs_state
        selected_path = _resolve_file_path((self.filepath or "").strip())
        if not selected_path or not os.path.exists(selected_path):
            self.report({"ERROR"}, "Pick a master image first.")
            return {"CANCELLED"}
        _set_part_extraction_source_image(state, selected_path, "Master image selected for part extraction.")
        _touch_ui()
        return {"FINISHED"}


class NYMPHSV2_OT_use_current_image_as_part_master(bpy.types.Operator):
    bl_idname = "nymphsv2.use_current_image_as_part_master"
    bl_label = "Use Image"
    bl_description = "Use the current Image field as the master character reference"

    def execute(self, context):
        state = context.scene.nymphs_state
        source_image_path = _resolve_file_path(getattr(state, "image_path", ""))
        if not source_image_path or not os.path.exists(source_image_path):
            self.report({"ERROR"}, "Pick or generate an Image first.")
            return {"CANCELLED"}
        _set_part_extraction_source_image(state, source_image_path, "Current Image set as part extraction master.")
        _touch_ui()
        return {"FINISHED"}


class NYMPHSV2_OT_use_last_generated_as_part_master(bpy.types.Operator):
    bl_idname = "nymphsv2.use_last_generated_as_part_master"
    bl_label = "Use Last"
    bl_description = "Use the last generated image as the master character reference"

    def execute(self, context):
        state = context.scene.nymphs_state
        source_image_path = _resolve_file_path(getattr(state, "imagegen_output_path", ""))
        if not source_image_path or not os.path.exists(source_image_path):
            self.report({"ERROR"}, "No last generated image is available.")
            return {"CANCELLED"}
        _set_part_extraction_source_image(state, source_image_path, "Last generated image set as part extraction master.")
        _touch_ui()
        return {"FINISHED"}


class NYMPHSV2_OT_plan_character_parts(bpy.types.Operator):
    bl_idname = "nymphsv2.plan_character_parts"
    bl_label = "Plan Parts"
    bl_description = "Use the current Image as a master reference and ask Gemini to identify extractable character parts"

    def execute(self, context):
        state = context.scene.nymphs_state
        if state.is_busy or state.imagegen_is_busy:
            self.report({"WARNING"}, "Another request is already running.")
            return {"CANCELLED"}
        source_image_path = _resolve_file_path(getattr(state, "part_extraction_source_path", ""))
        if not source_image_path:
            self.report({"ERROR"}, "Choose a Source Image first.")
            return {"CANCELLED"}
        if not os.path.exists(source_image_path):
            self.report({"ERROR"}, f"Image does not exist: {source_image_path}")
            return {"CANCELLED"}
        try:
            _require_network_access(OPENROUTER_API_ROOT)
            snapshot = _part_extraction_snapshot(state, source_image_path)
        except Exception as exc:
            state.imagegen_status_text = str(exc)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        state.imagegen_is_busy = True
        state.imagegen_started_at = time.time()
        state.imagegen_status_text = "Planning character parts..."
        state.imagegen_task_status = "queued"
        state.imagegen_task_stage = "Planning Character Parts"
        state.imagegen_task_detail = f"{snapshot.get('part_planner_label', 'Planner')} will identify extractable assets."
        state.imagegen_task_progress = ""
        state.part_extraction_source_path = source_image_path
        _clear_part_extraction_plan_state(state)
        _touch_ui()

        threading.Thread(
            target=_part_planning_worker,
            args=(context.scene.name, snapshot),
            daemon=True,
        ).start()
        _schedule_event_loop()
        return {"FINISHED"}


class NYMPHSV2_OT_extract_character_parts(bpy.types.Operator):
    bl_idname = "nymphsv2.extract_character_parts"
    bl_label = "Extract Parts"
    bl_description = "Run one Gemini Flash image-edit request per planned character part"

    def execute(self, context):
        state = context.scene.nymphs_state
        if state.is_busy or state.imagegen_is_busy:
            self.report({"WARNING"}, "Another request is already running.")
            return {"CANCELLED"}
        selected_plan = _selected_part_plan_from_state(state)
        parts = selected_plan.get("parts", [])
        planned_count = int(selected_plan.get("planned_count", 0) or 0)
        if not planned_count:
            self.report({"ERROR"}, "Plan parts before extracting.")
            return {"CANCELLED"}
        if not parts:
            self.report({"ERROR"}, "Select at least one part to extract.")
            return {"CANCELLED"}
        source_image_path = _resolve_file_path(getattr(state, "part_extraction_source_path", "") or getattr(state, "image_path", ""))
        if not source_image_path or not os.path.exists(source_image_path):
            self.report({"ERROR"}, "The planned source image is missing. Pick the image and plan parts again.")
            return {"CANCELLED"}
        try:
            _require_network_access(OPENROUTER_API_ROOT)
            snapshot = _part_extraction_snapshot(state, source_image_path)
        except Exception as exc:
            state.imagegen_status_text = str(exc)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        state.imagegen_is_busy = True
        state.imagegen_started_at = time.time()
        state.imagegen_status_text = f"Extracting {len(parts)} character parts..."
        state.imagegen_task_status = "queued"
        state.imagegen_task_stage = "Extracting Character Parts"
        state.imagegen_task_detail = f"Using {_gemini_model_label(snapshot.get('model_id'))} with current Image as guide."
        state.imagegen_task_progress = f"0/{len(parts)}"
        state.part_extraction_results_path = ""
        _touch_ui()

        threading.Thread(
            target=_part_extraction_worker,
            args=(context.scene.name, snapshot, json.dumps({"parts": parts}, indent=2)),
            daemon=True,
        ).start()
        _schedule_event_loop()
        return {"FINISHED"}


class NYMPHSV2_OT_clear_character_part_plan(bpy.types.Operator):
    bl_idname = "nymphsv2.clear_character_part_plan"
    bl_label = "Clear Plan"
    bl_description = "Clear the current character-part extraction plan"

    def execute(self, context):
        state = context.scene.nymphs_state
        _clear_part_extraction_plan_state(state)
        state.imagegen_status_text = "Cleared character part plan."
        _touch_ui()
        return {"FINISHED"}


class NYMPHSV2_OT_open_imagegen_folder(bpy.types.Operator):
    bl_idname = "nymphsv2.open_imagegen_folder"
    bl_label = "Open Folder"
    bl_description = "Open the folder that contains the generated images"

    def execute(self, context):
        state = context.scene.nymphs_state
        folder_path = _current_imagegen_folder(state)
        if not folder_path:
            self.report({"ERROR"}, "No generated image folder is available yet.")
            return {"CANCELLED"}
        try:
            bpy.ops.wm.path_open(filepath=folder_path)
        except Exception as exc:
            self.report({"ERROR"}, f"Could not open folder: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class NYMPHSV2_OT_clear_imagegen_folder(bpy.types.Operator):
    bl_idname = "nymphsv2.clear_imagegen_folder"
    bl_label = "Clear Folder"
    bl_description = "Delete generated image files from the current image output folder"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        state = context.scene.nymphs_state
        folder_path = _current_imagegen_folder(state)
        try:
            _clear_folder_contents(folder_path)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        state.imagegen_output_path = ""
        state.imagegen_metadata_path = ""
        state.imagegen_status_text = "Image output folder cleared."
        _touch_ui()
        return {"FINISHED"}


class NYMPHSV2_OT_open_shape_folder(bpy.types.Operator):
    bl_idname = "nymphsv2.open_shape_folder"
    bl_label = "Open Folder"
    bl_description = "Open the folder that contains the latest generated mesh"

    def execute(self, context):
        state = context.scene.nymphs_state
        folder_path = (state.shape_output_dir or "").strip()
        if not folder_path and state.shape_output_path.strip():
            folder_path = os.path.dirname(state.shape_output_path.strip())
        if not folder_path:
            self.report({"ERROR"}, "No generated mesh folder is available yet.")
            return {"CANCELLED"}
        try:
            bpy.ops.wm.path_open(filepath=folder_path)
        except Exception as exc:
            self.report({"ERROR"}, f"Could not open folder: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class NYMPHSV2_OT_clear_shape_folder(bpy.types.Operator):
    bl_idname = "nymphsv2.clear_shape_folder"
    bl_label = "Clear Folder"
    bl_description = "Delete generated mesh files from the current shape output folder"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        state = context.scene.nymphs_state
        folder_path = (state.shape_output_dir or "").strip()
        if not folder_path and state.shape_output_path.strip():
            folder_path = os.path.dirname(state.shape_output_path.strip())
        try:
            _clear_folder_contents(folder_path)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        state.shape_output_path = ""
        state.last_result = "Shape output folder cleared."
        _touch_ui()
        return {"FINISHED"}



class NYMPHSV2_PT_server(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Nymphs"
    bl_label = "Nymphs Server"

    def draw(self, context):
        state = context.scene.nymphs_state
        layout = self.layout

        top = layout.box()
        progress_stage, progress_detail, progress_value = _server_panel_job_lines(state)
        current_job = progress_stage or "Idle"
        current_detail = _server_panel_detail_text(state, progress_detail)
        active_services = _active_service_summaries(state)
        gpu_summary = ""
        if (state.gpu_utilization or "").strip() or (state.gpu_memory or "").strip():
            gpu_parts = []
            if (state.gpu_utilization or "").strip():
                gpu_parts.append(f"Load {state.gpu_utilization}")
            if (state.gpu_memory or "").strip():
                gpu_parts.append(f"VRAM {state.gpu_memory}")
            gpu_summary = " | ".join(gpu_parts)
        if current_detail and progress_value and current_detail.strip() == progress_value.strip():
            current_detail = ""
        top.label(text=f"Status: {_server_status_line(state)}"[:160])
        if active_services:
            top.label(text=f"Runtimes: {' | '.join(active_services)}"[:160])
        if gpu_summary:
            top.label(text=f"GPU: {gpu_summary}"[:160])
        top.label(text=f"Image Backend: {_current_image_backend_label(state)}"[:160])
        image_backend_detail = _current_image_backend_detail(state)
        if image_backend_detail:
            _draw_wrapped_lines(top, image_backend_detail, prefix="Image Detail: ", width=52, max_lines=2)
        top.label(text=f"Current Job: {current_job}"[:160])
        if current_detail:
            _draw_wrapped_lines(top, current_detail, prefix="Detail: ", width=52, max_lines=2)
        if progress_value:
            top.label(text=f"Progress: {progress_value}"[:160])

        startup = layout.box()
        startup.prop(
            state,
            "show_startup",
            text="Runtimes",
            icon="TRIA_DOWN" if state.show_startup else "TRIA_RIGHT",
            emboss=False,
        )
        if state.show_startup:
            actions = startup.row(align=True)
            actions.operator("nymphsv2.stop_backend", text="Stop All")
            actions.operator("nymphsv2.probe_backend", text="Refresh")
            for service_key in SERVICE_ORDER:
                _draw_service_block(startup, state, service_key)

        advanced = layout.box()
        advanced.prop(
            state,
            "show_advanced",
            text="Advanced",
            icon="TRIA_DOWN" if state.show_advanced else "TRIA_RIGHT",
            emboss=False,
        )
        if state.show_advanced:
            target = advanced.box()
            target.label(text="WSL Target")
            target.use_property_split = True
            target.use_property_decorate = False
            target.prop(state, "wsl_distro_name", text="Distro")
            target.prop(state, "wsl_user_name", text="User")
            advanced.prop(state, "launch_open_terminal")
            advanced.prop(state, "api_root")
            guidance_entries = _runtime_guidance_entries(state)
            if guidance_entries:
                for _service_key, label, api_root, estimate, basis, mode, caveat in guidance_entries:
                    estimate_box = advanced.box()
                    estimate_box.label(text=f"{label}: {estimate} | {mode}"[:160])
                    estimate_box.label(text=f"API: {api_root}"[:160])
                    _draw_wrapped_lines(estimate_box, basis, width=48, max_lines=1)
                    _draw_wrapped_lines(estimate_box, caveat, prefix="Note: ", width=48, max_lines=2)
            gpu_box = advanced.box()
            gpu_box.label(text="GPU")
            gpu_box.operator("nymphsv2.probe_gpu", text="Refresh GPU")
            gpu_box.label(text=f"GPU: {state.gpu_name}"[:160])
            gpu_box.label(text=f"VRAM: {state.gpu_memory}"[:160])
            gpu_box.label(text=f"Load: {state.gpu_utilization}"[:160])
            gpu_box.label(text=f"Driver Status: {state.gpu_status_message}"[:160])


def _draw_imagegen_status_box(layout, state):
    image_status = (state.imagegen_status_text or "").strip()
    result = layout.box()
    result.label(text=f"Status: {image_status or 'Ready'}"[:160])
    if state.imagegen_output_path:
        result.label(text=f"Last Image: {_path_leaf(state.imagegen_output_path)}"[:160])
    action_row = result.row(align=True)
    action_row.operator("nymphsv2.open_imagegen_folder", text="Open Folder")
    action_row.operator("nymphsv2.clear_imagegen_folder", text="Clear Folder")


class NYMPHSV2_PT_image_generation(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Nymphs"
    bl_label = "Nymphs Image"

    def draw(self, context):
        state = context.scene.nymphs_state
        layout = self.layout

        panel = layout.box()
        image_backend = getattr(state, "imagegen_backend", "Z_IMAGE")
        _draw_imagegen_status_box(panel, state)

        generation_box = panel.box()
        generation_box.prop(
            state,
            "show_image_generation",
            text="Image Generation",
            icon="TRIA_DOWN" if state.show_image_generation else "TRIA_RIGHT",
            emboss=False,
        )
        if state.show_image_generation:
            backend_row = generation_box.row(align=True)
            backend_row.prop(state, "imagegen_backend", expand=True)
            image_backend = getattr(state, "imagegen_backend", "Z_IMAGE")

            if image_backend == "Z_IMAGE" and not _service_runtime_is_available(state, "n2d2"):
                hint = generation_box.box()
                hint.label(text="Start Z-Image in Runtimes.")
                _draw_service_control_row(hint, state, "n2d2")
            else:
                _sync_imagegen_prompt_preset(state)
                if image_backend == "Z_IMAGE":
                    _sync_imagegen_settings_preset(state)

                if image_backend == "Z_IMAGE":
                    _draw_service_control_row(generation_box, state, "n2d2")
                elif not _online_access_enabled():
                    warning = generation_box.box()
                    warning.label(text="Enable Blender online access to use Gemini.")

                request = generation_box.box()

                if image_backend == "Z_IMAGE":
                    settings_label_row = request.row(align=True)
                    settings_label_row.label(text="Profile")
                    settings_label_row.operator("nymphsv2.load_imagegen_settings_preset", text="Apply")
                    settings_preset_row = request.row(align=True)
                    settings_preset_row.prop(state, "imagegen_settings_preset", text="")
                    settings_preset_tools = request.row(align=True)
                    settings_preset_tools.operator("nymphsv2.save_imagegen_settings_preset", text="Save")
                    settings_preset_tools.operator("nymphsv2.delete_imagegen_settings_preset", text="Delete")
                    settings_preset_tools.operator("nymphsv2.open_imagegen_settings_presets_folder", text="Open")
                else:
                    gemini_box = request.box()
                    _ensure_openrouter_api_key_loaded(state)
                    gemini_model_row = gemini_box.split(factor=0.42, align=True)
                    gemini_model_row.label(text="Gemini Flash")
                    gemini_model_row.prop(state, "gemini_model", text="")
                    gemini_box.prop(state, "openrouter_api_key", text="API")
                    gemini_row = gemini_box.row(align=True)
                    gemini_row.prop(state, "gemini_aspect_ratio")
                    if _gemini_model_id(state) in GEMINI_IMAGE_SIZE_MODELS:
                        gemini_row.prop(state, "gemini_image_size")
                    guide_box = gemini_box.box()
                    guide_toggle_row = guide_box.row(align=True)
                    guide_toggle_row.prop(state, "gemini_use_guide_image")
                    if state.gemini_use_guide_image:
                        guide_box.prop(state, "gemini_guide_image_path", text="Guide")
                        guide_actions = guide_box.row(align=True)
                        guide_actions.operator("nymphsv2.pick_gemini_guide_image", text="Pick")
                        guide_actions.operator("nymphsv2.clear_gemini_guide_image", text="Clear")
                    if not (state.openrouter_api_key or os.environ.get("OPENROUTER_API_KEY")):
                        gemini_box.label(text="Uses OPENROUTER_API_KEY when the field is blank.")

                prompts_box = request.box()
                prompts_box.label(text="PROMPTS")
                _sync_imagegen_prompt_preset(state, "imagegen_prompt_preset", PROMPT_KIND_SUBJECT)
                _sync_imagegen_style_preset(state)
                _sync_imagegen_managed_prompt_blocks(state)
                subject_row = prompts_box.row(align=True)
                subject_row.prop(state, "imagegen_prompt_preset", text="Subject")
                style_preset_row = prompts_box.row(align=True)
                style_preset_row.prop(state, "imagegen_style_preset", text="Style")
                prompt_insert_tools = prompts_box.row(align=True)
                prompt_insert_tools.operator("nymphsv2.open_prompt_presets_folder", text="Open")

                prompt_row = request.row(align=True)
                prompt_row.label(text="Manual Prompt Editing")
                prompt_tools_primary = request.row(align=True)
                large_prompt = prompt_tools_primary.operator("nymphsv2.open_image_prompt_text_block", text="Editor")
                large_prompt.target = "prompt"
                use_prompt_text = prompt_tools_primary.operator("nymphsv2.pull_image_prompt_text_block", text="Apply")
                use_prompt_text.target = "prompt"
                prompt_tools_secondary = request.row(align=True)
                edit_prompt = prompt_tools_secondary.operator("nymphsv2.edit_image_prompts", text="Quick Edit")
                edit_prompt.target = "prompt"
                prompt_tools_secondary.operator("nymphsv2.preview_image_prompt", text="Preview")
                clear_prompt = prompt_tools_secondary.operator("nymphsv2.clear_image_prompt_field", text="Clear")
                clear_prompt.target = "prompt"
                if (state.imagegen_prompt or "").strip():
                    _draw_wrapped_lines(request, state.imagegen_prompt, prefix="Text: ", width=52, max_lines=1)
                else:
                    request.label(text="No prompt text yet.")
                _sync_imagegen_prompt_preset(state, "imagegen_saved_prompt_preset", PROMPT_KIND_SAVED)
                saved_prompt_row = request.row(align=True)
                saved_prompt_row.prop(state, "imagegen_saved_prompt_preset", text="Saved Prompt")
                saved_prompt_tools = request.row(align=True)
                saved_prompt_tools.operator("nymphsv2.save_current_prompt", text="Save Current")
                if image_backend == "Z_IMAGE":
                    size_row = request.row(align=True)
                    size_row.prop(state, "imagegen_width")
                    size_row.prop(state, "imagegen_height")
                    settings_row = request.row(align=True)
                    settings_row.prop(state, "imagegen_steps")
                    settings_row.prop(state, "imagegen_guidance_scale")

                    seed_row = request.row(align=True)
                    seed_row.prop(state, "imagegen_seed", text="Seed")
                variant_split = request.split(factor=0.62, align=True)
                variant_row = variant_split.row(align=True)
                variant_row.prop(state, "imagegen_variant_count")
                mv_row = variant_split.row(align=True)
                mv_row.prop(state, "imagegen_generate_mv")
                if image_backend == "Z_IMAGE":
                    variant_row.prop(state, "imagegen_seed_step", text="Step")

                if bool(getattr(state, "imagegen_generate_mv", False)):
                    generate_label = "Generate 4-View MV"
                else:
                    generate_label = "Generate Image" if int(getattr(state, "imagegen_variant_count", 1)) <= 1 else "Generate Variants"
                primary_action = request.row(align=True)
                primary_action.enabled = not state.is_busy and not state.imagegen_is_busy
                primary_action.scale_y = 1.25
                primary_action.operator("nymphsv2.generate_image", text=generate_label, icon="RENDER_STILL")

        if image_backend == "GEMINI":
            parts_box = panel.box()
            parts_box.prop(
                state,
                "show_part_extraction",
                text="Image Part Extraction",
                icon="TRIA_DOWN" if state.show_part_extraction else "TRIA_RIGHT",
                emboss=False,
            )
        if image_backend == "GEMINI" and state.show_part_extraction:
            extraction_source = (state.part_extraction_source_path or "").strip()
            source_name = _path_leaf(extraction_source) if extraction_source else "No source image selected"
            parts_box.label(text="Source Image")
            _draw_wrapped_lines(parts_box, source_name, width=52, max_lines=1)
            choose_row = parts_box.row(align=True)
            choose_row.operator("nymphsv2.pick_part_master_image", text="Choose")

            parts_box.label(text="Plan Parts")
            model_row = parts_box.row(align=True)
            model_row.prop(state, "part_planner_model", text="")
            model_row.prop(state, "part_extraction_max_parts", text="Max")
            plan_row = parts_box.row(align=True)
            plan_row.enabled = not state.is_busy and not state.imagegen_is_busy
            plan_row.operator("nymphsv2.plan_character_parts", text="Plan")
            parts_box.prop(state, "part_extraction_style_lock")
            base_row = parts_box.row(align=True)
            base_row.prop(state, "part_base_include_face", toggle=True)
            eyes_row = base_row.row(align=True)
            eyes_row.enabled = bool(state.part_base_include_face)
            eyes_row.prop(state, "part_base_include_eyes", toggle=True)
            eye_part_row = parts_box.row(align=True)
            eye_part_row.prop(state, "part_include_eye_part", toggle=True)

            parts_box.label(text="Extraction Prompt")
            guidance_tools_primary = parts_box.row(align=True)
            guidance_editor = guidance_tools_primary.operator("nymphsv2.open_image_prompt_text_block", text="Editor")
            guidance_editor.target = "guidance"
            guidance_apply = guidance_tools_primary.operator("nymphsv2.pull_image_prompt_text_block", text="Apply")
            guidance_apply.target = "guidance"
            guidance_tools_secondary = parts_box.row(align=True)
            guidance_quick_edit = guidance_tools_secondary.operator("nymphsv2.edit_image_prompts", text="Quick Edit")
            guidance_quick_edit.target = "guidance"
            guidance_preview = guidance_tools_secondary.operator("nymphsv2.preview_image_prompt", text="Preview")
            guidance_preview.target = "guidance"
            guidance_clear = guidance_tools_secondary.operator("nymphsv2.clear_image_prompt_field", text="Clear")
            guidance_clear.target = "guidance"
            if (state.part_extraction_guidance or "").strip():
                _draw_wrapped_lines(parts_box, state.part_extraction_guidance, prefix="Text: ", width=52, max_lines=1)
            else:
                parts_box.label(text="No guidance text yet.")

            parts_box.label(text="Parts")
            planned_parts = _part_extraction_parts_from_state(state)
            selected_count = sum(1 for item in planned_parts if bool(getattr(item, "selected", True)))
            if planned_parts:
                parts_box.label(text=f"{selected_count}/{len(planned_parts)} selected")
                symmetry_header = parts_box.row(align=True)
                symmetry_header.alignment = "RIGHT"
                symmetry_header.label(text="Symmetry")
                for item in planned_parts:
                    part_label = (item.display_name or item.part_id or "Part").strip()
                    part_row = parts_box.split(factor=0.92, align=True)
                    part_row.prop(item, "selected", text=part_label)
                    part_row.prop(item, "symmetry", text="")
            elif state.part_extraction_plan_path:
                parts_box.label(text="No usable parts in current plan.")
            else:
                parts_box.label(text="No plan yet.")
            parts_actions = parts_box.row(align=True)
            parts_actions.enabled = not state.is_busy and not state.imagegen_is_busy
            extract_action = parts_actions.row(align=True)
            extract_action.enabled = bool(selected_count)
            extract_action.operator("nymphsv2.extract_character_parts", text=f"Extract Selected ({selected_count})")
            parts_actions.operator("nymphsv2.clear_character_part_plan", text="Clear")


def _panel_status_text(state):
    if state.last_result:
        return state.last_result
    active_status = (state.task_status or "").lower()
    if state.is_busy or active_status in {"processing", "queued"}:
        if state.waiting_for_backend_progress or active_status == "queued":
            return "Submitting..."
        return "Running..."

    status_text = (state.status_text or "").strip()
    if not status_text:
        return "Ready"

    lowered = status_text.lower()
    passive_prefixes = (
        "submitting ",
        "launch ",
        "stop ",
        "backend probe",
        "gpu probe",
        "generation finished",
        "mesh imported",
        "local backend is ready",
    )
    passive_exact = {
        "Launch requested.",
        "Stop requested.",
        "Backend probe succeeded.",
        "GPU probe finished.",
        "Local backend is ready.",
        "No managed backend was running.",
        "A managed backend is already running.",
    }
    if status_text in passive_exact or lowered.startswith(passive_prefixes):
        return "Ready"

    return status_text


def _wrap_panel_text(text, width=44, max_lines=4):
    if not text:
        return []
    wrapped_lines = []
    for raw_line in text.strip().splitlines() or [""]:
        line_chunks = textwrap.wrap(
            raw_line,
            width=width,
            break_long_words=False,
            break_on_hyphens=False,
        )
        wrapped_lines.extend(line_chunks or [""])
    if len(wrapped_lines) > max_lines:
        wrapped_lines = wrapped_lines[:max_lines]
        if wrapped_lines:
            last = wrapped_lines[-1]
            max_last = max(1, width - 3)
            wrapped_lines[-1] = (last[:max_last] + "...") if len(last) > max_last else (last + "...")
    return wrapped_lines


def _draw_wrapped_preview(layout, title, text, width=44, max_lines=4):
    if not (text or "").strip():
        return
    preview = layout.box()
    preview.label(text=title)
    for line in _wrap_panel_text(text, width=width, max_lines=max_lines):
        preview.label(text=line)


def _draw_wrapped_lines(layout, text, *, prefix="", width=44, max_lines=4):
    if not (text or "").strip():
        return
    wrapped = _wrap_panel_text(text, width=width, max_lines=max_lines)
    if not wrapped:
        return
    for index, line in enumerate(wrapped):
        if index == 0 and prefix:
            layout.label(text=f"{prefix}{line}")
        else:
            layout.label(text=line)


def _draw_preview_toggle(layout, state, attr_name, title, text, *, width=44, max_lines=4):
    if not (text or "").strip():
        return
    expanded = bool(getattr(state, attr_name, False))
    layout.prop(
        state,
        attr_name,
        text=title,
        icon="TRIA_DOWN" if expanded else "TRIA_RIGHT",
        emboss=False,
    )
    if expanded:
        _draw_wrapped_preview(layout, "Current", text, width=width, max_lines=max_lines)


def _draw_labeled_prop(layout, state, attr_name, label):
    layout.label(text=label)
    layout.prop(state, attr_name, text="")


def _draw_request_status(panel, state):
    status = panel.box()
    _draw_wrapped_lines(status, _panel_status_text(state), prefix="Status: ", width=46, max_lines=4)
    if state.task_stage:
        stage_norm = _normalized_panel_text(state.task_stage)
        if stage_norm and stage_norm not in {"idle", "request sent"}:
            _draw_wrapped_lines(status, state.task_stage, prefix="Current Job: ", width=46, max_lines=3)
    if state.last_result:
        _draw_wrapped_lines(status, state.last_result, width=46, max_lines=4)


def _service_status_line(state, service_key):
    launch_state = _service_get(state, service_key, "launch_state", "Stopped")
    summary = (_service_get(state, service_key, "backend_summary", "Unavailable") or "").strip()
    if launch_state == "Launching":
        return "Launching"
    if launch_state == "Stopping":
        return "Stopping"
    if _service_summary_is_ready(summary):
        return "Ready"
    if _backend_is_alive(service_key):
        return "Running"
    return "Stopped"


def _draw_service_control_row(layout, state, service_key):
    control_row = layout.row(align=True)
    control_row.label(text=_service_status_line(state, service_key))
    start_op = control_row.operator("nymphsv2.start_service", text="Start")
    start_op.service_key = service_key
    stop_op = control_row.operator("nymphsv2.stop_service", text="Stop")
    stop_op.service_key = service_key
    return control_row


def _service_live_lines(state, service_key):
    if service_key == "n2d2":
        return _imagegen_progress_lines(state)
    if service_key == _selected_3d_service_key(state):
        return _progress_lines(state)
    return "", "", ""


def _n2d2_loaded_runtime_label(summary):
    if not _service_summary_is_ready(summary):
        return ""
    if "runtime=" not in summary:
        return ""
    runtime = summary.split("runtime=", 1)[1].split("|", 1)[0].strip()
    if not runtime:
        return ""
    if runtime.lower().startswith("nunchaku"):
        parts = runtime.split()
        for part in parts:
            if part.lower().startswith("r") and part[1:].isdigit():
                return f"Nunchaku {part.lower()}"
        return "Nunchaku"
    return runtime.replace("_", " ").title()


def _n2d2_loaded_model_label(summary):
    if not _service_summary_is_ready(summary):
        return ""
    if "loaded=" not in summary:
        return ""
    model = summary.split("loaded=", 1)[1].split("|", 1)[0].strip()
    if not model or model == "not loaded":
        return ""
    return _path_leaf(model) or model


def _draw_service_block(layout, state, service_key):
    label = _service_display_name(service_key)
    runtime_label = _runtime_card_name(state, service_key)
    show_attr = _service_prop_name(service_key, "show")
    port_attr = _service_prop_name(service_key, "port")
    launch_detail = _service_get(state, service_key, "launch_detail", "")
    summary = _service_get(state, service_key, "backend_summary", "Unavailable")
    live_job, live_detail, live_progress = _service_live_lines(state, service_key)
    detail = (live_detail or launch_detail or "").strip()
    if detail in {"Server ready", "Reusing existing server", f"{label} is not running."}:
        detail = ""
    box = layout.box()

    box.label(text=runtime_label)
    config_row = box.row(align=True)
    config_row.label(text=f"Port: {_service_port(state, service_key)}")
    config_row.prop(
        state,
        show_attr,
        text="Config Details",
        icon="TRIA_DOWN" if getattr(state, show_attr) else "TRIA_RIGHT",
        emboss=False,
    )

    if getattr(state, show_attr):
        details = box.column(align=True)
        _draw_labeled_prop(details, state, port_attr, "Server Port")
        if service_key != "n2d2" and _service_summary_is_ready(summary):
            _draw_wrapped_lines(details, summary, prefix="Summary: ", width=44, max_lines=2)
        if live_job and live_job not in {"Request Sent"}:
            _draw_wrapped_lines(details, live_job, prefix="Current Job: ", width=44, max_lines=3)
        if detail:
            _draw_wrapped_lines(details, detail, prefix="Detail: ", width=44, max_lines=2)
        if live_progress:
            _draw_wrapped_lines(details, live_progress, prefix="Progress: ", width=44, max_lines=2)
        if service_key in {"2mv", "trellis"}:
            target_row = details.row(align=True)
            if service_key == _selected_3d_service_key(state):
                target_row.label(text="Current 3D Target")
            else:
                target_op = target_row.operator("nymphsv2.set_3d_target", text="Use For 3D")
                target_op.service_key = service_key

        if service_key == "2mv":
            _draw_labeled_prop(details, state, "repo_2mv_path", "Repo Path")
            _draw_labeled_prop(details, state, "python_2mv_path", "Python Path")
            row = details.row(align=True)
            row.prop(state, "launch_texture_support")
            row = details.row(align=True)
            row.prop(state, "launch_turbo")
            row.prop(state, "launch_flashvdm")
        elif service_key == "trellis":
            _draw_labeled_prop(details, state, "repo_trellis_path", "Repo Path")
            _draw_labeled_prop(details, state, "trellis_python_path", "Python Path")
        else:
            _draw_labeled_prop(details, state, "repo_n2d2_path", "Repo Path")
            _draw_labeled_prop(details, state, "n2d2_python_path", "Python Path")
            _draw_labeled_prop(details, state, "n2d2_model_preset", "Model Choice")
            if state.n2d2_model_preset == "custom":
                _draw_labeled_prop(details, state, "n2d2_model_id", "Model ID")
                _draw_labeled_prop(details, state, "n2d2_nunchaku_rank", "Nunchaku Rank")
            loaded_runtime = _n2d2_loaded_runtime_label(summary)
            if loaded_runtime:
                details.label(text=f"Loaded: {loaded_runtime}")
            loaded_model = _n2d2_loaded_model_label(summary)
            if loaded_model:
                details.label(text=f"Model: {loaded_model}"[:160])

    _draw_service_control_row(box, state, service_key)


class NYMPHSV2_PT_shape(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Nymphs"
    bl_label = "Nymphs Shape"

    def draw(self, context):
        state = context.scene.nymphs_state
        layout = self.layout

        panel = layout.box()
        backend_row = panel.row(align=True)
        backend_row.prop(state, "launch_backend", expand=True)
        selected_service = _selected_3d_service_key(state)
        caps = _state_server_capabilities(state)
        texture_requested = bool(state.shape_generate_texture and caps["texture"])
        active_family = caps["family"]
        if active_family == "Unknown":
            active_family = _launch_backend_label(state)
        if not _service_runtime_is_available(state, selected_service):
            warn = panel.box()
            warn.label(text=f"Start {_service_display_name(selected_service)} in Runtimes.")
            _draw_service_control_row(warn, state, selected_service)
            return

        _draw_service_control_row(panel, state, selected_service)

        if not caps["shape"] or caps["texture_only"]:
            warn = panel.box()
            warn.label(text="Current server is not exposing shape generation.")
            warn.label(text="Use a shape-capable backend instance.")
            panel.label(text=f"Status: {_panel_status_text(state)}"[:160])
            return

        if active_family == "TRELLIS.2":
            _sync_trellis_shape_preset(state)
            request = panel.box()
            request.label(text="Workflow")
            request.prop(state, "image_path")
            request.prop(state, "auto_remove_background")
            texture_row = request.row()
            texture_row.enabled = caps["texture"]
            texture_row.prop(state, "shape_generate_texture")

            trellis_opts = panel.box()
            preset_row = trellis_opts.row(align=True)
            preset_row.prop(state, "trellis_shape_preset", text="")
            preset_row.operator("nymphsv2.load_trellis_shape_preset", text="Apply")
            preset_tools = trellis_opts.row(align=True)
            preset_tools.operator("nymphsv2.save_trellis_shape_preset", text="Save")
            preset_tools.operator("nymphsv2.delete_trellis_shape_preset", text="Delete")
            preset_tools.operator("nymphsv2.open_trellis_shape_presets_folder", text="Open")
            trellis_opts.prop(state, "trellis_pipeline_type")
            seed_row = trellis_opts.row(align=True)
            seed_row.prop(state, "trellis_seed", text="Seed")
            trellis_opts.prop(state, "trellis_max_tokens", text="Tokens")

            sparse_box = trellis_opts.box()
            sparse_box.label(text="Early Pass")
            row = sparse_box.row(align=True)
            row.prop(state, "trellis_ss_sampling_steps", text="Steps")
            row.prop(state, "trellis_ss_guidance_strength", text="Image")
            sparse_box.prop(state, "trellis_ss_guidance_rescale", text="Rescale")
            interval_row = sparse_box.row(align=True)
            interval_row.prop(state, "trellis_ss_guidance_interval_start", text="Start")
            interval_row.prop(state, "trellis_ss_guidance_interval_end", text="End")
            sparse_box.prop(state, "trellis_ss_rescale_t", text="Timing")

            shape_box = trellis_opts.box()
            shape_box.label(text="Shape Pass")
            row = shape_box.row(align=True)
            row.prop(state, "trellis_shape_sampling_steps", text="Steps")
            row.prop(state, "trellis_shape_guidance_strength", text="Image")
            shape_box.prop(state, "trellis_shape_guidance_rescale", text="Rescale")
            interval_row = shape_box.row(align=True)
            interval_row.prop(state, "trellis_shape_guidance_interval_start", text="Start")
            interval_row.prop(state, "trellis_shape_guidance_interval_end", text="End")
            shape_box.prop(state, "trellis_shape_rescale_t", text="Timing")

            if state.shape_generate_texture:
                texture_box = trellis_opts.box()
                texture_box.label(text="Texture Pass")
                row = texture_box.row(align=True)
                row.prop(state, "trellis_tex_sampling_steps", text="Steps")
                row.prop(state, "trellis_tex_guidance_strength", text="Image")
                texture_box.prop(state, "trellis_tex_guidance_rescale", text="Rescale")
                interval_row = texture_box.row(align=True)
                interval_row.prop(state, "trellis_tex_guidance_interval_start", text="Start")
                interval_row.prop(state, "trellis_tex_guidance_interval_end", text="End")
                texture_box.prop(state, "trellis_tex_rescale_t", text="Timing")
                texture_box.prop(state, "trellis_texture_size")
                texture_box.prop(state, "trellis_decimation_target", text="Faces")

            action = panel.row()
            action.enabled = not state.is_busy and not state.imagegen_is_busy
            action.operator(
                "nymphsv2.run_shape_request",
                text="Generate Shape + Texture" if texture_requested else "Generate Shape",
            )

            if state.shape_output_path:
                result = panel.box()
                if state.shape_output_dir:
                    result.label(text=f"Folder: {_path_leaf(state.shape_output_dir) or 'shape_outputs'}")
                result.label(text=f"Last Mesh: {_path_leaf(state.shape_output_path)}")
                action_row = result.row(align=True)
                action_row.operator("nymphsv2.open_shape_folder", text="Open Folder")
                action_row.operator("nymphsv2.clear_shape_folder", text="Clear Folder")

            _draw_request_status(panel, state)
            return

        request = panel.box()
        request.label(text="Workflow")
        request.prop(state, "shape_workflow")

        text_row = request.row()
        text_row.enabled = caps["text"] or state.shape_workflow != "TEXT"
        text_row.prop(state, "prompt")
        if state.shape_workflow == "TEXT" and not caps["text"]:
            request.label(text="This server was not started with text support.")

        if state.shape_workflow == "IMAGE":
            request.prop(state, "image_path")
        elif state.shape_workflow == "MULTIVIEW":
            if not caps["multiview"]:
                request.label(text="Current server does not expose multiview.")
            if any((state.mv_front, state.mv_left, state.mv_right, state.mv_back)):
                source = (state.imagegen_mv_received_source or "").strip()
                stamp = _format_clock_time(state.imagegen_mv_received_at)
                if source and stamp:
                    request.label(text=f"Received from {source} at {stamp}")
                elif source:
                    request.label(text=f"Received from {source}")
            request.prop(state, "mv_front")
            request.prop(state, "mv_back")
            request.prop(state, "mv_left")
            request.prop(state, "mv_right")

        request.prop(state, "auto_remove_background")
        texture_row = request.row()
        texture_row.enabled = caps["texture"]
        texture_row.prop(state, "shape_generate_texture")
        if not caps["texture"]:
            request.label(text="This server was started without texture support.")
            if state.shape_generate_texture:
                request.label(text="Shape requests will run without texture on this server.")
        request.prop(state, "mesh_detail")
        request.prop(state, "detail_passes")
        request.prop(state, "reference_strength")

        action = panel.row()
        workflow_supported = True
        if state.shape_workflow == "MULTIVIEW" and not caps["multiview"]:
            workflow_supported = False
        if state.shape_workflow == "TEXT" and not caps["text"]:
            workflow_supported = False
        action.enabled = not state.is_busy and not state.imagegen_is_busy and workflow_supported
        action.operator(
            "nymphsv2.run_shape_request",
            text="Generate Shape + Texture" if texture_requested else "Generate Shape",
        )

        if state.shape_output_path:
            result = panel.box()
            if state.shape_output_dir:
                result.label(text=f"Folder: {_path_leaf(state.shape_output_dir) or 'shape_outputs'}")
            result.label(text=f"Last Mesh: {_path_leaf(state.shape_output_path)}")
            action_row = result.row(align=True)
            action_row.operator("nymphsv2.open_shape_folder", text="Open Folder")
            action_row.operator("nymphsv2.clear_shape_folder", text="Clear Folder")

        _draw_request_status(panel, state)


class NYMPHSV2_PT_texture(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Nymphs"
    bl_label = "Nymphs Texture"

    def draw(self, context):
        state = context.scene.nymphs_state
        layout = self.layout
        panel = layout.box()

        backend_row = panel.row(align=True)
        backend_row.prop(state, "texture_backend", expand=True)
        selected_service = _selected_texture_service_key(state)
        caps = _service_capabilities_from_summary(state, selected_service)
        active_family = caps["family"]
        if active_family == "Unknown":
            active_family = "TRELLIS.2" if selected_service == "trellis" else "2mv"
        if not _service_runtime_is_available(state, selected_service):
            warn = panel.box()
            warn.label(text=f"Start {_service_display_name(selected_service)} in Runtimes.")
            _draw_service_control_row(warn, state, selected_service)
            return

        _draw_service_control_row(panel, state, selected_service)

        info = panel.box()
        info.label(text=f"Retexture Selected Mesh with {_service_display_name(selected_service)}")
        if not caps["texture"]:
            info.label(text="Current server does not expose texture generation.")
            return
        elif not caps["retexture"]:
            if active_family == "TRELLIS.2":
                info.label(text="TRELLIS retexture is unavailable.")
            else:
                info.label(text="Selected-mesh retexture is unavailable.")
            return
        elif caps["texture_only"]:
            info.label(text="Texture-only server mode is active.")

        if active_family == "TRELLIS.2":
            request = panel.box()
            request.label(text="Texture Guidance")
            request.prop(state, "image_path")
            request.prop(state, "auto_remove_background")

            texture_opts = panel.box()
            texture_opts.label(text="TRELLIS Texture Options")
            texture_opts.prop(state, "trellis_texture_resolution")
            texture_opts.prop(state, "trellis_texture_size")
            texture_opts.prop(state, "trellis_seed")
            row = texture_opts.row(align=True)
            row.prop(state, "trellis_tex_sampling_steps", text="Steps")
            row.prop(state, "trellis_tex_guidance_strength", text="Follow Image")
            texture_opts.prop(
                state,
                "show_texture_settings",
                text="Advanced Texture Controls",
                icon="TRIA_DOWN" if state.show_texture_settings else "TRIA_RIGHT",
                emboss=False,
            )
            if state.show_texture_settings:
                texture_opts.prop(state, "trellis_tex_guidance_rescale", text="Guidance Rescale")
                interval_row = texture_opts.row(align=True)
                interval_row.prop(state, "trellis_tex_guidance_interval_start", text="Start")
                interval_row.prop(state, "trellis_tex_guidance_interval_end", text="End")
                texture_opts.prop(state, "trellis_tex_rescale_t", text="Rescale T")
                texture_opts.prop(state, "trellis_decimation_target")

            action = panel.row()
            action.enabled = not state.is_busy and not state.imagegen_is_busy and caps["retexture"]
            action.operator("nymphsv2.run_texture_request", text="Retexture Selected Mesh")

            _draw_request_status(panel, state)
            return

        request = panel.box()
        request.label(text="Texture Guidance")
        source = (state.imagegen_mv_received_source or "").strip()
        stamp = _format_clock_time(state.imagegen_mv_received_at)
        if source and stamp:
            request.label(text=f"Received from {source} at {stamp}")
        elif source:
            request.label(text=f"Received from {source}")
        request.prop(state, "mv_front")
        request.prop(state, "mv_back")
        request.prop(state, "mv_left")
        request.prop(state, "mv_right")
        request.prop(state, "auto_remove_background")

        texture_opts = panel.box()
        texture_opts.label(text="Hunyuan Texture Options")
        texture_opts.prop(
            state,
            "show_texture_settings",
            text="Texture Options",
            icon="TRIA_DOWN" if state.show_texture_settings else "TRIA_RIGHT",
            emboss=False,
        )
        if state.show_texture_settings:
            texture_opts.prop(state, "texture_face_limit")
            size_col = texture_opts.column(align=True)
            size_col.prop(state, "texture_resolution_2mv")

        action = panel.row()
        action.enabled = not state.is_busy and not state.imagegen_is_busy and caps["retexture"]
        action.operator("nymphsv2.run_texture_request", text="Retexture Selected Mesh")

        _draw_request_status(panel, state)



CLASSES = (
    NymphsPartPlanItem,
    NymphsV2State,
    NYMPHSV2_OT_probe_backend,
    NYMPHSV2_OT_probe_gpu,
    NYMPHSV2_OT_stop_backend,
    NYMPHSV2_OT_start_service,
    NYMPHSV2_OT_stop_service,
    NYMPHSV2_OT_set_3d_target,
    NYMPHSV2_OT_run_shape_request,
    NYMPHSV2_OT_run_texture_request,
    NYMPHSV2_OT_generate_image,
    NYMPHSV2_OT_generate_mv_set,
    NYMPHSV2_OT_clear_managed_prompt_presets,
    NYMPHSV2_OT_insert_saved_prompt,
    NYMPHSV2_OT_save_current_prompt,
    NYMPHSV2_OT_load_prompt_preset,
    NYMPHSV2_OT_load_imagegen_settings_preset,
    NYMPHSV2_OT_save_imagegen_settings_preset,
    NYMPHSV2_OT_delete_imagegen_settings_preset,
    NYMPHSV2_OT_open_imagegen_settings_presets_folder,
    NYMPHSV2_OT_save_prompt_preset,
    NYMPHSV2_OT_delete_prompt_preset,
    NYMPHSV2_OT_open_prompt_presets_folder,
    NYMPHSV2_OT_load_style_preset,
    NYMPHSV2_OT_save_style_preset,
    NYMPHSV2_OT_delete_style_preset,
    NYMPHSV2_OT_open_style_presets_folder,
    NYMPHSV2_OT_clear_image_style_field,
    NYMPHSV2_OT_load_trellis_shape_preset,
    NYMPHSV2_OT_save_trellis_shape_preset,
    NYMPHSV2_OT_delete_trellis_shape_preset,
    NYMPHSV2_OT_open_trellis_shape_presets_folder,
    NYMPHSV2_OT_clear_image_prompt_field,
    NYMPHSV2_OT_open_image_prompt_text_block,
    NYMPHSV2_OT_pull_image_prompt_text_block,
    NYMPHSV2_OT_edit_image_prompts,
    NYMPHSV2_OT_preview_image_prompt,
    NYMPHSV2_OT_pick_gemini_guide_image,
    NYMPHSV2_OT_clear_gemini_guide_image,
    NYMPHSV2_OT_pick_part_master_image,
    NYMPHSV2_OT_use_current_image_as_part_master,
    NYMPHSV2_OT_use_last_generated_as_part_master,
    NYMPHSV2_OT_plan_character_parts,
    NYMPHSV2_OT_extract_character_parts,
    NYMPHSV2_OT_clear_character_part_plan,
    NYMPHSV2_OT_open_imagegen_folder,
    NYMPHSV2_OT_clear_imagegen_folder,
    NYMPHSV2_OT_open_shape_folder,
    NYMPHSV2_OT_clear_shape_folder,
    NYMPHSV2_PT_server,
    NYMPHSV2_PT_image_generation,
    NYMPHSV2_PT_shape,
    NYMPHSV2_PT_texture,
)


def _safe_remove_scene_state() -> None:
    if hasattr(bpy.types.Scene, "nymphs_state"):
        try:
            del bpy.types.Scene.nymphs_state
        except Exception:
            pass


def _safe_unregister_registered_class(cls) -> None:
    registered_cls = getattr(bpy.types, cls.__name__, None)
    if registered_cls is None:
        return
    try:
        bpy.utils.unregister_class(registered_cls)
    except Exception:
        pass


def register():
    global ATEXIT_REGISTERED
    _safe_remove_scene_state()
    for cls in reversed(CLASSES):
        _safe_unregister_registered_class(cls)
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.nymphs_state = PointerProperty(type=NymphsV2State)
    if not bpy.app.timers.is_registered(_server_probe_timer):
        bpy.app.timers.register(_server_probe_timer, persistent=True)
    if not bpy.app.timers.is_registered(_active_probe_timer):
        bpy.app.timers.register(_active_probe_timer, persistent=True)
    if not bpy.app.timers.is_registered(_gpu_probe_timer):
        bpy.app.timers.register(_gpu_probe_timer, persistent=True)
    if not bpy.app.timers.is_registered(_ui_refresh_timer):
        bpy.app.timers.register(_ui_refresh_timer, persistent=True)
    if not ATEXIT_REGISTERED:
        atexit.register(_shutdown_managed_backends)
        ATEXIT_REGISTERED = True


def unregister():
    global ATEXIT_REGISTERED
    _shutdown_managed_backends()
    if bpy.app.timers.is_registered(_drain_events):
        bpy.app.timers.unregister(_drain_events)
    if bpy.app.timers.is_registered(_server_probe_timer):
        bpy.app.timers.unregister(_server_probe_timer)
    if bpy.app.timers.is_registered(_active_probe_timer):
        bpy.app.timers.unregister(_active_probe_timer)
    if bpy.app.timers.is_registered(_gpu_probe_timer):
        bpy.app.timers.unregister(_gpu_probe_timer)
    if bpy.app.timers.is_registered(_ui_refresh_timer):
        bpy.app.timers.unregister(_ui_refresh_timer)
    if ATEXIT_REGISTERED:
        try:
            atexit.unregister(_shutdown_managed_backends)
        except Exception:
            pass
        ATEXIT_REGISTERED = False
    _safe_remove_scene_state()
    for cls in reversed(CLASSES):
        _safe_unregister_registered_class(cls)


if __name__ == "__main__":
    register()
