"""Assemble the 1080x1920 MP4.

The whole video is a single moviepy VideoClip driven by one frame function.
Segments (title card, each photo, outro card) sit on a timeline that overlaps
by the crossfade duration; the frame function finds which one or two segments
are live at time t, renders them, and blends. Ken Burns motion is a crop-and-
resize in Pillow.

Doing it this way means moviepy is only responsible for encoding and audio —
none of the effects API that was rewritten between 1.x and 2.x is touched, so
the same code runs on either version.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from . import mpcompat
from .cards import fit_cover, open_photo, render_caption_overlay, render_outro_card, render_title_card
from .config import Config
from .models import Listing, Photo


class Segment:
    """One stretch of the timeline. `duration` includes its crossfade tails."""

    duration: float = 0.0

    def frame(self, local_t: float) -> np.ndarray:  # pragma: no cover - interface
        raise NotImplementedError


@dataclass
class StillSegment(Segment):
    """A pre-rendered card. Held as an array so no work happens per frame."""

    array: np.ndarray
    duration: float

    def frame(self, local_t: float) -> np.ndarray:
        return self.array


class PhotoSegment(Segment):
    """A photo with a slow pan/zoom and an optional caption bar."""

    def __init__(self, path: Path, cfg: Config, index: int, duration: float, caption: str = ""):
        self.duration = duration
        self.cfg = cfg
        self.size = cfg.size
        self.zoom = max(1.0, cfg.zoom)

        # Oversample once so every frame is a crop of the same source.
        big = (int(cfg.width * self.zoom), int(cfg.height * self.zoom))
        self.base = fit_cover(open_photo(path), big)
        # Derive the real zoom range from the base we actually got: rounding to
        # whole pixels above can leave it a fraction short of width * zoom, and
        # a crop box even slightly outside the source is a hard error in Pillow.
        self.max_k = min(self.base.width / cfg.width, self.base.height / cfg.height)

        # Alternate zoom direction and pan axis so a long slideshow doesn't
        # feel mechanical.
        self.zoom_in = index % 2 == 0
        self.pan_x = 1 if index % 4 in (0, 3) else -1
        self.pan_y = 1 if index % 3 == 0 else -1

        overlay = render_caption_overlay(caption, cfg) if cfg.captions != "off" else None
        if overlay is not None:
            layer = np.asarray(overlay).astype(np.float32)
            self.caption_rgb = layer[..., :3]
            self.caption_alpha = (layer[..., 3:4]) / 255.0
        else:
            self.caption_rgb = None
            self.caption_alpha = None

    def frame(self, local_t: float) -> np.ndarray:
        width, height = self.size
        progress = 0.0 if self.duration <= 0 else min(max(local_t / self.duration, 0.0), 1.0)
        eased = progress * progress * (3 - 2 * progress)  # smoothstep

        # k is the crop size as a multiple of the output; 1.0 is fully zoomed in.
        span = self.max_k - 1.0
        k = (self.max_k - span * eased) if self.zoom_in else (1.0 + span * eased)
        crop_w = min(width * k, self.base.width)
        crop_h = min(height * k, self.base.height)

        # Pan across whatever slack the crop leaves inside the oversampled base.
        slack_x = max(self.base.width - crop_w, 0)
        slack_y = max(self.base.height - crop_h, 0)
        travel_x = eased if self.pan_x > 0 else 1.0 - eased
        travel_y = eased if self.pan_y > 0 else 1.0 - eased
        left = slack_x * (0.5 + (travel_x - 0.5) * 0.9)
        top = slack_y * (0.5 + (travel_y - 0.5) * 0.9)

        box = (left, top, min(left + crop_w, self.base.width), min(top + crop_h, self.base.height))
        frame = self.base.resize((width, height), Image.BICUBIC, box=box)
        array = np.asarray(frame, dtype=np.uint8)

        if self.caption_rgb is not None:
            blended = array * (1.0 - self.caption_alpha) + self.caption_rgb * self.caption_alpha
            array = blended.astype(np.uint8)
        return array


class Timeline:
    """Segments laid end to end, overlapping by `crossfade`."""

    def __init__(self, segments: list[Segment], crossfade: float, size: tuple[int, int]):
        self.segments = segments
        self.size = size
        # Never let a crossfade eat more than a third of its shortest neighbour.
        shortest = min((s.duration for s in segments), default=1.0)
        self.crossfade = max(0.0, min(crossfade, shortest / 3))

        self.starts: list[float] = []
        cursor = 0.0
        for segment in segments:
            self.starts.append(cursor)
            cursor += segment.duration - self.crossfade
        self.duration = max(cursor + self.crossfade, 0.1)

    def frame(self, t: float) -> np.ndarray:
        active: list[tuple[Segment, float]] = []
        for segment, start in zip(self.segments, self.starts):
            local = t - start
            if -1e-6 <= local < segment.duration:
                active.append((segment, local))

        if not active:
            # Past the end (moviepy can ask for exactly `duration`): hold the
            # last frame rather than emitting a black flash.
            last = self.segments[-1]
            return last.frame(last.duration)

        if len(active) == 1 or self.crossfade <= 0:
            segment, local = active[0]
            return segment.frame(local)

        outgoing, out_local = active[0]
        incoming, in_local = active[1]
        alpha = min(max(in_local / self.crossfade, 0.0), 1.0)
        a = outgoing.frame(out_local).astype(np.float32)
        b = incoming.frame(in_local).astype(np.float32)
        return (a * (1.0 - alpha) + b * alpha).astype(np.uint8)


AUDIO_FPS = 44100
AUDIO_CHUNK = 4096  # samples per reader request; see _build_audio


def _build_audio(cfg: Config, duration: float):
    """Background music: looped to length, faded in and out, level-matched.

    The track is decoded once into a numpy buffer and then served by index.
    Reading through AudioFileClip.get_frame per chunk means handing moviepy's
    ffmpeg reader non-monotonic times once the loop wraps, which it does not
    handle; a flat buffer is both faster and version-proof.
    """
    if not cfg.music_path:
        return None
    path = Path(cfg.music_path).expanduser()
    if not path.exists():
        print(f"    ! music file not found, continuing without audio: {path}")
        return None

    source = mpcompat.AudioFileClip(str(path))
    try:
        # The small buffersize is load-bearing. to_soundarray only takes its
        # chunked path when duration > buffersize/fps; otherwise it hands the
        # whole time array to the ffmpeg reader in one call, and that reader
        # mis-recurses (it passes its boolean mask where it means the time
        # slice) for any request over ~25k samples. Keeping every request small
        # stays on the safe path regardless of track length.
        buffer = np.asarray(
            source.to_soundarray(fps=AUDIO_FPS, buffersize=AUDIO_CHUNK), dtype=np.float32
        )
    except Exception as exc:  # noqa: BLE001 - bad audio must not lose the video
        print(f"    ! could not decode music, continuing without audio: {exc}")
        return None
    finally:
        mpcompat.close(source)

    if buffer.ndim == 1:
        buffer = buffer[:, None]
    if buffer.shape[0] == 0:
        return None

    total = int(duration * AUDIO_FPS) + 1
    data = buffer[np.arange(total) % buffer.shape[0]] * cfg.music_volume
    del buffer

    fade = max(0.0, min(cfg.music_fade_seconds, duration / 2))
    if fade > 0:
        times = np.arange(total, dtype=np.float32) / AUDIO_FPS
        envelope = np.minimum(np.clip(times / fade, 0, 1), np.clip((duration - times) / fade, 0, 1))
        data *= envelope[:, None]

    channels = data.shape[1]

    def frame(t):
        scalar = np.ndim(t) == 0
        indices = np.clip((np.atleast_1d(np.asarray(t)) * AUDIO_FPS).astype(np.int64), 0, total - 1)
        samples = data[indices]
        return samples[0] if scalar else samples

    clip = mpcompat.make_audio_clip(frame, duration=duration, fps=AUDIO_FPS)
    # moviepy infers channel count by probing the frame function; setting it
    # explicitly avoids a mono/stereo mismatch at encode time.
    clip.nchannels = channels
    return clip, None


def build_video(
    listing: Listing,
    photos: list[Photo],
    out_path: str | Path,
    cfg: Config,
    *,
    verbose: bool = True,
) -> Path:
    """Render the listing to an MP4 and return its path."""
    out_path = Path(out_path).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    usable = [p for p in photos if p.path and Path(p.path).exists()]
    if not usable:
        raise ValueError("no usable photos — cannot render a slideshow")

    hero = open_photo(usable[0].path)  # type: ignore[arg-type]

    if verbose:
        print("    rendering cards…")
    segments: list[Segment] = [
        StillSegment(np.asarray(render_title_card(listing, cfg, hero), dtype=np.uint8), cfg.title_seconds),
    ]

    if verbose:
        print(f"    preparing {len(usable)} photo segment(s)…")
    for index, photo in enumerate(usable):
        segments.append(
            PhotoSegment(
                Path(photo.path),  # type: ignore[arg-type]
                cfg,
                index=index,
                duration=cfg.photo_seconds,
                caption=photo.caption,
            )
        )

    segments.append(
        StillSegment(np.asarray(render_outro_card(listing, cfg, hero), dtype=np.uint8), cfg.outro_seconds)
    )

    timeline = Timeline(segments, cfg.crossfade_seconds, cfg.size)
    clip = mpcompat.make_video_clip(timeline.frame, timeline.duration)

    built = _build_audio(cfg, timeline.duration)
    has_audio = built is not None
    if built:
        audio_clip, _ = built
        clip = mpcompat.with_audio(clip, audio_clip)

    if verbose:
        print(f"    encoding {timeline.duration:.1f}s at {cfg.width}x{cfg.height}…")

    clip.write_videofile(
        str(out_path),
        fps=cfg.fps,
        codec=cfg.video_codec,
        audio_codec="aac" if has_audio else None,
        bitrate=cfg.video_bitrate,
        preset=cfg.video_preset,
        threads=os.cpu_count() or 4,
        # yuv420p is what phones and every platform's transcoder expect;
        # +faststart puts the index first so previews start instantly.
        ffmpeg_params=["-pix_fmt", "yuv420p", "-movflags", "+faststart"],
        logger="bar" if verbose else None,
    )

    mpcompat.close(clip)
    return out_path
