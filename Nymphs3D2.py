"""
Live Blender addon implementation for Nymphs3D2.
"""

bl_info = {
    "name": "Nymphs3D2",
    "author": "Nymphs3D",
    "version": (1, 1, 109),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > Nymphs",
    "description": "Blender client for local or remote Nymphs3D backends",
    "category": "3D View",
}

import base64
import atexit
import json
import ntpath
import os
import queue
import re
import signal
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
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)


LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
EVENT_QUEUE = queue.Queue()
PROCESS_LOCK = threading.Lock()
PARTS_PROCESS_LOCK = threading.Lock()
CACHE_LOCK = threading.Lock()
PARTS_ACTIVE_PROCS = {}
PARTS_STOP_REQUESTS = set()
MANAGED_BACKENDS = {}
MANAGED_BACKEND_TARGETS = {}
TRANSIENT_CACHE = {}
ATEXIT_REGISTERED = False
STAGE_LINE = re.compile(r"STAGE:\s*([A-Za-z0-9_]+)")
PROGRESS_LINE = re.compile(r"PROGRESS:\s*([A-Za-z0-9_ -]+)\s+(\d+)/(\d+)")
PARTS_TQDM_PROGRESS_LINE = re.compile(
    r"^(?P<label>.+?)\s+(?P<percent>\d+)%\|.*?\|\s*(?P<current>\d+)/(?P<total>\d+)"
)
PARTS_ELAPSED_LINE = re.compile(r"Still working\.\.\.\s*(?P<seconds>\d+)s elapsed")
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
PARTS_REPO_STATUS_CACHE_TTL_SECONDS = 3.0
WSL_DISTRO_CACHE_TTL_SECONDS = 15.0
LOG_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[^\|]*\|\s*[A-Z]+\s*\|\s*(?:stdout|stderr|[^|]+)\s*\|\s*")
LOCAL_SHAPE_OUTPUT_DIRNAME = "nymphs3d2_shape_outputs"
LOCAL_PARTS_OUTPUT_DIRNAME = "nymphs3d2_parts_sources"
CACHE_MISS = object()

DEFAULT_WSL_DISTRO = "NymphsCore"
DEFAULT_WSL_USER = "nymph"
DEFAULT_REPO_2MV_PATH = "~/Hunyuan3D-2"
DEFAULT_REPO_N2D2_PATH = "~/Z-Image"
DEFAULT_REPO_TRELLIS_PATH = "~/TRELLIS.2"
DEFAULT_REPO_PARTS_PATH = "~/Hunyuan3D-Part"
DEFAULT_2MV_PYTHON_PATH = "~/Hunyuan3D-2/.venv/bin/python"
DEFAULT_N2D2_PYTHON_PATH = "~/Z-Image/.venv-nunchaku/bin/python"
DEFAULT_TRELLIS_PYTHON_PATH = "~/TRELLIS.2/.venv/bin/python"
DEFAULT_PARTS_PYTHON_PATH = "~/Hunyuan3D-Part/.venv-official/bin/python"
DEFAULT_N2D2_MODEL_ID = "Tongyi-MAI/Z-Image-Turbo"
DEFAULT_N2D2_MODEL_VARIANT = ""
DEFAULT_N2D2_NUNCHAKU_RANK = "32"
DEFAULT_N2D2_MODEL_PRESET = "zimage_nunchaku_r32"
DEFAULT_IMAGEGEN_PROMPT_PRESET = "clean_asset_concept"
DEFAULT_IMAGEGEN_PROMPT_TEXT_NAME = "Nymphs Image Prompt"
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
        "label": "Clean Asset Concept",
        "description": "General-purpose asset prompt for props, creatures, buildings, and objects",
        "prompt": (
            "single game asset concept, centered subject, isolated on a plain light background, "
            "clean readable silhouette, whole subject visible, consistent design, soft even lighting, "
            "minimal shadows, no scenery, no text, high quality concept art, clear shape language, "
            "designed for 3D modeling reference"
        ),
    },
    "stylized_prop": {
        "label": "Stylized Prop",
        "description": "Painterly prop/object prompt for game assets",
        "prompt": (
            "single stylized game prop, centered, isolated on a plain light background, clean silhouette, "
            "hand-painted fantasy asset style, readable materials, simple appealing shapes, soft even lighting, "
            "clear front-facing design, no scenery, no text, high quality concept art"
        ),
    },
    "character_asset": {
        "label": "Character Asset",
        "description": "Readable full-character prompt for image-to-3D workflows",
        "prompt": (
            "single original character concept, full body, centered, head to toe visible, clean silhouette, "
            "simple plain light background, soft even lighting, readable costume design, clear limb separation, "
            "appealing stylized proportions, no scenery, no props crossing the body, high quality game character concept art"
        ),
    },
    "character_parts_breakout": {
        "label": "Character Parts Breakout",
        "description": "Character sheet prompt with separate clothing and weapon pieces laid out clearly",
        "prompt": (
            "single character equipment breakdown sheet, one full-body character wearing the full outfit, plus separate isolated "
            "breakout views of every clothing piece, accessory, armor piece, and weapon on the same canvas, all items shown to "
            "consistent scale, neatly arranged, non-overlapping, plain light background, soft even lighting, clean readable "
            "silhouettes, clear material definition, concept art presentation sheet for 3D asset production, no scenery, no text"
        ),
    },
    "creature_asset": {
        "label": "Creature Asset",
        "description": "Creature/monster prompt with a readable whole-body shape",
        "prompt": (
            "single fantasy creature concept, whole creature visible, centered, isolated on a plain light background, "
            "clean readable silhouette, clear anatomy, distinctive shape language, soft even lighting, minimal shadows, "
            "stylized game art, designed as a 3D creature reference, no scenery, no text"
        ),
    },
    "building_asset": {
        "label": "Building Asset",
        "description": "Isolated building or environment-piece prompt",
        "prompt": (
            "single stylized building asset, centered, isolated on a plain light background, full structure visible, "
            "clean silhouette, readable roof and wall shapes, clear material zones, soft even lighting, "
            "fantasy game environment concept art, designed for 3D modeling reference, no surrounding scene, no text"
        ),
    },
    "hard_surface_asset": {
        "label": "Hard Surface Asset",
        "description": "Vehicle, machine, robot, or sci-fi object prompt",
        "prompt": (
            "single hard-surface asset concept, centered, isolated on a plain light background, whole object visible, "
            "clean silhouette, readable mechanical forms, consistent proportions, clear panel lines and material breaks, "
            "soft studio lighting, high quality game asset concept art, no scenery, no text"
        ),
    },
}
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
    parts_import: bool = False
    collection_name: str = ""


def _active_state(scene_name):
    scene = bpy.data.scenes.get(scene_name)
    if scene is None:
        return None
    return getattr(scene, "nymphs3d2v2_state", None)


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
    if _parts_run_is_active(state, scene_name):
        return True
    return False


def _ui_refresh_timer():
    for scene in bpy.data.scenes:
        state = getattr(scene, "nymphs3d2v2_state", None)
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
    parts_import=False,
    collection_name="",
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
                parts_import,
                collection_name,
            ),
            None,
        )
    )
    _schedule_event_loop()


def _set_parts_process(scene_name, proc):
    with PARTS_PROCESS_LOCK:
        if proc is None:
            PARTS_ACTIVE_PROCS.pop(scene_name, None)
        else:
            PARTS_ACTIVE_PROCS[scene_name] = proc


def _get_parts_process(scene_name):
    with PARTS_PROCESS_LOCK:
        return PARTS_ACTIVE_PROCS.get(scene_name)


def _clear_parts_process(scene_name, proc=None):
    with PARTS_PROCESS_LOCK:
        current = PARTS_ACTIVE_PROCS.get(scene_name)
        if proc is None or current is proc:
            PARTS_ACTIVE_PROCS.pop(scene_name, None)


def _mark_parts_stop_requested(scene_name):
    with PARTS_PROCESS_LOCK:
        PARTS_STOP_REQUESTS.add(scene_name)


def _consume_parts_stop_requested(scene_name):
    with PARTS_PROCESS_LOCK:
        if scene_name in PARTS_STOP_REQUESTS:
            PARTS_STOP_REQUESTS.discard(scene_name)
            return True
        return False


def _clear_parts_stop_requested(scene_name):
    with PARTS_PROCESS_LOCK:
        PARTS_STOP_REQUESTS.discard(scene_name)


def _parts_process_is_active(scene_name):
    proc = _get_parts_process(scene_name)
    if proc is None:
        return False
    try:
        return proc.poll() is None
    except Exception:
        return False


def _parts_run_is_active(state=None, scene_name=""):
    resolved_scene = (scene_name or "").strip()
    if not resolved_scene and state is not None:
        owner = getattr(state, "id_data", None)
        resolved_scene = getattr(owner, "name", "") or ""
    if resolved_scene and _parts_process_is_active(resolved_scene):
        return True
    if state is None:
        return False
    return float(getattr(state, "parts_started_at", 0.0) or 0.0) > 0.0


def _terminate_parts_process(scene_name):
    proc = _get_parts_process(scene_name)
    if proc is None:
        return False
    try:
        if proc.poll() is not None:
            _clear_parts_process(scene_name, proc)
            return False
    except Exception:
        _clear_parts_process(scene_name, proc)
        return False

    _mark_parts_stop_requested(scene_name)
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            return False
    return True


def _encode_object_name_list(names):
    cleaned = []
    for name in names or []:
        value = (name or "").strip()
        if value and value not in cleaned:
            cleaned.append(value)
    return "\n".join(cleaned)


def _decode_object_name_list(raw_value):
    if not raw_value:
        return []
    return [line.strip() for line in str(raw_value).splitlines() if line.strip()]


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


def _selected_mesh_root_names(context):
    selected_meshes = [obj for obj in getattr(context, "selected_objects", []) if getattr(obj, "type", "") == "MESH"]
    if not selected_meshes:
        return []
    roots = []
    for obj in selected_meshes:
        root = _object_root(obj) or obj
        if root is not None and root.name not in roots:
            roots.append(root.name)
    return roots


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
        elif kind == "import":
            _import_result(payload_a)

    if processed:
        _touch_ui()

    if not EVENT_QUEUE.empty():
        return 0.1

    for scene in bpy.data.scenes:
        state = getattr(scene, "nymphs3d2v2_state", None)
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


def _http_call(method, url, payload=None, timeout=10):
    body = None
    headers = {}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = Request(url=url, data=body, headers=headers, method=method)
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


def _imagegen_preset_dir():
    return bpy.utils.user_resource(
        "CONFIG",
        path=os.path.join("nymphs3d2", "image_presets"),
        create=True,
    )


def _imagegen_preset_slug(name):
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
    return slug or "prompt_preset"


def _imagegen_preset_file(key):
    return os.path.join(_imagegen_preset_dir(), f"{_imagegen_preset_slug(key)}.json")


def _seed_imagegen_prompt_presets():
    try:
        preset_dir = _imagegen_preset_dir()
    except Exception:
        return
    marker = os.path.join(preset_dir, ".defaults_seeded")
    if os.path.exists(marker):
        return
    for key, data in IMAGEGEN_PROMPT_PRESETS.items():
        path = _imagegen_preset_file(key)
        if os.path.exists(path):
            continue
        payload = {
            "name": data["label"],
            "prompt": data["prompt"],
        }
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
    with open(marker, "w", encoding="utf-8") as handle:
        handle.write("Defaults seeded.\n")


def _load_imagegen_prompt_presets():
    preset_dir = _imagegen_preset_dir()

    def _load():
        presets = {
            key: {
                "label": data["label"],
                "description": data["description"],
                "prompt": data["prompt"],
            }
            for key, data in IMAGEGEN_PROMPT_PRESETS.items()
        }
        try:
            _seed_imagegen_prompt_presets()
            for filename in sorted(os.listdir(preset_dir)):
                if not filename.lower().endswith(".json"):
                    continue
                key = os.path.splitext(filename)[0]
                if key in IMAGEGEN_PROMPT_PRESETS:
                    continue
                path = os.path.join(preset_dir, filename)
                try:
                    with open(path, "r", encoding="utf-8") as handle:
                        data = json.load(handle)
                except Exception:
                    continue
                name = str(data.get("name") or key.replace("_", " ").title()).strip()
                prompt = str(data.get("prompt") or "").strip()
                if not prompt:
                    continue
                presets[key] = {
                    "label": name,
                    "description": f"Prompt preset file: {filename}",
                    "prompt": prompt,
                }
        except Exception:
            pass
        return presets

    return _transient_cached("imagegen_prompt_presets", preset_dir, PRESET_CACHE_TTL_SECONDS, _load)


