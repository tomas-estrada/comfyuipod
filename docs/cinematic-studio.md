# Cinematic Studio

Cinematic Studio adds scene direction controls to Pod Studio for creating a still image: shot type, camera/lens language, lighting, visual look, aspect ratio, and the main SDXL sampling settings. A live preview shows the exact composed prompt before submission.

## Local render path

The panel asks Pod's Studio API to compose and validate the prompt, then submits it as an asynchronous render using the existing `sdxl_image` template. That template runs through the configured local ComfyUI instance. Its normal job record, SSE progress, project history, and output persistence remain in use; completed images appear in the active project's gallery. No separate engine is registered, and this feature does not call Comfy Router or another hosted image API.

Prompt composition follows this order:

`shot type. scene. camera/lens. lighting. visual look`

Camera and lens choices are natural-language guidance in the prompt. They do not change focal length or physically simulate a camera. The default format follows the active project when available; the user can choose 16:9, 9:16, 1:1, or 4:5 for an individual render. Random seed is on by default; disabling it exposes a reproducible seed field.

The workflow uses the existing SDXL graph and core ComfyUI nodes (`CheckpointLoaderSimple`, `CLIPTextEncode`, `EmptyLatentImage`, `KSampler`, `VAEDecode`, and `SaveImage`). This change adds no custom-node pack and ships no model weights.

## Model terms

The intended use for this integration is **research and non-commercial use only**. The existing `sdxl_image` template references `Juggernaut-XI-byRunDiffusion.safetensors`. Its [Hugging Face model card](https://huggingface.co/RunDiffusion/Juggernaut-XI-v11) identifies the checkpoint as **CC BY-NC-ND 4.0**. The card describes attribution when sharing output, non-commercial use, no derivatives, and limits on paid or competing hosted deployments. This feature is local, but that does not remove the checkpoint's terms. This change does not ship model weights; Pod's existing model profile lists the checkpoint for download, subject to access and the model's license. Do not assume generated-image rights are unrestricted; review the linked terms before use.

The workflow uses only existing core nodes, so it introduces no third-party custom-node licensing dependency. Any replacement checkpoint or additional node pack must be assessed under its own license before use.

## Source attribution

The direction-choice wording and prompt composition order adapt the feature approach in [Comfy-Org/comfy-cinematic-studio](https://github.com/Comfy-Org/comfy-cinematic-studio), licensed under MIT. Its source notice is retained here:

> MIT License
>
> Copyright (c) 2026 Comfy Org
>
> Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
