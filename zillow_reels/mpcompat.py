"""A thin shim over moviepy 1.x and 2.x.

moviepy renamed its imports (`moviepy.editor` -> `moviepy`) and its setters
(`set_duration` -> `with_duration`) in 2.0. Rather than pin a version and break
on whatever the user already has installed, we touch as little of moviepy as
possible — clip construction, audio, and encoding — and normalise it here.

Everything visual is rendered with Pillow/numpy, so none of moviepy's effects
API (the part that changed most) is used at all.
"""

from __future__ import annotations

from typing import Any, Callable


def _import_classes():
    try:  # moviepy 2.x
        from moviepy import AudioClip, AudioFileClip, VideoClip

        return VideoClip, AudioClip, AudioFileClip
    except ImportError:  # pragma: no cover - moviepy 1.x
        from moviepy.editor import AudioClip, AudioFileClip, VideoClip

        return VideoClip, AudioClip, AudioFileClip


try:
    VideoClip, AudioClip, AudioFileClip = _import_classes()
except ImportError as exc:  # pragma: no cover - surfaced as a clean CLI error
    raise ImportError(
        "moviepy is not installed. Run: pip install -r requirements.txt"
    ) from exc


def make_video_clip(frame_fn: Callable[[float], Any], duration: float) -> Any:
    """VideoClip(frame_function=...) on 2.x, VideoClip(make_frame=...) on 1.x."""
    try:
        return VideoClip(frame_function=frame_fn, duration=duration)
    except TypeError:
        return VideoClip(make_frame=frame_fn, duration=duration)


def make_audio_clip(frame_fn: Callable[[Any], Any], duration: float, fps: int = 44100) -> Any:
    try:
        return AudioClip(frame_function=frame_fn, duration=duration, fps=fps)
    except TypeError:
        return AudioClip(make_frame=frame_fn, duration=duration, fps=fps)


def with_audio(clip: Any, audio: Any) -> Any:
    setter = getattr(clip, "with_audio", None) or getattr(clip, "set_audio")
    return setter(audio)


def close(*clips: Any) -> None:
    for clip in clips:
        try:
            if clip is not None:
                clip.close()
        except Exception:  # noqa: BLE001 - closing must never mask a real error
            pass