def _imagegen_prompt_preset_data(preset_key):
    presets = _load_imagegen_prompt_presets()
    resolved_key = (preset_key or "").strip()
    if resolved_key in presets:
        return presets[resolved_key]
    if DEFAULT_IMAGEGEN_PROMPT_PRESET in presets:
        return presets[DEFAULT_IMAGEGEN_PROMPT_PRESET]
    return next(iter(presets.values()), {"label": "No Preset", "prompt": ""})


def _imagegen_prompt_preset_items(self, context):
    presets = _load_imagegen_prompt_presets()
    if not presets:
        return (("__none__", "No Presets", "No prompt presets found"),)
    return tuple((key, data["label"], data["description"]) for key, data in presets.items())


def _resolve_imagegen_prompt_preset_key(preset_key):
    presets = _load_imagegen_prompt_presets()
    resolved_key = (preset_key or "").strip()
    if resolved_key in presets:
        return resolved_key
    if DEFAULT_IMAGEGEN_PROMPT_PRESET in presets:
        return DEFAULT_IMAGEGEN_PROMPT_PRESET
    return next(iter(presets.keys()), "__none__")


def _sync_imagegen_prompt_preset(state):
    key = _resolve_imagegen_prompt_preset_key(getattr(state, "imagegen_prompt_preset", ""))
    try:
        if getattr(state, "imagegen_prompt_preset", "") != key:
            state.imagegen_prompt_preset = key
    except Exception:
        pass
    return key


