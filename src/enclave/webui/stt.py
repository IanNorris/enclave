"""Local speech-to-text for voice dictation, backed by faster-whisper.

Runs entirely on the host — no audio leaves the box, no per-use cost. The
browser records audio (webm/opus via MediaRecorder) and posts the bytes to the
``/transcribe`` route, which calls :func:`transcribe` here. faster-whisper
bundles PyAV (ffmpeg libraries), so it decodes webm/opus directly — no system
ffmpeg dependency.

NixOS note: the faster-whisper / PyAV manylinux wheels dynamically link a couple
of base C libraries (``libz``, ``libstdc++``) that NixOS does not expose at the
standard FHS paths. We preload those from the nix store — resolved dynamically
with a glob at runtime, so there are no hard-coded store hashes to break on a
nixpkgs update — before importing faster-whisper. On a normal FHS distro the
glob finds nothing and we simply import faster-whisper directly.

The model is loaded once, lazily, on the first request and cached in memory.
Transcription is CPU-bound and blocking; callers must run :func:`transcribe`
off the event loop (e.g. ``await asyncio.to_thread(...)``).
"""

from __future__ import annotations

import glob
import os
import tempfile
import threading

from enclave.common.logging import get_logger

log = get_logger("stt")

# Configuration (env-overridable).
_MODEL_NAME = os.environ.get("ENCLAVE_STT_MODEL", "small.en")
_DEVICE = os.environ.get("ENCLAVE_STT_DEVICE", "cpu")
_COMPUTE_TYPE = os.environ.get("ENCLAVE_STT_COMPUTE", "int8")

_model = None
_model_lock = threading.Lock()
_libs_preloaded = False


def _preload_native_libs() -> None:
    """Preload libz / libstdc++ from the nix store so the PyAV wheel can load.

    No-op on FHS distros (glob finds nothing) and harmless if already loaded.
    Resolved dynamically at runtime — no pinned store paths.
    """
    global _libs_preloaded
    if _libs_preloaded:
        return
    import ctypes

    patterns = (
        "/nix/store/*gcc-*-lib/lib/libstdc++.so.6",
        "/nix/store/*zlib-*/lib/libz.so.1",
    )
    for pat in patterns:
        hits = sorted(glob.glob(pat))
        if hits:
            try:
                ctypes.CDLL(hits[-1], mode=ctypes.RTLD_GLOBAL)
            except OSError as e:
                log.warning("Failed to preload %s: %s", hits[-1], e)
    _libs_preloaded = True


def _get_model():
    """Load (once) and return the faster-whisper model. Thread-safe."""
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        _preload_native_libs()
        from faster_whisper import WhisperModel

        log.info(
            "Loading STT model %s (device=%s, compute=%s)…",
            _MODEL_NAME, _DEVICE, _COMPUTE_TYPE,
        )
        _model = WhisperModel(
            _MODEL_NAME, device=_DEVICE, compute_type=_COMPUTE_TYPE,
        )
        log.info("STT model loaded")
        return _model


def transcribe(audio_bytes: bytes, suffix: str = ".webm") -> str:
    """Transcribe recorded audio bytes to text. Blocking (run in a thread).

    Args:
        audio_bytes: The raw recorded audio (e.g. webm/opus from MediaRecorder).
        suffix: File suffix hinting the container format (for the temp file).

    Returns:
        The transcribed text (may be empty for silence).
    """
    model = _get_model()
    # faster-whisper decodes from a path via PyAV; write to a temp file.
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
            tf.write(audio_bytes)
            tmp_path = tf.name
        segments, _info = model.transcribe(tmp_path, beam_size=1)
        return " ".join(seg.text for seg in segments).strip()
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def warm_up() -> None:
    """Eagerly load the model (optional; otherwise it loads on first request)."""
    _get_model()
