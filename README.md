# NymphsCore Blender Extension

NymphsCore adds a Blender sidebar workflow for creating image references, generating a textured mesh, and retexturing the result when it needs another pass.

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

## Workflow

1. Generate image references in `Nymphs Image`.
2. Generate the mesh and first texture pass in `Nymphs Shape`.
3. Retexture the selected mesh in `Nymphs Texture` if the first texture needs another pass.
4. Open the output folders when you want to inspect saved images, meshes, or metadata.

Start with a single prompt image when the object is simple. Use a front, back, left, and right image set when the shape needs multiple views.

## Image Generation

The `Nymphs Image` panel is the first step. It creates the reference image or multiview set that drives the rest of the workflow.

Image backends:

- `Z-Image`, the default local backend running in the managed runtime
- `Gemini Flash`, using OpenRouter for Nano Banana / Gemini image models

For OpenRouter, paste an API key in the addon field or set `OPENROUTER_API_KEY` in the environment before Blender starts. Generated images are saved into the same output flow used by Z-Image, so they can be reused for shape generation or multiview sets.

Useful image tools:

- prompt and negative prompt fields
- saved prompt presets
- generation profiles for size, steps, seed, guidance, and variant count
- four-view multiview generation for front, back, left, and right references
- open and clear buttons for generated image folders

## Shape And Texture Generation

The `Nymphs Shape` panel turns the generated image references into a mesh and first texture result, then imports the returned model into Blender.

Single-image path:

1. Start `TRELLIS.2` in `Nymphs Runtimes`.
2. Generate or choose an image.
3. Run shape generation from the `Nymphs Shape` panel.
4. Adjust TRELLIS guidance presets when a prompt needs more or less image adherence.

Multiview path:

1. Create or choose front, back, left, and right reference images.
2. Start `Hunyuan 2mv` in `Nymphs Runtimes`.
3. Send the multiview set from the `Nymphs Shape` panel.
4. Use the imported mesh as the base model for cleanup or texturing.

`Hunyuan 2mv` is intended for cases where multiple aligned views describe the object better than one image can.

## Retexture

The `Nymphs Texture` panel is the optional cleanup pass after shape generation. Use it when the imported mesh is good but the texture needs a different reference image or another pass.

Typical flow:

1. Select a mesh in Blender.
2. Choose a texture reference image.
3. Start `TRELLIS.2` or `Hunyuan 2mv`.
4. Run the texture request and inspect the imported result.

## Runtime

NymphsCore is built around the managed `NymphsCore` WSL runtime and the `nymph` user created by the Windows Manager.

Use the `Nymphs Runtimes` panel to start, stop, and probe:

- `Z-Image` for local prompt-to-image generation
- `TRELLIS.2` for single-image shape, texture, and retexture work
- `Hunyuan 2mv` for multiview mesh generation from front, back, left, and right images

The retired Hunyuan Parts / P3-SAM / X-Part workflow is no longer included.

## Outputs

NymphsCore keeps generated images, meshes, and metadata in local output folders. Use the panel folder buttons to open or clear those folders while testing builds from this feed.