def _imagegen_settings_preset_dir():
    return bpy.utils.user_resource(
        "CONFIG",
        path=os.path.join("nymphs3d2", "image_settings_presets"),
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
        path=os.path.join("nymphs3d2", "trellis_shape_presets"),
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
    return "imagegen_prompt"


def _imagegen_text_name_field(target):
    return "imagegen_prompt_text_name"


def _default_imagegen_text_name(target):
    return DEFAULT_IMAGEGEN_PROMPT_TEXT_NAME


def _linked_imagegen_text(state, target):
    stored_name = (getattr(state, _imagegen_text_name_field(target), "") or "").strip()
    if stored_name:
        return bpy.data.texts.get(stored_name)
    return None


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
    if created or not text.as_string().strip():
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
    setattr(state, _imagegen_text_field_name(target), _text_to_prompt_value(text))
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


def _shape_output_path(source_object_name=""):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    stem = _sanitize_name_fragment(source_object_name, fallback="shape")
    candidate = os.path.join(_shape_output_dir(), f"{stamp}-{stem}.glb")
    if not os.path.exists(candidate):
        return candidate
    return os.path.join(_shape_output_dir(), f"{stamp}-{stem}-{int(time.time() * 1000) % 100000}.glb")


def _parts_output_dir():
    path = os.path.join(tempfile.gettempdir(), LOCAL_PARTS_OUTPUT_DIRNAME)
    os.makedirs(path, exist_ok=True)
    return path


def _parts_output_path(source_name="", export_format="glb"):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    stem = _sanitize_name_fragment(source_name, fallback="parts-source")
    suffix = f".{(export_format or 'glb').strip().lower()}"
    candidate = os.path.join(_parts_output_dir(), f"{stamp}-{stem}{suffix}")
    if not os.path.exists(candidate):
        return candidate
    return os.path.join(_parts_output_dir(), f"{stamp}-{stem}-{int(time.time() * 1000) % 100000}{suffix}")


def _parts_result_root_wsl(state=None):
    return _resolved_user_path("~/.cache/hunyuan3d-part/outputs", state)


def _parts_result_dir_wsl(state=None, source_name="", backend_label="parts"):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    stem = _sanitize_name_fragment(source_name, fallback="parts")
    backend = _sanitize_name_fragment(backend_label, fallback="parts")
    return f"{_parts_result_root_wsl(state)}/{stamp}-{backend}-{stem}"


def _parts_result_root_host(state=None):
    return _to_blender_accessible_path(state, _parts_result_root_wsl(state))


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


def _parts_preserved_run_dirs(state=None, extra_preserve=None):
    preserved = set()
    if extra_preserve:
        for raw_path in extra_preserve:
            path = (raw_path or "").strip()
            if not path:
                continue
            if os.path.isdir(path):
                preserved.add(os.path.abspath(path))
            else:
                preserved.add(os.path.abspath(os.path.dirname(path)))
    if state is None:
        return preserved
    for attr_name in (
        "parts_output_dir",
        "parts_output_path",
        "parts_stage1_manifest_path",
        "parts_stage1_summary_path",
    ):
        path = (getattr(state, attr_name, "") or "").strip()
        if not path:
            continue
        if os.path.isdir(path):
            preserved.add(os.path.abspath(path))
        else:
            preserved.add(os.path.abspath(os.path.dirname(path)))
    return preserved


def _prune_parts_result_dirs(state=None, keep_recent=0, extra_preserve=None):
    root_path = _parts_result_root_host(state)
    if not root_path or not os.path.isdir(root_path):
        return 0
    keep_recent = max(0, int(keep_recent or 0))
    preserved = _parts_preserved_run_dirs(state, extra_preserve=extra_preserve)
    removed = 0
    retained_recent = 0
    candidates = [
        entry.path
        for entry in os.scandir(root_path)
        if entry.is_dir(follow_symlinks=False)
    ]
    candidates.sort(key=lambda path: os.path.getmtime(path), reverse=True)
    for path in candidates:
        normalized = os.path.abspath(path)
        if normalized in preserved:
            continue
        if retained_recent < keep_recent:
            retained_recent += 1
            continue
        shutil.rmtree(path)
        removed += 1
    return removed


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
    prompt = state.imagegen_prompt.strip()
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
    base_prompt = state.imagegen_prompt.strip()
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


def _parts_source_summary(context, state):
    source_mode = (getattr(state, "parts_source_mode", "SELECTED") or "SELECTED").upper()
    if source_mode == "LATEST":
        latest_path = (getattr(state, "shape_output_path", "") or "").strip()
        if latest_path:
            return True, f"Latest Result: {_path_leaf(latest_path)}"
        selected_meshes = [obj for obj in getattr(context, "selected_objects", []) if getattr(obj, "type", "") == "MESH"]
        if selected_meshes:
            active_object = getattr(context, "active_object", None)
            source_object = active_object if active_object in selected_meshes else selected_meshes[0]
            return True, f"Latest Result unavailable. Fallback: {source_object.name}"
        return False, "No latest generated mesh is available yet."

    selected_meshes = [obj for obj in getattr(context, "selected_objects", []) if getattr(obj, "type", "") == "MESH"]
    if not selected_meshes:
        return False, "Select a mesh object first."
    active_object = getattr(context, "active_object", None)
    source_object = active_object if active_object in selected_meshes else selected_meshes[0]
    return True, f"Selected Mesh: {source_object.name}"


def _parts_repo_status(state):
    repo_path = _resolved_user_path(
        getattr(state, "parts_repo_path", DEFAULT_REPO_PARTS_PATH).strip() or DEFAULT_REPO_PARTS_PATH,
        state,
    )
    python_path = _resolved_user_path(
        getattr(state, "parts_python_path", DEFAULT_PARTS_PYTHON_PATH).strip() or DEFAULT_PARTS_PYTHON_PATH,
        state,
    )
    cache_key = (
        _resolved_wsl_distro_name(state),
        _resolved_wsl_user_name(state),
        repo_path,
        python_path,
    )

    def _load():
        repo_status_path = _to_blender_accessible_path(state, repo_path)
        python_status_path = _to_blender_accessible_path(state, python_path)
        repo_exists = _blender_path_is_dir(state, repo_status_path)
        python_exists = _blender_path_exists(state, python_status_path)
        p3_sam_exists = repo_exists and _blender_path_is_dir(
            state, _to_blender_accessible_path(state, os.path.join(repo_path, "P3-SAM"))
        )
        xpart_exists = repo_exists and _blender_path_is_dir(
            state, _to_blender_accessible_path(state, os.path.join(repo_path, "XPart"))
        )
        p3_sam_demo = p3_sam_exists and _blender_path_is_file(
            state, _to_blender_accessible_path(state, os.path.join(repo_path, "P3-SAM", "demo", "auto_mask.py"))
        )
        xpart_demo = xpart_exists and _blender_path_is_file(
            state, _to_blender_accessible_path(state, os.path.join(repo_path, "XPart", "demo.py"))
        )
        xpart_weights_note = xpart_exists and _blender_path_is_file(
            state, _to_blender_accessible_path(state, os.path.join(repo_path, "XPart", "README.md"))
        )

        if not repo_exists:
            summary = "Repo not found."
        elif p3_sam_demo and xpart_demo:
            summary = "P3-SAM ready to prototype. X-Part is research-only for now."
        elif p3_sam_demo:
            summary = "P3-SAM ready to prototype."
        elif xpart_demo:
            summary = "X-Part code found, but public release still looks incomplete."
        else:
            summary = "Repo found, but no usable public demo entrypoints were detected."

        return {
            "repo_path": repo_path,
            "python_path": python_path,
            "repo_exists": repo_exists,
            "python_exists": python_exists,
            "p3_sam_exists": p3_sam_exists,
            "xpart_exists": xpart_exists,
            "p3_sam_demo": p3_sam_demo,
            "xpart_demo": xpart_demo,
            "xpart_weights_note": xpart_weights_note,
            "summary": summary,
        }

    return _transient_cached("parts_repo_status", cache_key, PARTS_REPO_STATUS_CACHE_TTL_SECONDS, _load)


def _parts_backend_label(backend_choice):
    choice = (backend_choice or "P3SAM").upper()
    if choice == "XPART":
        return "X-Part"
    return "P3-SAM"


def _parts_compose_status_text(stage="", detail="", progress=""):
    stage = (stage or "").strip()
    detail = (detail or "").strip()
    progress = (progress or "").strip()
    if stage and progress:
        return f"{stage} | {progress}"
    if stage and detail:
        return f"{stage}: {detail}"
    return stage or detail or progress


def _parts_tqdm_progress(line):
    cleaned = (line or "").replace("\r", "").strip()
    match = PARTS_TQDM_PROGRESS_LINE.search(cleaned)
    if not match:
        return None
    label = match.group("label").strip().rstrip(":").strip()
    percent = match.group("percent").strip()
    current = match.group("current").strip()
    total = match.group("total").strip()
    return {
        "label": label,
        "progress": f"{current}/{total} ({percent}%)",
    }


def _parts_progress_update(line, backend_choice, current_stage="", current_detail="", current_progress=""):
    cleaned = (line or "").replace("\r", "").strip()
    if not cleaned:
        return None
    if cleaned.startswith("__NYMPHS_PARTS_PID__ "):
        return None
    stage = (current_stage or "").strip()
    detail = (current_detail or "").strip()
    progress = (current_progress or "").strip()
    backend_label = _parts_backend_label(backend_choice)
    lowered = cleaned.lower()

    def _result(stage_value=None, detail_value=None, progress_value=None):
        next_stage = (stage_value if stage_value is not None else stage).strip()
        next_detail = (detail_value if detail_value is not None else detail).strip()
        next_progress = (progress_value if progress_value is not None else progress).strip()
        status_text = _parts_compose_status_text(next_stage, next_detail, next_progress)[:160]
        changes = {
            "parts_status_text": status_text,
            "status_text": status_text,
            "parts_status_stage": next_stage,
            "parts_status_detail": next_detail,
            "parts_status_progress": next_progress,
        }
        if stage_value == "" and not next_stage:
            changes["parts_status_stage"] = ""
        if detail_value == "" and not next_detail:
            changes["parts_status_detail"] = ""
        if progress_value == "" and not next_progress:
            changes["parts_status_progress"] = ""
        return changes

    if cleaned.startswith("[X-Part] "):
        text = cleaned.replace("[X-Part] ", "", 1).strip()
        elapsed_match = PARTS_ELAPSED_LINE.search(text)
        if elapsed_match:
            return _result(progress_value=f"Elapsed {elapsed_match.group('seconds')}s")
        if text.startswith("Resolving Stage-1 analysis inputs"):
            return _result(stage_value="Resolve Inputs", detail_value=text, progress_value="")
        if text.startswith("Loading X-Part model"):
            return _result(stage_value="Load Model", detail_value=text, progress_value="")
        if text.startswith("Moving X-Part model to target device"):
            return _result(stage_value="Model Transfer", detail_value=text, progress_value="")
        if text.startswith("Model loaded. Starting X-Part pipeline"):
            return _result(stage_value="Prepare Generation", detail_value=text, progress_value="")
        if text.startswith("X-Part settings:"):
            return _result(detail_value=text)
        if text.startswith("CUDA ready on"):
            return _result(detail_value=text)
        if text.startswith("Preparing latent allocation and conditioner inputs"):
            return _result(stage_value="Prepare Generation", detail_value=text, progress_value="")
        if text.startswith("Encoding part and object conditions"):
            return _result(stage_value="Conditioning", detail_value=text, progress_value="")
        if text.startswith("Conditioning complete. Starting diffusion sampling"):
            return _result(stage_value="Diffusion Sampling", detail_value=text, progress_value="")
        if text.startswith("Diffusion complete. Starting mesh extraction"):
            return _result(stage_value="Export Geometry", detail_value=text, progress_value="")
        if text.startswith("Pipeline returned. Exporting part meshes"):
            return _result(stage_value="Finalize Export", detail_value=text, progress_value="")
        if text.startswith("Export complete"):
            return _result(stage_value="Completed", detail_value=text, progress_value="")
        return _result(detail_value=text)
    if cleaned.startswith("Diffusion Sampling::"):
        progress_info = _parts_tqdm_progress(cleaned)
        if progress_info:
            return _result(
                stage_value="Diffusion Sampling",
                detail_value="Sampling part latents on CUDA",
                progress_value=progress_info["progress"],
            )
        return _result(stage_value="Diffusion Sampling", detail_value=cleaned[:140])
    if cleaned.startswith("MC Level ") and "Implicit Function" in cleaned:
        progress_info = _parts_tqdm_progress(cleaned)
        if progress_info:
            return _result(
                stage_value="Export Geometry",
                detail_value=progress_info["label"][:140],
                progress_value=progress_info["progress"],
            )
        return _result(stage_value="Export Geometry", detail_value=cleaned[:140])
    if cleaned.startswith("[X-PartDiag] "):
        if cleaned.startswith("[X-PartDiag] startup "):
            return _result(stage_value="Startup Checks", detail_value="Verifying Python, torch, and CUDA runtime", progress_value="")
        if cleaned.startswith("[X-PartDiag] pre-model-transfer "):
            return _result(stage_value="Pre-Transfer Checks", detail_value="Verifying CUDA before model transfer", progress_value="")
        if cleaned.startswith("[X-PartDiag] pre-generation "):
            return _result(stage_value="Pre-Generation Checks", detail_value="Verifying CUDA before generation", progress_value="")
        return _result(detail_value=cleaned[:140])
    if cleaned.startswith("[X-PartExportDiag] "):
        phase_match = re.search(r"phase=([a-z0-9\-]+)", cleaned)
        phase = phase_match.group(1) if phase_match else ""
        phase_map = {
            "export-stage-start": ("Prepare Export", "Preparing export stage"),
            "export-stage-after-offload": ("Prepare Export", "Offloaded model and conditioner to CPU"),
            "export-stage-after-vae-to-device": ("Prepare Export", "Moved VAE to CUDA"),
            "export-before-vae-decode": ("Prepare Export", "Decoding VAE latents"),
            "export-after-vae-decode": ("Export Geometry", "Decoded VAE latents"),
            "export-after-latent2mesh": ("Export Geometry", "Converted latent field to mesh"),
            "export-exception": ("Export Failed", "Export failed during latent-to-mesh"),
        }
        stage_value, detail_value = phase_map.get(phase, (current_stage or "Export", cleaned[:140]))
        return _result(stage_value=stage_value, detail_value=detail_value)
    if cleaned.startswith(">>>>>>"):
        cleaned = cleaned.replace(">>>>>>", "", 1).strip()
        cleaned = cleaned.replace("代码", "", 1).strip()
        stage_value = "Analyze Mesh" if backend_label == "P3-SAM" else current_stage
        return _result(stage_value=stage_value, detail_value=cleaned[:140])
    if cleaned.startswith("{") and '"part_count"' in cleaned:
        return _result(stage_value="Completed", detail_value="Parts export finished.", progress_value="")
    if cleaned.startswith("{") and '"parts_output_path"' in cleaned:
        return _result(stage_value="Completed", detail_value="Part generation finished.", progress_value="")
    if "CUDA preflight failed" in cleaned:
        return _result(stage_value="Failed", detail_value="CUDA preflight failed. Restart WSL or reboot Windows before retrying X-Part.", progress_value="")
    if "CUDA device became unavailable during" in cleaned:
        return _result(stage_value="Failed", detail_value="CUDA device became unavailable during X-Part model transfer.", progress_value="")
    if "CUDA runtime failed during" in cleaned:
        return _result(stage_value="Failed", detail_value="CUDA runtime failed during X-Part.", progress_value="")
    if "CUDA driver error: device not ready" in cleaned:
        return _result(stage_value="Failed", detail_value="CUDA device not ready. Restart WSL or reboot Windows before retrying X-Part.", progress_value="")
    if "Found no NVIDIA driver on your system" in cleaned:
        return _result(stage_value="Failed", detail_value="No NVIDIA driver is visible inside WSL. Restart WSL or reboot Windows.", progress_value="")
    if "Loading checkpoint from HuggingFace" in cleaned:
        return _result(stage_value="Load Model", detail_value="Loading Sonata checkpoint...", progress_value="")
    if "trying to download model from huggingface" in cleaned:
        return _result(stage_value="Analyze Mesh", detail_value="Loading P3-SAM weights...", progress_value="")
    if cleaned.startswith("flash attention:"):
        return _result(detail_value=cleaned[:120])
    if "点数：" in cleaned or "面片数：" in cleaned:
        stage_value = "Analyze Mesh" if backend_label == "P3-SAM" else current_stage
        return _result(stage_value=stage_value, detail_value=cleaned[:140])
    if "生成face_ids完成" in cleaned:
        return _result(stage_value="Analyze Mesh", detail_value="Projected part labels onto mesh.", progress_value="")
    if "最终mask数量" in cleaned:
        return _result(stage_value="Analyze Mesh", detail_value=cleaned[:140], progress_value="")
    return None


def _prepare_parts_source(context, state):
    source_mode = (getattr(state, "parts_source_mode", "SELECTED") or "SELECTED").upper()
    export_format = (getattr(state, "parts_export_format", "glb") or "glb").strip().lower()

    if source_mode == "LATEST":
        latest_path = (getattr(state, "shape_output_path", "") or "").strip()
        if not latest_path:
            raise RuntimeError("No latest generated mesh is available yet.")
        resolved = bpy.path.abspath(latest_path)
        if not os.path.exists(resolved):
            raise RuntimeError(f"Latest generated mesh is missing: {latest_path}")
        source_name = _path_leaf(resolved) or "latest-result"
        destination_path = _parts_output_path(source_name, export_format="glb")
        shutil.copy2(resolved, destination_path)
        latest_object_name = (getattr(state, "latest_result_object_name", "") or "").strip()
        source_object_names = _decode_object_name_list(latest_object_name)
        source_object_names = [name for name in source_object_names if bpy.data.objects.get(name) is not None]
        if not source_object_names:
            source_object_names = _selected_mesh_root_names(context)
        return destination_path, f"Latest Result: {_path_leaf(destination_path)}", _encode_object_name_list(source_object_names)

    destination_path = _parts_output_path("selected-mesh", export_format=export_format)
    export_path, source_name = _export_selected_mesh_file(
        context,
        export_format=export_format,
        destination_path=destination_path,
    )
    return export_path, f"Selected Mesh: {source_name}", source_name


def _parts_input_shell_path(raw_path):
    path = os.path.abspath((raw_path or "").strip())
    if os.name == "nt":
        drive, tail = ntpath.splitdrive(path)
        if drive and len(drive) == 2 and drive[1] == ":":
            drive_letter = drive[0].lower()
            wsl_tail = tail.replace("\\", "/")
            return shlex.quote(f"/mnt/{drive_letter}{wsl_tail}")
        return f'$(wslpath -a {shlex.quote(path)})'
    return shlex.quote(path)


def _parts_snapshot_get(state, attr_name, default=None):
    if isinstance(state, dict):
        return state.get(attr_name, default)
    return getattr(state, attr_name, default)


def _parts_run_shell(state, prepared_path, output_dir, backend_choice):
    raw_repo_path = (_parts_snapshot_get(state, "parts_repo_path", DEFAULT_REPO_PARTS_PATH) or DEFAULT_REPO_PARTS_PATH).strip()
    raw_python_path = (_parts_snapshot_get(state, "parts_python_path", DEFAULT_PARTS_PYTHON_PATH) or DEFAULT_PARTS_PYTHON_PATH).strip()
    repo_path = _resolved_user_path(raw_repo_path or DEFAULT_REPO_PARTS_PATH, state)
    python_path = _resolved_user_path(raw_python_path or DEFAULT_PARTS_PYTHON_PATH, state)
    backend_label = _parts_backend_label(backend_choice)
    output_dir_q = shlex.quote(output_dir)
    exports = (
        "export CUDA_HOME=/usr/local/cuda-12.4; "
        'export PATH="$CUDA_HOME/bin:$PATH"; '
        'export LD_LIBRARY_PATH="$CUDA_HOME/lib64:$LD_LIBRARY_PATH"; '
        'export TORCH_CUDA_ARCH_LIST="8.9"; '
        'export PYTORCH_CUDA_ALLOC_CONF="expandable_segments:True"; '
        f'export NYMPHS3D_PARTS_CACHE_ROOT={shlex.quote(_resolved_user_path("~/.cache/hunyuan3d-part", state))}; '
    )
    if backend_choice == "XPART":
        manifest_path = (_parts_snapshot_get(state, "parts_stage1_manifest_path", "") or "").strip()
        if not manifest_path:
            return None, "Run Analyze Mesh first so X-Part has a Stage-1 manifest to consume."
        manifest_expr = _parts_input_shell_path(manifest_path)
        cpu_threads = max(1, int(_parts_snapshot_get(state, "parts_xpart_cpu_threads", 4)))
        parts = [
            python_path,
            "-u",
            "scripts/run_xpart_generate.py",
            "--stage1_manifest",
            manifest_expr,
            "--output_dir",
            output_dir_q,
            "--num_inference_steps",
            shlex.quote(str(max(1, int(_parts_snapshot_get(state, "parts_xpart_steps", 50))))),
            "--octree_resolution",
            shlex.quote(str(max(256, int(_parts_snapshot_get(state, "parts_xpart_octree_resolution", 512))))),
            "--dtype",
            shlex.quote(str((_parts_snapshot_get(state, "parts_xpart_dtype", "float32") or "float32"))),
            "--max_aabb",
            shlex.quote(str(max(0, int(_parts_snapshot_get(state, "parts_xpart_max_aabb", 0))))),
            "--cpu_threads",
            shlex.quote(str(cpu_threads)),
            "--progress_interval",
            "10",
        ]
        cmd = " ".join(parts)
        shell = (
            f"{exports}"
            f"export OMP_NUM_THREADS={cpu_threads}; "
            f"export OPENBLAS_NUM_THREADS={cpu_threads}; "
            f"export MKL_NUM_THREADS={cpu_threads}; "
            f"export NUMEXPR_NUM_THREADS={cpu_threads}; "
            f"export VECLIB_MAXIMUM_THREADS={cpu_threads}; "
            f"export BLIS_NUM_THREADS={cpu_threads}; "
            f"mkdir -p {output_dir_q}; "
            f"cd {_shell_repo_path(repo_path)}; "
            "printf '__NYMPHS_PARTS_PID__ %s\\n' $$; "
            f"exec nice -n 10 {cmd}"
        )
        return shell, ""

    mesh_expr = _parts_input_shell_path(prepared_path)
    parts = [
        python_path,
        "-u",
        "scripts/run_p3sam_segment.py",
        "--mesh_path",
        mesh_expr,
        "--output_dir",
        output_dir_q,
        "--point_num",
        shlex.quote(str(max(1000, int(_parts_snapshot_get(state, "parts_point_num", 30000))))),
        "--prompt_num",
        shlex.quote(str(max(8, int(_parts_snapshot_get(state, "parts_prompt_num", 96))))),
        "--prompt_bs",
        shlex.quote(str(max(1, int(_parts_snapshot_get(state, "parts_prompt_bs", 4))))),
        "--parallel",
        "0",
    ]
    cmd = " ".join(parts)
    shell = (
        f"{exports}"
        f"mkdir -p {output_dir_q}; "
        f"cd {_shell_repo_path(repo_path)}; "
        "printf '__NYMPHS_PARTS_PID__ %s\\n' $$; "
        f"exec {cmd}"
    )
    return shell, ""


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
    if not state.imagegen_is_busy and not has_live_task:
        return "", "", ""
    stage = (state.imagegen_task_stage or "").strip()
    detail = (state.imagegen_task_detail or state.imagegen_status_text or "").strip()
    progress = (state.imagegen_task_progress or "").strip()
    runtime_hint = _service_runtime_hint(state, "n2d2")
    generic_detail = {
        "",
        "Generating image...",
        "Waiting for image backend progress...",
        "Running image model inference...",
    }
    if runtime_hint and detail in generic_detail:
        detail = runtime_hint
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
        return "Running Inference", detail or "Running image model inference...", progress
    return stage or "Generating Image", detail, progress


def _parts_progress_lines(state):
    if not _parts_run_is_active(state):
        return "", "", ""
    stage = (getattr(state, "parts_status_stage", "") or "").strip()
    detail = (getattr(state, "parts_status_detail", "") or "").strip()
    progress = (getattr(state, "parts_status_progress", "") or "").strip()
    if not stage and not detail and not progress:
        detail = (state.parts_status_text or "").strip()
    if not stage and detail:
        lowered = detail.lower()
        if "x-part" in lowered:
            stage = "Running X-Part"
        elif "p3-sam" in lowered:
            stage = "Running P3-SAM"
        else:
            stage = "Running Parts"
    if not detail and not progress:
        return "", "", ""
    return stage, detail, progress


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
    if _parts_run_is_active(state):
        parts_lines = _parts_progress_lines(state)
        if any(parts_lines):
            return parts_lines
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
    active_services = _active_service_entries(state)
    if state.launch_state == "Launching":
        return f"{backend_label} Launching"
    if state.launch_state == "Stopping":
        return "Stopping"
    if active_status == "processing":
        return f"{backend_label} Busy"
    if active_status == "queued" or state.waiting_for_backend_progress:
        return f"{backend_label} Submitting"
    if _parts_run_is_active(state):
        return "Nymphs Parts Busy"
    if image_status == "processing":
        return f"{SERVICE_LABELS['n2d2']} Busy"
    if image_status == "queued" or state.imagegen_is_busy:
        return f"{SERVICE_LABELS['n2d2']} Submitting"
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

    for scene_name in list(PARTS_ACTIVE_PROCS.keys()):
        try:
            _terminate_parts_process(scene_name)
        except Exception:
            pass


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


def _parts_internal_pid_from_line(line):
    cleaned = (line or "").replace("\r", "").strip()
    prefix = "__NYMPHS_PARTS_PID__ "
    if not cleaned.startswith(prefix):
        return 0
    try:
        return max(0, int(cleaned[len(prefix):].strip()))
    except Exception:
        return 0


def _format_kib_for_status(raw_value):
    try:
        kib = float(raw_value or 0.0)
    except Exception:
        return ""
    if kib <= 0:
        return ""
    if kib >= 1024:
        return f"{kib / 1024.0:.0f} MiB"
    return f"{int(kib)} KiB"


def _parts_process_stats(scene_name, state=None):
    proc = _get_parts_process(scene_name)
    if proc is None:
        return {}
    try:
        if proc.poll() is not None:
            return {}
    except Exception:
        return {}

    if os.name == "nt":
        wsl_pid = max(0, int(getattr(state, "parts_wsl_pid", 0) or 0))
        if wsl_pid <= 0:
            return {}
        command = _compose_wsl_invocation(
            f"ps -p {wsl_pid} -o %cpu=,%mem=,rss=,etime=",
            state,
        )
    else:
        pid = max(0, int(getattr(state, "parts_wsl_pid", 0) or 0)) or int(proc.pid)
        command = ["ps", "-p", str(pid), "-o", "%cpu=,%mem=,rss=,etime="]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
    except Exception:
        return {}
    if result.returncode != 0:
        return {}

    line = next((raw.strip() for raw in result.stdout.splitlines() if raw.strip()), "")
    if not line:
        return {}
    parts = line.split(None, 3)
    if len(parts) != 4:
        return {}
    cpu_raw, mem_raw, rss_raw, elapsed = parts

    cpu_text = ""
    mem_text = ""
    try:
        cpu_text = f"{float(cpu_raw):.1f}%"
    except Exception:
        pass
    try:
        mem_text = f"{float(mem_raw):.1f}%"
    except Exception:
        pass

    return {
        "cpu": cpu_text,
        "mem": mem_text,
        "rss": _format_kib_for_status(rss_raw),
        "elapsed": elapsed.strip(),
    }


def _parts_local_usage_text(scene_name, state=None):
    if state is None:
        return ""
    if not _parts_run_is_active(state, scene_name):
        return ""
    stats = _parts_process_stats(scene_name, state)
    pieces = []
    if stats.get("cpu"):
        pieces.append(f"CPU {stats['cpu']}")
    if stats.get("rss"):
        pieces.append(f"RAM {stats['rss']}")
    elif stats.get("mem"):
        pieces.append(f"MEM {stats['mem']}")
    return " | ".join(pieces)


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
    parts_usage_text = _parts_local_usage_text(scene_name, state)
    if parts_usage_text != (state.parts_host_usage_text or ""):
        _emit_status(scene_name, parts_host_usage_text=parts_usage_text)
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
        state = getattr(scene, "nymphs3d2v2_state", None)
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
        state = getattr(scene, "nymphs3d2v2_state", None)
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
        state = getattr(scene, "nymphs3d2v2_state", None)
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
    if event.parts_import and state is not None and getattr(state, "parts_new_collection", False):
        collection_name = (event.collection_name or getattr(state, "parts_collection_name", "") or "Nymphs Parts").strip() or "Nymphs Parts"
        target_collection = bpy.data.collections.get(collection_name)
        if target_collection is None:
            target_collection = bpy.data.collections.new(collection_name)
            bpy.context.scene.collection.children.link(target_collection)
        for obj in imported_objects:
            if obj is None:
                continue
            if obj not in target_collection.objects[:]:
                target_collection.objects.link(obj)
            for coll in list(obj.users_collection):
                if coll != target_collection:
                    try:
                        coll.objects.unlink(obj)
                    except Exception:
                        pass

    if event.hide_source and event.source_object_name:
        source_object_names = _decode_object_name_list(event.source_object_name)
        imported_object = bpy.data.objects.get(imported_names[0]) if imported_names else None
        source_object = bpy.data.objects.get(source_object_names[0]) if source_object_names else None
        if source_object is not None and imported_object is not None:
            imported_object.location = source_object.location
            imported_object.rotation_euler = source_object.rotation_euler
            imported_object.scale = source_object.scale
        for object_name in source_object_names:
            source_candidate = bpy.data.objects.get(object_name)
            if source_candidate is not None:
                _hide_object_tree(source_candidate)

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
            state.latest_result_object_name = _encode_object_name_list([obj.name for obj in latest_roots if obj is not None])
        else:
            state.last_result = "Imported parts result"
            state.parts_started_at = 0.0
            state.parts_wsl_pid = 0
            state.parts_host_usage_text = ""
            state.parts_status_stage = ""
            state.parts_status_detail = ""
            state.parts_status_progress = ""
            state.parts_last_imported_object_name = _encode_object_name_list(
                [obj.name for obj in latest_roots if obj is not None]
            )


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


def _parts_job_worker(scene_name, state_snapshot, prepared_path, source_object_name, hide_source):
    state = _active_state(scene_name)
    _clear_parts_stop_requested(scene_name)
    backend_choice = (state_snapshot.get("parts_backend_choice") or "P3SAM").upper()
    backend_label = _parts_backend_label(backend_choice)
    import_source_object_name = source_object_name
    if backend_choice == "XPART":
        prior_parts_import = (state_snapshot.get("parts_last_imported_object_name") or "").strip()
        if prior_parts_import:
            import_source_object_name = prior_parts_import
    output_dir = _parts_result_dir_wsl(
        state_snapshot,
        source_name=source_object_name or _path_leaf(prepared_path),
        backend_label=backend_label,
    )
    blender_output_dir = _to_blender_accessible_path(state, output_dir)
    _emit_status(
        scene_name,
        parts_output_dir=blender_output_dir,
        parts_status_text=f"Starting {backend_label}. Output: {blender_output_dir or output_dir}",
        parts_status_stage=f"Starting {backend_label}",
        parts_status_detail=f"Output: {blender_output_dir or output_dir}",
        parts_status_progress="",
    )
    shell, message = _parts_run_shell(state_snapshot, prepared_path, output_dir, backend_choice)
    if not shell:
        _emit_status(
            scene_name,
            is_busy=False,
            status_text=message,
            parts_status_text=message,
            parts_status_stage="Failed",
            parts_status_detail=message,
            parts_status_progress="",
            parts_started_at=0.0,
            parts_wsl_pid=0,
            parts_host_usage_text="",
        )
        return

    last_hint = ""
    recent_lines = []
    host_log_dir = blender_output_dir or output_dir
    log_path = os.path.join(host_log_dir, "nymphs_parts_run.log")
    current_parts_stage = f"Starting {backend_label}"
    current_parts_detail = f"Output: {blender_output_dir or output_dir}"
    current_parts_progress = ""
    log_handle = None
    try:
        os.makedirs(host_log_dir, exist_ok=True)
        log_handle = open(log_path, "w", encoding="utf-8", errors="replace")
        log_handle.write(f"backend={backend_label}\n")
        log_handle.write(f"input={prepared_path}\n")
        log_handle.write(f"output_dir={output_dir}\n\n")
        log_handle.flush()
        popen_kwargs = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
            "bufsize": 1,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )
        else:
            popen_kwargs["start_new_session"] = True
        proc = subprocess.Popen(_compose_wsl_invocation(shell, state_snapshot), **popen_kwargs)
        _set_parts_process(scene_name, proc)
        _emit_status(
            scene_name,
            parts_started_at=time.time(),
            parts_wsl_pid=0,
            parts_host_usage_text="",
            parts_log_path=log_path,
        )
        if proc.stdout is not None:
            for raw_line in proc.stdout:
                if log_handle is not None:
                    log_handle.write(raw_line)
                    log_handle.flush()
                cleaned = (raw_line or "").replace("\r", "").strip()
                internal_pid = _parts_internal_pid_from_line(cleaned)
                if internal_pid > 0:
                    _emit_status(scene_name, parts_wsl_pid=internal_pid)
                if cleaned and internal_pid <= 0:
                    recent_lines.append(cleaned)
                    recent_lines = recent_lines[-8:]
                hint_changes = _parts_progress_update(
                    raw_line,
                    backend_choice,
                    current_stage=current_parts_stage,
                    current_detail=current_parts_detail,
                    current_progress=current_parts_progress,
                )
                if hint_changes:
                    current_parts_stage = hint_changes.get("parts_status_stage", current_parts_stage)
                    current_parts_detail = hint_changes.get("parts_status_detail", current_parts_detail)
                    current_parts_progress = hint_changes.get("parts_status_progress", current_parts_progress)
                    last_hint = hint_changes.get("parts_status_text", last_hint)
                    _emit_status(scene_name, **hint_changes)
        exit_code = proc.wait()
        stopped_by_user = _consume_parts_stop_requested(scene_name)
        if exit_code != 0:
            if stopped_by_user:
                _emit_status(
                    scene_name,
                    is_busy=False,
                    status_text="Parts run stopped.",
                    parts_status_text="Parts run stopped.",
                    parts_status_stage="Stopped",
                    parts_status_detail="Parts run stopped.",
                    parts_status_progress="",
                    parts_output_dir=blender_output_dir,
                    parts_started_at=0.0,
                    parts_wsl_pid=0,
                    parts_host_usage_text="",
                )
                return
            detail = last_hint or (recent_lines[-1] if recent_lines else f"{backend_label} exited with code {exit_code}.")
            _emit_status(
                scene_name,
                is_busy=False,
                status_text=detail,
                parts_status_text=f"{detail} Log: {log_path}",
                parts_status_stage="Failed",
                parts_status_detail=detail,
                parts_status_progress="",
                parts_log_path=log_path,
                parts_output_dir=blender_output_dir,
                parts_started_at=0.0,
                parts_wsl_pid=0,
                parts_host_usage_text="",
            )
            return

        if backend_choice == "XPART":
            output_path = f"{output_dir}/xpart_parts.glb"
            done_text = "Part generation finished. Importing result..."
        else:
            output_path = f"{output_dir}/p3sam_segmented.glb"
            done_text = "Parts analysis finished. Importing result..."
        state = _active_state(scene_name)
        blender_output_path = _to_blender_accessible_path(state, output_path)
        if not blender_output_path:
            raise RuntimeError("Parts run finished, but no output path was produced.")
        status_changes = {
            "parts_output_path": blender_output_path,
            "parts_output_dir": _to_blender_accessible_path(state, output_dir),
            "status_text": done_text,
            "parts_status_text": done_text,
            "parts_status_stage": "Importing Result",
            "parts_status_detail": done_text,
            "parts_status_progress": "",
            "parts_log_path": log_path,
        }
        if backend_choice == "P3SAM":
            manifest_output_path = f"{output_dir}/stage1_manifest.json"
            summary_output_path = f"{output_dir}/summary.json"
            status_changes["parts_stage1_manifest_path"] = _to_blender_accessible_path(state, manifest_output_path)
            status_changes["parts_stage1_summary_path"] = _to_blender_accessible_path(state, summary_output_path)
        _emit_status(
            scene_name,
            **status_changes,
        )
        _emit_import(
            scene_name,
            blender_output_path,
            import_source_object_name,
            hide_source=hide_source,
            update_shape_result=False,
            parts_import=(backend_choice == "XPART"),
            collection_name=(state_snapshot.get("parts_collection_name") or "").strip(),
        )
    except Exception as exc:
        _emit_status(
            scene_name,
            is_busy=False,
            status_text=str(exc),
            parts_status_text=str(exc),
            parts_status_stage="Failed",
            parts_status_detail=str(exc),
            parts_status_progress="",
            parts_started_at=0.0,
            parts_wsl_pid=0,
            parts_host_usage_text="",
        )
    finally:
        _clear_parts_process(scene_name)
        _clear_parts_stop_requested(scene_name)
        if log_handle is not None:
            try:
                log_handle.close()
            except Exception:
                pass
        try:
            prune_state = _active_state(scene_name) or state_snapshot
            current_output = _to_blender_accessible_path(prune_state, output_dir)
            _prune_parts_result_dirs(
                prune_state,
                keep_recent=state_snapshot.get("parts_keep_recent_runs", 8),
                extra_preserve={current_output},
            )
        except Exception:
            pass


