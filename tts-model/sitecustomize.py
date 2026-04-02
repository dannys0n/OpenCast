"""Local runtime patches for the repo-managed vLLM-Omni environment."""

from __future__ import annotations

try:
    from vllm_omni.entrypoints.openai.serving_speech import OmniOpenAIServingSpeech

    if not hasattr(OmniOpenAIServingSpeech, "_generate_pcm_chunks"):

        async def _generate_pcm_chunks(self, generator, request_id: str):
            async for chunk in self._generate_audio_chunks(generator, request_id, "pcm"):
                yield chunk

        OmniOpenAIServingSpeech._generate_pcm_chunks = _generate_pcm_chunks
except Exception:
    # Avoid breaking unrelated Python entrypoints if vllm-omni is not yet installed.
    pass
