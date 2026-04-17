# NymphsCore Blender Extension

NymphsCore adds a Blender sidebar workflow for generating reference images, turning images into 3D shapes, and retexturing selected meshes through the NymphsCore runtime.

## Install From This Feed

1. Open Blender 4.2 or newer.
2. Go to `Edit > Preferences > Extensions`.
3. Add this remote repository URL:

   ```text
   https://raw.githubusercontent.com/nymphnerds/NymphsExt/main/index.json
   ```

4. Install `NymphsCore`.
5. Open the 3D View sidebar and use the `Nymphs` tabs.

The current test package is `1.1.110`. The Blender extension id remains `nymphs3d2` so existing test installs can update from the same feed.

## Runtime

NymphsCore is built around the managed `NymphsCore` WSL runtime and the `nymph` user created by the Windows Manager.

Use the `Nymphs Runtimes` panel to start, stop, and probe:

- `TRELLIS.2` for image-to-3D shape and texture work
- `Hunyuan 2mv` for multiview mesh generation from front, back, and side images
- `Z-Image` for local prompt-to-image generation

The retired Hunyuan Parts / P3-SAM / X-Part workflow is no longer included.

## Image Generation

The `Nymphs Image` panel supports two image backends:

- `Z-Image`, the default local backend running in the managed runtime
- `Gemini Flash`, using OpenRouter for Nano Banana / Gemini image models

For OpenRouter, paste an API key in the addon field or set `OPENROUTER_API_KEY` in the environment before Blender starts. Generated images are saved into the same output flow used by Z-Image, so they can be reused for shape generation or multiview sets.

Useful image tools:

- prompt and negative prompt fields
- saved prompt presets
- generation profiles for size, steps, seed, guidance, and variant count
- four-view multiview generation for front, back, left, and right references
- open and clear buttons for generated image folders

## Shape Generation

The `Nymphs Shape` panel sends the selected source images to the chosen 3D backend and imports the returned mesh into Blender.

Recommended single-image path:

1. Start `TRELLIS.2` in `Nymphs Runtimes`.
2. Generate or choose an image.
3. Run shape generation from the `Nymphs Shape` panel.
4. Adjust TRELLIS guidance presets when a prompt needs more or less image adherence.

Recommended multiview path:

1. Create or choose front, back, left, and right reference images.
2. Start `Hunyuan 2mv` in `Nymphs Runtimes`.
3. Send the multiview set from the `Nymphs Shape` panel.
4. Use the imported mesh as the base model for cleanup or texturing.

`Hunyuan 2mv` is intended for cases where multiple aligned views describe the object better than one image can.

## Texture Generation

The `Nymphs Texture` panel retextures the selected mesh using an image prompt and the selected texture backend.

Typical flow:

1. Select a mesh in Blender.
2. Choose a texture reference image.
3. Start `TRELLIS.2` or `Hunyuan 2mv`.
4. Run the texture request and inspect the imported result.

## Outputs

NymphsCore keeps generated images, meshes, and metadata in local output folders. Use the panel folder buttons to open or clear those folders while testing builds from this feed.