def _imagegen_worker(scene_name, api_root, payload):
    try:
        payloads = list(payload) if isinstance(payload, (list, tuple)) else [payload]
        total = len(payloads)
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
            last_output_path = blender_output_path
            last_metadata_path = blender_metadata_path
            output_dir = os.path.dirname(blender_output_path) if blender_output_path else output_dir

        final_status = "Image generated and assigned to Image."
        if total > 1:
            final_status = f"Generated {total} image variants. Last variant assigned to Image."

        _emit_status(
            scene_name,
            imagegen_is_busy=False,
            imagegen_started_at=0.0,
            imagegen_status_text=final_status,
            imagegen_task_status="idle",
            imagegen_task_stage="",
            imagegen_task_detail="",
            imagegen_task_progress="",
            imagegen_output_path=last_output_path,
            imagegen_output_dir=output_dir,
            imagegen_metadata_path=last_metadata_path,
            image_path=last_output_path,
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
    show_advanced: BoolProperty(default=False)
    show_shape: BoolProperty(default=False)
    show_parts: BoolProperty(default=False)
    show_parts_stage1: BoolProperty(default=False)
    show_parts_stage2: BoolProperty(default=False)
    show_parts_support: BoolProperty(default=False)
    show_parts_status: BoolProperty(default=False)
    show_texture: BoolProperty(default=False)
    show_parts_backend: BoolProperty(default=False)
    parts_mode: EnumProperty(
        name="Parts Mode",
        description="Experimental future direction for part-aware mesh workflows.",
        items=(
            ("SEGMENT", "Segment Parts", "Find semantic regions on the current mesh"),
            ("DECOMPOSE", "Decompose Parts", "Try generating separated usable parts from the current mesh"),
        ),
        default="SEGMENT",
    )
    parts_point_num: IntProperty(
        name="Points",
        description="Sampled point count for P3-SAM. Lower values reduce VRAM usage but can reduce detail.",
        default=30000,
        min=1000,
        max=200000,
    )
    parts_prompt_num: IntProperty(
        name="Prompts",
        description="Prompt count for P3-SAM. Lower values reduce VRAM usage and runtime.",
        default=96,
        min=8,
        max=512,
    )
    parts_prompt_bs: IntProperty(
        name="Prompt Batch",
        description="Prompt batch size for P3-SAM inference. Lower values reduce VRAM usage.",
        default=4,
        min=1,
        max=64,
    )
    parts_xpart_steps: IntProperty(
        name="Steps",
        description="X-Part diffusion step count. This local default is the current practical 4080 lane, not the heavier upstream default.",
        default=20,
        min=1,
        max=100,
    )
    parts_xpart_octree_resolution: IntProperty(
        name="Octree",
        description="X-Part export octree resolution. This local default is tuned for the current 4080 lane. Must be at least 256.",
        default=300,
        min=256,
        max=1024,
    )
    parts_xpart_max_aabb: IntProperty(
        name="Max Boxes",
        description="Cap how many Stage-1 bounding boxes X-Part should use. This local default is tuned for the current 4080 lane.",
        default=8,
        min=0,
        max=64,
    )
    parts_xpart_dtype: EnumProperty(
        name="Precision",
        description="X-Part execution dtype. The upstream demo uses float32, and it remains the proven stable path here.",
        items=(
            ("float32", "Float32", "Upstream demo path and current stable addon path"),
            ("bfloat16", "BFloat16", "Experimental; current public path is unstable here"),
            ("float16", "Float16", "Experimental reduced-precision path"),
        ),
        default="float32",
    )
    parts_xpart_cpu_threads: IntProperty(
        name="CPU Threads",
        description="Cap CPU worker threads used by X-Part so it does not overwhelm the desktop. Lower is gentler but slower.",
        default=8,
        min=1,
        max=64,
    )
    parts_backend_choice: EnumProperty(
        name="Backend",
        description="Which experimental parts backend to target. P3-SAM is the more practical first path. X-Part remains more exploratory.",
        items=(
            ("P3SAM", "P3-SAM", "3D part segmentation path"),
            ("XPART", "X-Part", "Part generation/decomposition path"),
        ),
        default="P3SAM",
    )
    parts_repo_path: StringProperty(
        name="Parts Repo Path",
        description="Local Hunyuan3D-Part repo path used for experimental parts research and future integration.",
        default=DEFAULT_REPO_PARTS_PATH,
    )
    parts_python_path: StringProperty(
        name="Parts Python",
        description="Python executable to use for future Hunyuan3D-Part integration. P3-SAM is the first realistic target.",
        default=DEFAULT_PARTS_PYTHON_PATH,
    )
    parts_source_mode: EnumProperty(
        name="Source",
        description="Which mesh the experimental parts workflow should use.",
        items=(
            ("SELECTED", "Selected Mesh", "Use the currently selected Blender mesh"),
            ("LATEST", "Latest Result", "Prefer the latest generated mesh when available"),
        ),
        default="SELECTED",
    )
    parts_export_format: EnumProperty(
        name="Format",
        description="Temporary mesh format to prepare for a future parts backend.",
        items=(
            ("glb", "GLB", "Preferred default for current mesh workflow experiments"),
            ("obj", "OBJ", "Useful for compatibility testing with older mesh tools"),
        ),
        default="glb",
    )
    parts_keep_original: BoolProperty(
        name="Keep Original Mesh",
        description="Keep the source mesh in the scene when future parts output is created.",
        default=False,
    )
    parts_keep_recent_runs: IntProperty(
        name="Keep Recent Runs",
        description="Auto-prune older Parts run folders after each run. Set to 0 to disable automatic pruning.",
        default=8,
        min=0,
        max=100,
    )
    parts_new_collection: BoolProperty(
        name="Send To New Collection",
        description="Place future parts output into a separate Blender collection.",
        default=True,
    )
    parts_collection_name: StringProperty(
        name="Collection Name",
        description="Collection name to use for future parts output when Send To New Collection is enabled.",
        default="Nymphs Parts",
    )
    parts_status_text: StringProperty(
        name="Parts Status",
        default="Idle",
    )
    parts_prepared_source_path: StringProperty(
        name="Prepared Parts Source",
        default="",
    )
    parts_prepared_source_object_name: StringProperty(
        name="Prepared Parts Source Object",
        default="",
    )
    latest_result_object_name: StringProperty(
        name="Latest Result Object",
        default="",
    )
    parts_output_path: StringProperty(
        name="Parts Output",
        default="",
    )
    parts_output_dir: StringProperty(
        name="Parts Output Dir",
        default="",
    )
    parts_last_imported_object_name: StringProperty(
        name="Last Parts Import",
        default="",
    )
    parts_stage1_manifest_path: StringProperty(
        name="Stage-1 Manifest",
        default="",
    )
    parts_stage1_summary_path: StringProperty(
        name="Stage-1 Summary",
        default="",
    )
    parts_status_stage: StringProperty(default="")
    parts_status_detail: StringProperty(default="")
    parts_status_progress: StringProperty(default="")
    parts_log_path: StringProperty(default="")
    parts_started_at: FloatProperty(default=0.0)
    parts_wsl_pid: IntProperty(default=0)
    parts_host_usage_text: StringProperty(default="")
    imagegen_prompt_text_name: StringProperty(default="")
    imagegen_prompt: StringProperty(
        name="Prompt",
        description="What Z-Image should create. Keep it direct and editable; use Edit for longer text.",
        default="",
    )
    imagegen_prompt_preset: EnumProperty(
        name="Presets",
        description="Load a reusable prompt. Image settings stay unchanged.",
        items=_imagegen_prompt_preset_items,
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
        state = context.scene.nymphs3d2v2_state
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
        state = context.scene.nymphs3d2v2_state
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
        state = context.scene.nymphs3d2v2_state
        stopped_any = False
        for service_key in SERVICE_ORDER:
            ok, _message = _stop_service_process(state, service_key)
            stopped_any = stopped_any or ok
        stopped_parts = False
        for scene_name in list(PARTS_ACTIVE_PROCS.keys()):
            try:
                if _terminate_parts_process(scene_name):
                    stopped_parts = True
                    parts_state = _active_state(scene_name)
                    if parts_state is not None:
                        parts_state.parts_status_text = "Stopping parts run..."
                        parts_state.parts_status_stage = "Stopping"
                        parts_state.parts_status_detail = parts_state.parts_status_text
                        parts_state.parts_status_progress = ""
                        parts_state.status_text = parts_state.parts_status_text
            except Exception:
                pass
        stopped_any = stopped_any or stopped_parts
        if not stopped_any:
            _best_effort_stop_local(state=state)
            state.launch_state = "Stopped"
            state.launch_detail = "No managed backends were running."
            state.status_text = "No managed backends were running."
            return {"CANCELLED"}
        state.status_text = "Stop requested."
        if stopped_parts:
            state.parts_status_text = "Stopping parts run..."
            state.parts_status_stage = "Stopping"
            state.parts_status_detail = state.parts_status_text
            state.parts_status_progress = ""
        _schedule_event_loop()
        return {"FINISHED"}


class NYMPHSV2_OT_start_service(bpy.types.Operator):
    bl_idname = "nymphsv2.start_service"
    bl_label = "Start Service"
    bl_description = "Launch one managed local WSL backend"

    service_key: StringProperty()

    def execute(self, context):
        state = context.scene.nymphs3d2v2_state
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
        state = context.scene.nymphs3d2v2_state
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
        state = context.scene.nymphs3d2v2_state
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
        state = context.scene.nymphs3d2v2_state
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
        state = context.scene.nymphs3d2v2_state
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
    bl_description = "Generate one image through Z-Image and assign it to Image"

    def execute(self, context):
        state = context.scene.nymphs3d2v2_state
        if state.is_busy or state.imagegen_is_busy:
            self.report({"WARNING"}, "Another request is already running.")
            return {"CANCELLED"}

        api_root = _normalize_api_root(_service_api_root(state, "n2d2"))
        try:
            _require_network_access(api_root)
            variant_count = max(1, int(getattr(state, "imagegen_variant_count", 1)))
            seed_step = max(1, int(getattr(state, "imagegen_seed_step", 1)))
            if variant_count > 1:
                base_seed, generated = _imagegen_seed_value(state)
                payload = [
                    _build_imagegen_payload_for_prompt(
                        state,
                        prompt=(state.imagegen_prompt or "").strip(),
                        seed=base_seed + (index * seed_step),
                    )
                    for index in range(variant_count)
                ]
            else:
                payload = _build_imagegen_payload(state)
                generated = False
        except Exception as exc:
            state.imagegen_status_text = str(exc)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        state.imagegen_is_busy = True
        state.imagegen_started_at = time.time()
        if variant_count > 1:
            state.imagegen_status_text = f"Generating {variant_count} image variants..."
            state.imagegen_task_stage = "Generating Variants"
            state.imagegen_task_detail = "Waiting for image backend progress..."
            state.imagegen_task_progress = f"0/{variant_count}"
            if generated:
                state.imagegen_seed = str(base_seed)
        else:
            state.imagegen_status_text = "Generating image..."
            state.imagegen_task_stage = "Generating Image"
            state.imagegen_task_detail = "Waiting for image backend progress..."
            state.imagegen_task_progress = ""
        state.imagegen_task_status = "queued"
        state.imagegen_output_path = ""
        state.imagegen_metadata_path = ""
        _touch_ui()

        worker = threading.Thread(
            target=_imagegen_worker,
            args=(context.scene.name, api_root, payload),
            daemon=True,
        )
        worker.start()
        _schedule_event_loop()
        return {"FINISHED"}


class NYMPHSV2_OT_generate_mv_set(bpy.types.Operator):
    bl_idname = "nymphsv2.generate_mv_set"
    bl_label = "Generate MV Set"
    bl_description = "Generate front, left, right, and back images through Z-Image and assign them to the multiview slots"

    def execute(self, context):
        state = context.scene.nymphs3d2v2_state
        if state.is_busy or state.imagegen_is_busy:
            self.report({"WARNING"}, "Another request is already running.")
            return {"CANCELLED"}

        api_root = _normalize_api_root(_service_api_root(state, "n2d2"))
        try:
            _require_network_access(api_root)
            if not state.imagegen_prompt.strip():
                raise RuntimeError("Enter an image-generation prompt first.")
            seed, generated = _imagegen_seed_value(state)
        except Exception as exc:
            state.imagegen_status_text = str(exc)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        state.imagegen_is_busy = True
        state.imagegen_started_at = time.time()
        state.imagegen_status_text = "Generating MV set..."
        state.imagegen_task_status = "queued"
        state.imagegen_task_stage = "Generating MV Set"
        state.imagegen_task_detail = "Preparing front, left, right, and back prompts..."
        state.imagegen_task_progress = "0/4"
        state.imagegen_output_path = ""
        state.imagegen_metadata_path = ""
        if generated:
            state.imagegen_seed = str(seed)
        _touch_ui()

        worker = threading.Thread(
            target=_imagegen_mv_worker,
            args=(context.scene.name, api_root, seed),
            daemon=True,
        )
        worker.start()
        _schedule_event_loop()
        return {"FINISHED"}


class NYMPHSV2_OT_load_prompt_preset(bpy.types.Operator):
    bl_idname = "nymphsv2.load_prompt_preset"
    bl_label = "Load Preset"
    bl_description = "Load the selected prompt preset without changing image settings"

    def execute(self, context):
        state = context.scene.nymphs3d2v2_state
        preset = _imagegen_prompt_preset_data(_sync_imagegen_prompt_preset(state))
        state.imagegen_prompt = preset["prompt"]
        state.imagegen_status_text = f"Loaded preset: {preset['label']}"
        _touch_ui()
        return {"FINISHED"}


class NYMPHSV2_OT_load_imagegen_settings_preset(bpy.types.Operator):
    bl_idname = "nymphsv2.load_imagegen_settings_preset"
    bl_label = "Apply Generation Profile"
    bl_description = "Apply the selected Z-Image profile. Built-in profiles can change model rank, image settings, and variants without changing prompt text."

    def execute(self, context):
        state = context.scene.nymphs3d2v2_state
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
        state = context.scene.nymphs3d2v2_state
        current = _imagegen_settings_preset_data(_sync_imagegen_settings_preset(state))
        self.name = current.get("label", "") if current.get("values") else ""
        return context.window_manager.invoke_props_dialog(self, width=420)

    def execute(self, context):
        state = context.scene.nymphs3d2v2_state
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
        state = context.scene.nymphs3d2v2_state
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
        state = context.scene.nymphs3d2v2_state
        current = _imagegen_prompt_preset_data(_sync_imagegen_prompt_preset(state))
        self.name = current.get("label", "") if current.get("prompt") else ""
        return context.window_manager.invoke_props_dialog(self, width=420)

    def execute(self, context):
        state = context.scene.nymphs3d2v2_state
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
        state = context.scene.nymphs3d2v2_state
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


class NYMPHSV2_OT_load_trellis_shape_preset(bpy.types.Operator):
    bl_idname = "nymphsv2.load_trellis_shape_preset"
    bl_label = "Apply TRELLIS Preset"
    bl_description = "Apply the selected TRELLIS shape preset"

    def execute(self, context):
        state = context.scene.nymphs3d2v2_state
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
        state = context.scene.nymphs3d2v2_state
        current = _trellis_shape_preset_data(_sync_trellis_shape_preset(state))
        self.name = current.get("label", "") if current.get("values") else ""
        return context.window_manager.invoke_props_dialog(self, width=420)

    def execute(self, context):
        state = context.scene.nymphs3d2v2_state
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
        state = context.scene.nymphs3d2v2_state
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
        ),
        default="prompt",
    )

    def execute(self, context):
        state = context.scene.nymphs3d2v2_state
        state.imagegen_prompt = ""
        state.imagegen_status_text = "Cleared image prompt."
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
        ),
        default="prompt",
    )

    def execute(self, context):
        state = context.scene.nymphs3d2v2_state
        text = _ensure_imagegen_text(state, self.target)
        opened = _open_text_in_editor(context, text)
        if opened:
            state.status_text = "Editing prompt in Text Editor. Return here and click Apply Text."
        else:
            state.status_text = f"Prepared prompt text block: {text.name}"
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
        ),
        default="prompt",
    )

    def execute(self, context):
        state = context.scene.nymphs3d2v2_state
        applied = []
        if _pull_imagegen_text_from_block(state, "prompt"):
            applied.append("prompt")
        if not applied:
            self.report({"WARNING"}, "No linked prompt text block was found.")
            return {"CANCELLED"}
        state.imagegen_status_text = "Loaded text from Blender Text Editor."
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
        ),
        default="prompt",
    )

    prompt: StringProperty(
        name="Prompt",
        description="Prompt text",
        default="",
    )

    def invoke(self, context, event):
        state = context.scene.nymphs3d2v2_state
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
        body.label(text="Prompt")
        prompt_row = body.row()
        prompt_row.scale_y = 3.0
        prompt_row.prop(self, "prompt", text="")
        body.label(text="For long prompts, use Text Editor in the panel.")

    def execute(self, context):
        state = context.scene.nymphs3d2v2_state
        setattr(state, _imagegen_text_field_name(self.target), self.prompt)
        _touch_ui()
        return {"FINISHED"}


class NYMPHSV2_OT_open_imagegen_folder(bpy.types.Operator):
    bl_idname = "nymphsv2.open_imagegen_folder"
    bl_label = "Open Folder"
    bl_description = "Open the folder that contains the generated Z-Image images"

    def execute(self, context):
        state = context.scene.nymphs3d2v2_state
        folder_path = (state.imagegen_output_dir or "").strip()
        if not folder_path and state.imagegen_output_path.strip():
            folder_path = os.path.dirname(state.imagegen_output_path.strip())
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
        state = context.scene.nymphs3d2v2_state
        folder_path = (state.imagegen_output_dir or "").strip()
        if not folder_path and state.imagegen_output_path.strip():
            folder_path = os.path.dirname(state.imagegen_output_path.strip())
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
        state = context.scene.nymphs3d2v2_state
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
        state = context.scene.nymphs3d2v2_state
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


class NYMPHSV2_OT_parts_placeholder_action(bpy.types.Operator):
    bl_idname = "nymphsv2.parts_placeholder_action"
    bl_label = "Run Parts Backend"
    bl_description = "Run the experimental Nymphs Parts workflow"

    backend_choice: StringProperty(
        name="Backend",
        default="",
    )

    def execute(self, context):
        state = context.scene.nymphs3d2v2_state
        repo_status = _parts_repo_status(state)
        if not repo_status["repo_exists"]:
            state.parts_status_text = "Parts repo path is missing. Set the local Hunyuan3D-Part path first."
            _touch_ui()
            self.report({"ERROR"}, state.parts_status_text)
            return {"CANCELLED"}
        backend_choice = (self.backend_choice or state.parts_backend_choice or "P3SAM").upper()
        parts_mode = "SEGMENT" if backend_choice == "P3SAM" else "DECOMPOSE"
        state.parts_backend_choice = backend_choice
        state.parts_mode = parts_mode
        backend_label = _parts_backend_label(backend_choice)
        if backend_choice == "P3SAM":
            prepared_path = (state.parts_prepared_source_path or "").strip()
            if not prepared_path:
                state.parts_status_text = "Prepare a source mesh first."
                _touch_ui()
                self.report({"ERROR"}, state.parts_status_text)
                return {"CANCELLED"}
            if not os.path.exists(prepared_path):
                state.parts_status_text = "Prepared source is missing. Prepare it again."
                _touch_ui()
                self.report({"ERROR"}, state.parts_status_text)
                return {"CANCELLED"}
            source_ok, source_label = _parts_source_summary(context, state)
            if not source_ok:
                state.parts_status_text = source_label
                _touch_ui()
                self.report({"ERROR"}, source_label)
                return {"CANCELLED"}
            if not repo_status["p3_sam_demo"]:
                state.parts_status_text = "P3-SAM was not detected in the configured repo. Segmentation is not ready yet."
                _touch_ui()
                self.report({"ERROR"}, state.parts_status_text)
                return {"CANCELLED"}
            source_object_name = (state.parts_prepared_source_object_name or "").strip()
            if not source_object_name and (state.parts_source_mode or "SELECTED").upper() == "SELECTED":
                selected_meshes = [obj for obj in getattr(context, "selected_objects", []) if getattr(obj, "type", "") == "MESH"]
                active_object = getattr(context, "active_object", None)
                source_object = active_object if active_object in selected_meshes else (selected_meshes[0] if selected_meshes else None)
                source_object_name = source_object.name if source_object is not None else ""
            state.is_busy = True
            state.status_text = f"Running {backend_label}..."
            state.parts_status_text = f"Running {backend_label} on {source_label}..."
            state.task_status = ""
            state.task_stage = ""
            state.task_detail = ""
            state.task_progress = ""
            state.task_message = ""
            state.waiting_for_backend_progress = False
            state.last_result = ""
            state.parts_output_path = ""
            state.parts_output_dir = ""
            state.parts_last_imported_object_name = ""
            state.parts_stage1_manifest_path = ""
            state.parts_stage1_summary_path = ""
            state.parts_status_stage = f"Starting {backend_label}"
            state.parts_status_detail = f"Running {backend_label} on {source_label}..."
            state.parts_status_progress = ""
            state.parts_log_path = ""
            state.parts_started_at = time.time()
            state.parts_wsl_pid = 0
            state.parts_host_usage_text = ""
            _touch_ui()
            snapshot = {
                "wsl_distro_name": _resolved_wsl_distro_name(state),
                "wsl_user_name": _resolved_wsl_user_name(state),
                "parts_repo_path": state.parts_repo_path,
                "parts_python_path": state.parts_python_path,
                "parts_backend_choice": backend_choice,
                "parts_point_num": state.parts_point_num,
                "parts_prompt_num": state.parts_prompt_num,
                "parts_prompt_bs": state.parts_prompt_bs,
                "parts_stage1_manifest_path": state.parts_stage1_manifest_path,
                "parts_keep_recent_runs": state.parts_keep_recent_runs,
                "parts_new_collection": state.parts_new_collection,
                "parts_collection_name": state.parts_collection_name,
                "parts_last_imported_object_name": state.parts_last_imported_object_name,
            }
            worker = threading.Thread(
                target=_parts_job_worker,
                args=(
                    context.scene.name,
                    snapshot,
                    prepared_path,
                    source_object_name,
                    not bool(state.parts_keep_original),
                ),
                daemon=True,
            )
            worker.start()
            _schedule_event_loop()
            return {"FINISHED"}
        else:
            manifest_path = (state.parts_stage1_manifest_path or "").strip()
            if not repo_status["xpart_demo"]:
                state.parts_status_text = "X-Part code was not detected in the configured repo."
                _touch_ui()
                self.report({"ERROR"}, state.parts_status_text)
                return {"CANCELLED"}
            if not manifest_path:
                state.parts_status_text = "Run Analyze Mesh first so X-Part has a Stage-1 manifest."
                _touch_ui()
                self.report({"ERROR"}, state.parts_status_text)
                return {"CANCELLED"}
            if not os.path.exists(manifest_path):
                state.parts_status_text = "Stage-1 manifest is missing. Analyze the mesh again."
                _touch_ui()
                self.report({"ERROR"}, state.parts_status_text)
                return {"CANCELLED"}
            source_object_name = (state.parts_prepared_source_object_name or "").strip()
            if not source_object_name and (state.parts_source_mode or "SELECTED").upper() == "SELECTED":
                selected_meshes = [obj for obj in getattr(context, "selected_objects", []) if getattr(obj, "type", "") == "MESH"]
                active_object = getattr(context, "active_object", None)
                source_object = active_object if active_object in selected_meshes else (selected_meshes[0] if selected_meshes else None)
                source_object_name = source_object.name if source_object is not None else ""
            state.is_busy = True
            state.status_text = f"Running {backend_label}..."
            state.parts_status_text = f"Running {backend_label} from Stage-1 analysis..."
            state.task_status = ""
            state.task_stage = ""
            state.task_detail = ""
            state.task_progress = ""
            state.task_message = ""
            state.waiting_for_backend_progress = False
            state.last_result = ""
            state.parts_output_path = ""
            state.parts_output_dir = ""
            state.parts_status_stage = f"Starting {backend_label}"
            state.parts_status_detail = f"Running {backend_label} from Stage-1 analysis..."
            state.parts_status_progress = ""
            state.parts_log_path = ""
            state.parts_started_at = time.time()
            state.parts_wsl_pid = 0
            state.parts_host_usage_text = ""
            _touch_ui()
            snapshot = {
                "wsl_distro_name": _resolved_wsl_distro_name(state),
                "wsl_user_name": _resolved_wsl_user_name(state),
                "parts_repo_path": state.parts_repo_path,
                "parts_python_path": state.parts_python_path,
                "parts_backend_choice": backend_choice,
                "parts_stage1_manifest_path": state.parts_stage1_manifest_path,
                "parts_xpart_steps": state.parts_xpart_steps,
                "parts_xpart_octree_resolution": state.parts_xpart_octree_resolution,
                "parts_xpart_dtype": state.parts_xpart_dtype,
                "parts_xpart_max_aabb": state.parts_xpart_max_aabb,
                "parts_xpart_cpu_threads": state.parts_xpart_cpu_threads,
                "parts_keep_recent_runs": state.parts_keep_recent_runs,
                "parts_new_collection": state.parts_new_collection,
                "parts_collection_name": state.parts_collection_name,
                "parts_last_imported_object_name": state.parts_last_imported_object_name,
            }
            worker = threading.Thread(
                target=_parts_job_worker,
                args=(
                    context.scene.name,
                    snapshot,
                    manifest_path,
                    source_object_name,
                    not bool(state.parts_keep_original),
                ),
                daemon=True,
            )
            worker.start()
            _schedule_event_loop()
            return {"FINISHED"}


class NYMPHSV2_OT_prepare_parts_source(bpy.types.Operator):
    bl_idname = "nymphsv2.prepare_parts_source"
    bl_label = "Prepare Parts Source"
    bl_description = "Export or copy a source mesh into a temp file for the experimental parts workflow"

    def execute(self, context):
        state = context.scene.nymphs3d2v2_state
        try:
            prepared_path, source_label, source_object_name = _prepare_parts_source(context, state)
        except Exception as exc:
            state.parts_status_text = str(exc)
            _touch_ui()
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        state.parts_prepared_source_path = prepared_path
        state.parts_prepared_source_object_name = source_object_name
        state.parts_status_text = f"Prepared parts source from {source_label}."
        _touch_ui()
        return {"FINISHED"}


class NYMPHSV2_OT_open_parts_folder(bpy.types.Operator):
    bl_idname = "nymphsv2.open_parts_folder"
    bl_label = "Open Parts Sources"
    bl_description = "Open the temp folder used for prepared Parts source files"

    def execute(self, context):
        folder_path = _parts_output_dir()
        try:
            bpy.ops.wm.path_open(filepath=folder_path)
        except Exception as exc:
            self.report({"ERROR"}, f"Could not open folder: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class NYMPHSV2_OT_clear_parts_sources_folder(bpy.types.Operator):
    bl_idname = "nymphsv2.clear_parts_sources_folder"
    bl_label = "Clear Parts Sources"
    bl_description = "Delete prepared source mesh files from the Parts temp folder"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        state = context.scene.nymphs3d2v2_state
        if _parts_process_is_active(context.scene.name):
            self.report({"ERROR"}, "Stop the current Parts run before clearing sources.")
            return {"CANCELLED"}
        folder_path = _parts_output_dir()
        try:
            _clear_folder_contents(folder_path)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        state.parts_prepared_source_path = ""
        state.parts_prepared_source_object_name = ""
        state.parts_status_text = "Cleared prepared Parts sources."
        _touch_ui()
        return {"FINISHED"}


class NYMPHSV2_OT_open_parts_outputs_folder(bpy.types.Operator):
    bl_idname = "nymphsv2.open_parts_outputs_folder"
    bl_label = "Open Parts Outputs"
    bl_description = "Open the cached Parts run output folder inside the WSL runtime"

    def execute(self, context):
        state = context.scene.nymphs3d2v2_state
        folder_path = _parts_result_root_host(state)
        if not folder_path:
            self.report({"ERROR"}, "No Parts output folder is available yet.")
            return {"CANCELLED"}
        os.makedirs(folder_path, exist_ok=True)
        try:
            bpy.ops.wm.path_open(filepath=folder_path)
        except Exception as exc:
            self.report({"ERROR"}, f"Could not open folder: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class NYMPHSV2_OT_prune_parts_outputs(bpy.types.Operator):
    bl_idname = "nymphsv2.prune_parts_outputs"
    bl_label = "Prune Parts Outputs"
    bl_description = "Delete older Parts run folders while keeping the newest configured set and current references"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        state = context.scene.nymphs3d2v2_state
        if _parts_process_is_active(context.scene.name):
            self.report({"ERROR"}, "Stop the current Parts run before pruning outputs.")
            return {"CANCELLED"}
        try:
            removed = _prune_parts_result_dirs(state, keep_recent=state.parts_keep_recent_runs)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        state.parts_status_text = (
            f"Pruned {removed} Parts run folder{'s' if removed != 1 else ''}. "
            f"Keeping {state.parts_keep_recent_runs} recent run{'s' if state.parts_keep_recent_runs != 1 else ''}."
        )
        _touch_ui()
        return {"FINISHED"}


class NYMPHSV2_OT_clear_parts_outputs_folder(bpy.types.Operator):
    bl_idname = "nymphsv2.clear_parts_outputs_folder"
    bl_label = "Clear Parts Outputs"
    bl_description = "Delete all cached Parts run output folders"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        state = context.scene.nymphs3d2v2_state
        if _parts_process_is_active(context.scene.name):
            self.report({"ERROR"}, "Stop the current Parts run before clearing outputs.")
            return {"CANCELLED"}
        folder_path = _parts_result_root_host(state)
        try:
            _clear_folder_contents(folder_path)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        for attr_name in (
            "parts_output_path",
            "parts_output_dir",
            "parts_stage1_manifest_path",
            "parts_stage1_summary_path",
        ):
            if _path_is_within(folder_path, getattr(state, attr_name, "")):
                setattr(state, attr_name, "")
        state.parts_started_at = 0.0
        state.parts_wsl_pid = 0
        state.parts_host_usage_text = ""
        state.parts_last_imported_object_name = ""
        state.parts_status_stage = ""
        state.parts_status_detail = ""
        state.parts_status_progress = ""
        state.parts_log_path = ""
        state.parts_status_text = "Cleared cached Parts outputs."
        _touch_ui()
        return {"FINISHED"}


class NYMPHSV2_OT_open_parts_repo(bpy.types.Operator):
    bl_idname = "nymphsv2.open_parts_repo"
    bl_label = "Open Parts Repo"
    bl_description = "Open the configured Hunyuan3D-Part repo folder"

    def execute(self, context):
        state = context.scene.nymphs3d2v2_state
        folder_path = _resolved_user_path(state.parts_repo_path.strip() or DEFAULT_REPO_PARTS_PATH, state)
        if not os.path.isdir(folder_path):
            self.report({"ERROR"}, f"Repo folder does not exist: {folder_path}")
            return {"CANCELLED"}
        folder_path = _to_blender_accessible_path(state, folder_path)
        try:
            bpy.ops.wm.path_open(filepath=folder_path)
        except Exception as exc:
            self.report({"ERROR"}, f"Could not open folder: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class NYMPHSV2_OT_stop_parts_run(bpy.types.Operator):
    bl_idname = "nymphsv2.stop_parts_run"
    bl_label = "Stop Parts Run"
    bl_description = "Stop the current local P3-SAM or X-Part run"

    def execute(self, context):
        state = context.scene.nymphs3d2v2_state
        scene_name = context.scene.name
        if not _parts_process_is_active(scene_name):
            state.parts_status_text = "No active parts run to stop."
            state.parts_status_stage = ""
            state.parts_status_detail = state.parts_status_text
            state.parts_status_progress = ""
            state.status_text = state.parts_status_text
            _touch_ui()
            self.report({"WARNING"}, state.parts_status_text)
            return {"CANCELLED"}
        stopped = _terminate_parts_process(scene_name)
        if not stopped:
            state.parts_status_text = "Could not stop the current parts run."
            state.parts_status_stage = "Failed"
            state.parts_status_detail = state.parts_status_text
            state.parts_status_progress = ""
            state.status_text = state.parts_status_text
            _touch_ui()
            self.report({"ERROR"}, state.parts_status_text)
            return {"CANCELLED"}
        state.parts_status_text = "Stopping parts run..."
        state.parts_status_stage = "Stopping"
        state.parts_status_detail = state.parts_status_text
        state.parts_status_progress = ""
        state.status_text = state.parts_status_text
        _touch_ui()
        return {"FINISHED"}


class NYMPHSV2_PT_server(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Nymphs"
    bl_label = "Nymphs Server"

    def draw(self, context):
        state = context.scene.nymphs3d2v2_state
        layout = self.layout

        layout.prop(
            state,
            "show_server",
            text="Server",
            icon="TRIA_DOWN" if state.show_server else "TRIA_RIGHT",
            emboss=False,
        )
        if not state.show_server:
            return

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
        if (state.parts_host_usage_text or "").strip():
            _draw_wrapped_lines(top, state.parts_host_usage_text, prefix="Local Parts: ", width=52, max_lines=2)
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


class NYMPHSV2_PT_image_generation(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Nymphs"
    bl_label = "Nymphs Image"

    def draw(self, context):
        state = context.scene.nymphs3d2v2_state
        layout = self.layout

        panel = layout.box()
        panel.prop(
            state,
            "show_image_generation",
            text="Image Generation",
            icon="TRIA_DOWN" if state.show_image_generation else "TRIA_RIGHT",
            emboss=False,
        )
        if not state.show_image_generation:
            return

        if not _service_runtime_is_available(state, "n2d2"):
            hint = panel.box()
            hint.label(text="Start Z-Image in Runtimes.")
            _draw_service_control_row(hint, state, "n2d2")
            return

        _sync_imagegen_prompt_preset(state)
        _sync_imagegen_settings_preset(state)

        _draw_service_control_row(panel, state, "n2d2")

        request = panel.box()

        settings_label_row = request.row(align=True)
        settings_label_row.label(text="Profile")
        settings_label_row.operator("nymphsv2.load_imagegen_settings_preset", text="Apply")
        settings_preset_row = request.row(align=True)
        settings_preset_row.prop(state, "imagegen_settings_preset", text="")
        settings_preset_tools = request.row(align=True)
        settings_preset_tools.operator("nymphsv2.save_imagegen_settings_preset", text="Save")
        settings_preset_tools.operator("nymphsv2.delete_imagegen_settings_preset", text="Delete")
        settings_preset_tools.operator("nymphsv2.open_imagegen_settings_presets_folder", text="Open")

        prompt_preset_label_row = request.row(align=True)
        prompt_preset_label_row.label(text="Prompt Preset")
        prompt_preset_label_row.operator("nymphsv2.load_prompt_preset", text="Load")
        prompt_preset_row = request.row(align=True)
        prompt_preset_row.prop(state, "imagegen_prompt_preset", text="")
        prompt_preset_tools = request.row(align=True)
        prompt_preset_tools.operator("nymphsv2.save_prompt_preset", text="Save")
        prompt_preset_tools.operator("nymphsv2.delete_prompt_preset", text="Delete")
        prompt_preset_tools.operator("nymphsv2.open_prompt_presets_folder", text="Open")

        prompt_row = request.row(align=True)
        prompt_row.label(text="Prompt")
        large_prompt = prompt_row.operator("nymphsv2.open_image_prompt_text_block", text="Text Editor")
        large_prompt.target = "prompt"
        prompt_tools = request.row(align=True)
        edit_prompt = prompt_tools.operator("nymphsv2.edit_image_prompts", text="Edit")
        edit_prompt.target = "prompt"
        clear_prompt = prompt_tools.operator("nymphsv2.clear_image_prompt_field", text="Clear")
        clear_prompt.target = "prompt"
        if state.imagegen_prompt_text_name:
            use_prompt_text = prompt_tools.operator("nymphsv2.pull_image_prompt_text_block", text="Apply Text")
            use_prompt_text.target = "prompt"
        prompt_field_row = request.row(align=True)
        prompt_field_row.prop(state, "imagegen_prompt", text="")
        size_row = request.row(align=True)
        size_row.prop(state, "imagegen_width")
        size_row.prop(state, "imagegen_height")
        settings_row = request.row(align=True)
        settings_row.prop(state, "imagegen_steps")
        settings_row.prop(state, "imagegen_guidance_scale")

        seed_row = request.row(align=True)
        seed_row.prop(state, "imagegen_seed", text="Seed")
        variant_row = request.row(align=True)
        variant_row.prop(state, "imagegen_variant_count")
        variant_row.prop(state, "imagegen_seed_step", text="Step")

        action = request.row(align=True)
        action.enabled = not state.is_busy and not state.imagegen_is_busy
        generate_label = "Generate Image" if int(getattr(state, "imagegen_variant_count", 1)) <= 1 else "Generate Variants"
        action.operator("nymphsv2.generate_image", text=generate_label)
        action.operator("nymphsv2.generate_mv_set", text="Generate 4-View MV")

        image_status = (state.imagegen_status_text or "").strip()
        status_lower = image_status.lower()
        has_image_output = bool(state.imagegen_output_dir.strip() or state.imagegen_output_path.strip())
        passive_image_status = (
            not image_status
            or status_lower == "idle"
            or (state.imagegen_output_path and status_lower.startswith("image generated"))
            or (state.imagegen_output_path and status_lower.startswith("generated "))
        )
        if state.imagegen_is_busy or not passive_image_status or has_image_output:
            result = panel.box()
            if state.imagegen_is_busy or not passive_image_status:
                result.label(text=f"Status: {image_status or 'Working...'}"[:160])
            if state.imagegen_output_path:
                result.label(text=f"Last Image: {_path_leaf(state.imagegen_output_path)}"[:160])
            action_row = result.row(align=True)
            action_row.enabled = has_image_output
            action_row.operator("nymphsv2.open_imagegen_folder", text="Open Folder")
            action_row.operator("nymphsv2.clear_imagegen_folder", text="Clear Folder")


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
        state = context.scene.nymphs3d2v2_state
        layout = self.layout

        panel = layout.box()
        panel.prop(
            state,
            "show_shape",
            text="Shape Request",
            icon="TRIA_DOWN" if state.show_shape else "TRIA_RIGHT",
            emboss=False,
        )
        if not state.show_shape:
            return

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
        state = context.scene.nymphs3d2v2_state
        layout = self.layout
        panel = layout.box()

        panel.prop(
            state,
            "show_texture",
            text="Texture Request",
            icon="TRIA_DOWN" if state.show_texture else "TRIA_RIGHT",
            emboss=False,
        )
        if not state.show_texture:
            return

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


class NYMPHSV2_PT_parts(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Nymphs"
    bl_label = "Nymphs Parts"

    def draw(self, context):
        state = context.scene.nymphs3d2v2_state
        layout = self.layout
        panel = layout.box()
        panel.prop(
            state,
            "show_parts",
            text="Nymphs Parts",
            icon="TRIA_DOWN" if state.show_parts else "TRIA_RIGHT",
            emboss=False,
        )
        if not state.show_parts:
            return

        source_ok, source_label = _parts_source_summary(context, state)
        repo_status = _parts_repo_status(state)
        parts_active = _parts_run_is_active(state, context.scene.name)

        backend = panel.box()
        backend.prop(
            state,
            "show_parts_backend",
            text="Experimental Backend",
            icon="TRIA_DOWN" if state.show_parts_backend else "TRIA_RIGHT",
            emboss=False,
        )
        if state.show_parts_backend:
            _draw_labeled_prop(backend, state, "parts_repo_path", "Repo Path")
            _draw_labeled_prop(backend, state, "parts_python_path", "Python Path")
            backend.label(text=f"P3-SAM: {'Found' if repo_status['p3_sam_demo'] else 'Missing'}")
            backend.label(text=f"X-Part: {'Research Only' if repo_status['xpart_demo'] else 'Missing'}")
            if repo_status["python_exists"]:
                backend.label(text="Python: Found")
            else:
                backend.label(text="Python: Missing")
            repo_actions = backend.row(align=True)
            repo_actions.operator("nymphsv2.open_parts_repo", text="Open Repo")
            repo_actions.operator("nymphsv2.open_parts_outputs_folder", text="Open Outputs")
            _draw_wrapped_lines(backend, repo_status["summary"], prefix="Status: ", width=44, max_lines=3)

        stage1 = panel.box()
        stage1.prop(
            state,
            "show_parts_stage1",
            text="Stage 1: Analyze Mesh",
            icon="TRIA_DOWN" if state.show_parts_stage1 else "TRIA_RIGHT",
            emboss=False,
        )
        if state.show_parts_stage1:
            source_box = stage1.box()
            source_box.label(text="Source")
            source_box.prop(state, "parts_source_mode")
            if state.parts_source_mode == "SELECTED":
                source_box.prop(state, "parts_export_format")
            if source_ok:
                _draw_wrapped_lines(source_box, source_label, width=44, max_lines=2)
            else:
                _draw_wrapped_lines(source_box, source_label, prefix="Missing: ", width=44, max_lines=2)
            prep = source_box.row(align=True)
            prep.enabled = source_ok and not parts_active
            prep.operator("nymphsv2.prepare_parts_source", text="Prepare Source")

            if state.parts_prepared_source_path:
                prepared = stage1.box()
                _draw_wrapped_lines(
                    prepared,
                    state.parts_prepared_source_path,
                    prefix="Prepared: ",
                    width=44,
                    max_lines=3,
                )

            _draw_wrapped_lines(
                stage1,
                "P3-SAM segments the prepared mesh and writes the Stage-1 bundle X-Part consumes.",
                width=46,
                max_lines=2,
            )
            stage1_tuning = stage1.column(align=True)
            stage1_tuning.prop(state, "parts_point_num")
            stage1_tuning.prop(state, "parts_prompt_num")
            stage1_tuning.prop(state, "parts_prompt_bs")
            stage1_action = stage1.row(align=True)
            stage1_action.enabled = bool((state.parts_prepared_source_path or "").strip()) and repo_status["p3_sam_demo"] and not parts_active
            analyze_op = stage1_action.operator("nymphsv2.parts_placeholder_action", text="Analyze Mesh")
            analyze_op.backend_choice = "P3SAM"

            if state.parts_stage1_manifest_path:
                analysis = stage1.box()
                _draw_wrapped_lines(
                    analysis,
                    state.parts_stage1_manifest_path,
                    prefix="Analysis: ",
                    width=44,
                    max_lines=3,
                )
            else:
                stage1.label(text="No Stage-1 analysis saved yet.")

        stage2 = panel.box()
        stage2.prop(
            state,
            "show_parts_stage2",
            text="Stage 2: Generate Parts",
            icon="TRIA_DOWN" if state.show_parts_stage2 else "TRIA_RIGHT",
            emboss=False,
        )
        if state.show_parts_stage2:
            _draw_wrapped_lines(
                stage2,
                "X-Part consumes the latest saved Stage-1 manifest and tries to generate separated parts.",
                width=46,
                max_lines=2,
            )
            stage2_tuning = stage2.column(align=True)
            stage2_tuning.prop(state, "parts_xpart_steps")
            stage2_tuning.prop(state, "parts_xpart_octree_resolution")
            stage2_tuning.prop(state, "parts_xpart_max_aabb")
            stage2_tuning.prop(state, "parts_xpart_dtype")
            stage2_tuning.prop(state, "parts_xpart_cpu_threads")
            if state.parts_stage1_manifest_path:
                _draw_wrapped_lines(
                    stage2,
                    _path_leaf(state.parts_stage1_manifest_path),
                    prefix="Using: ",
                    width=44,
                    max_lines=2,
                )
            else:
                stage2.label(text="Run Stage 1 first.")
            stage2_action = stage2.row(align=True)
            stage2_action.enabled = bool((state.parts_stage1_manifest_path or "").strip()) and repo_status["xpart_demo"] and not parts_active
            generate_op = stage2_action.operator("nymphsv2.parts_placeholder_action", text="Generate Parts")
            generate_op.backend_choice = "XPART"

        if state.parts_output_path:
            result_box = panel.box()
            _draw_wrapped_lines(
                result_box,
                state.parts_output_path,
                prefix="Last Result: ",
                width=44,
                max_lines=3,
            )

        support = panel.box()
        support.prop(
            state,
            "show_parts_support",
            text="Import & Storage",
            icon="TRIA_DOWN" if state.show_parts_support else "TRIA_RIGHT",
            emboss=False,
        )
        if state.show_parts_support:
            support.prop(state, "parts_keep_original")
            support.prop(state, "parts_new_collection")
            if state.parts_new_collection:
                support.prop(state, "parts_collection_name")
            support.prop(state, "parts_keep_recent_runs")
            source_actions = support.row(align=True)
            source_actions.enabled = not parts_active
            source_actions.operator("nymphsv2.open_parts_folder", text="Open Sources")
            source_actions.operator("nymphsv2.clear_parts_sources_folder", text="Clear Sources")
            output_actions = support.row(align=True)
            output_actions.enabled = not parts_active
            output_actions.operator("nymphsv2.open_parts_outputs_folder", text="Open Outputs")
            output_actions.operator("nymphsv2.prune_parts_outputs", text="Prune Outputs")
            clear_outputs = support.row(align=True)
            clear_outputs.enabled = not parts_active
            clear_outputs.operator("nymphsv2.clear_parts_outputs_folder", text="Clear Outputs")

        status = panel.box()
        status.prop(
            state,
            "show_parts_status",
            text="Run Status",
            icon="TRIA_DOWN" if state.show_parts_status else "TRIA_RIGHT",
            emboss=False,
        )
        if state.show_parts_status:
            action = status.row(align=True)
            stop_op = action.row(align=True)
            stop_op.enabled = parts_active
            stop_op.operator("nymphsv2.stop_parts_run", text="Stop Parts Run")
            if state.parts_host_usage_text:
                _draw_wrapped_lines(status, state.parts_host_usage_text, prefix="Load: ", width=46, max_lines=2)
            if state.parts_wsl_pid:
                status.label(text=f"PID: {state.parts_wsl_pid}")
            if (state.parts_status_stage or "").strip():
                _draw_wrapped_lines(status, state.parts_status_stage, prefix="Stage: ", width=46, max_lines=2)
            if (state.parts_status_detail or "").strip():
                _draw_wrapped_lines(status, state.parts_status_detail, prefix="Detail: ", width=46, max_lines=3)
            if (state.parts_status_progress or "").strip():
                _draw_wrapped_lines(status, state.parts_status_progress, prefix="Progress: ", width=46, max_lines=2)
            _draw_wrapped_lines(status, state.parts_status_text, prefix="Status: ", width=46, max_lines=3)
            if (state.parts_log_path or "").strip():
                _draw_wrapped_lines(status, state.parts_log_path, prefix="Log: ", width=46, max_lines=3)


CLASSES = (
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
    NYMPHSV2_OT_load_prompt_preset,
    NYMPHSV2_OT_load_imagegen_settings_preset,
    NYMPHSV2_OT_save_imagegen_settings_preset,
    NYMPHSV2_OT_delete_imagegen_settings_preset,
    NYMPHSV2_OT_open_imagegen_settings_presets_folder,
    NYMPHSV2_OT_save_prompt_preset,
    NYMPHSV2_OT_delete_prompt_preset,
    NYMPHSV2_OT_open_prompt_presets_folder,
    NYMPHSV2_OT_load_trellis_shape_preset,
    NYMPHSV2_OT_save_trellis_shape_preset,
    NYMPHSV2_OT_delete_trellis_shape_preset,
    NYMPHSV2_OT_open_trellis_shape_presets_folder,
    NYMPHSV2_OT_clear_image_prompt_field,
    NYMPHSV2_OT_open_image_prompt_text_block,
    NYMPHSV2_OT_pull_image_prompt_text_block,
    NYMPHSV2_OT_edit_image_prompts,
    NYMPHSV2_OT_open_imagegen_folder,
    NYMPHSV2_OT_clear_imagegen_folder,
    NYMPHSV2_OT_open_shape_folder,
    NYMPHSV2_OT_clear_shape_folder,
    NYMPHSV2_OT_prepare_parts_source,
    NYMPHSV2_OT_open_parts_folder,
    NYMPHSV2_OT_clear_parts_sources_folder,
    NYMPHSV2_OT_open_parts_outputs_folder,
    NYMPHSV2_OT_prune_parts_outputs,
    NYMPHSV2_OT_clear_parts_outputs_folder,
    NYMPHSV2_OT_open_parts_repo,
    NYMPHSV2_OT_stop_parts_run,
    NYMPHSV2_OT_parts_placeholder_action,
    NYMPHSV2_PT_server,
    NYMPHSV2_PT_image_generation,
    NYMPHSV2_PT_shape,
    NYMPHSV2_PT_parts,
    NYMPHSV2_PT_texture,
)


def _safe_remove_scene_state() -> None:
    if hasattr(bpy.types.Scene, "nymphs3d2v2_state"):
        try:
            del bpy.types.Scene.nymphs3d2v2_state
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
    bpy.types.Scene.nymphs3d2v2_state = PointerProperty(type=NymphsV2State)
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
