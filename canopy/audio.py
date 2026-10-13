"""Split long audio for transcription and join the transcripts back up.

oMLX takes one file per request and has no "start at" option, and some ASR
models silently trim what they get (mlx-audio's VibeVoice keeps only the
first 59 minutes). So Canopy cuts long recordings into pieces itself, using
macOS's own ``afinfo`` / ``afconvert``, and merges the results.
"""

import json
import re
import subprocess
import wave
from pathlib import Path

import numpy as np

# Below VibeVoice's 59-minute trim, with room for a quiet cut point.
MAX_PIECE_SECONDS = 55 * 60
# How far before the limit to look for a pause to cut at.
CUT_SEARCH_SECONDS = 120
CUT_WINDOW_SECONDS = 0.5
# ASR models that silently drop audio past a fixed length, matched by a
# substring of the model id. mlx-audio's VibeVoice-ASR trims to 59 minutes;
# Whisper and GLM-ASR step through the whole file in 30 s windows.
MODEL_LIMITS = {"vibevoice": 59 * 60}
# A transcript ending this close to the model's limit was probably cut.
LIMIT_SLACK_SECONDS = 10
# Files estimated above this share of the limit get measured exactly.
ESTIMATE_MARGIN = 0.8
SAMPLE_RATE = 24000  # VibeVoice's native rate; other models resample anyway


def audio_duration(path: Path) -> float | None:
    """Duration in seconds per ``afinfo``, or None if macOS can't read it."""
    try:
        out = subprocess.run(
            ["afinfo", str(path)], capture_output=True, text=True, timeout=60,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"estimated duration:\s*([\d.]+)\s*sec", out)
    return float(m.group(1)) if m else None


def plan_cuts(samples: np.ndarray, sr: int, max_seconds: float = MAX_PIECE_SECONDS,
              search_seconds: float = CUT_SEARCH_SECONDS,
              window_seconds: float = CUT_WINDOW_SECONDS) -> list[tuple[int, int]]:
    """Return ``(start, end)`` sample ranges, each at most ``max_seconds`` long.

    Each cut lands on the quietest ``window_seconds`` in the ``search_seconds``
    before the limit, so a word is not split between two pieces.
    """
    total = len(samples)
    max_len = int(max_seconds * sr)
    window = max(1, int(window_seconds * sr))
    pieces = []
    start = 0
    while total - start > max_len:
        hi = start + max_len
        lo = max(start + window, hi - int(search_seconds * sr))
        region = samples[lo:hi].astype(np.float32)
        n = len(region) // window
        if n == 0:
            cut = hi
        else:
            energy = (region[: n * window].reshape(n, window) ** 2).mean(axis=1)
            quietest = int(energy.argmin())
            cut = lo + quietest * window + window // 2
        pieces.append((start, cut))
        start = cut
    pieces.append((start, total))
    return pieces


def _read_pcm16(path: Path) -> np.ndarray:
    """Samples from the ``data`` chunk of a 16-bit mono WAV.

    ``afconvert`` writes WAVE_FORMAT_EXTENSIBLE headers, which Python 3.11's
    ``wave`` module rejects, so walk the RIFF chunks directly.
    """
    raw = path.read_bytes()
    pos = 12  # past "RIFF", size, "WAVE"
    while pos + 8 <= len(raw):
        chunk_id = raw[pos:pos + 4]
        size = int.from_bytes(raw[pos + 4:pos + 8], "little")
        if chunk_id == b"data":
            return np.frombuffer(raw, dtype="<i2", count=size // 2, offset=pos + 8)
        pos += 8 + size + (size & 1)
    raise ValueError(f"no data chunk in {path}")


def split_for_transcription(src: Path, workdir: Path,
                            max_seconds: float = MAX_PIECE_SECONDS) -> list[tuple[float, Path]]:
    """Cut ``src`` into pieces no longer than ``max_seconds``.

    Returns ``(offset_seconds, path)`` per piece. A file that fits, or that
    macOS can't decode, comes back whole as ``[(0.0, src)]`` so it is sent
    exactly as before.
    """
    duration = audio_duration(src)
    # afinfo only estimates some formats (raw ADTS .aac read 15% short), so
    # anything near the limit is decoded and measured before deciding.
    if duration is None or duration <= max_seconds * ESTIMATE_MARGIN:
        return [(0.0, src)]

    pcm = workdir / "full.wav"
    try:
        subprocess.run(
            ["afconvert", "-f", "WAVE", "-d", f"LEI16@{SAMPLE_RATE}", "-c", "1",
             str(src), str(pcm)],
            check=True, capture_output=True, timeout=1800,
        )
    except (OSError, subprocess.SubprocessError):
        return [(0.0, src)]

    sr = SAMPLE_RATE
    samples = _read_pcm16(pcm)
    cuts = plan_cuts(samples, sr, max_seconds)
    if len(cuts) == 1:
        pcm.unlink()
        return [(0.0, src)]

    pieces = []
    for i, (start, end) in enumerate(cuts):
        part_wav = workdir / f"part{i}.wav"
        with wave.open(str(part_wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(samples[start:end].tobytes())
        # AAC keeps each upload to tens of MB instead of ~160 MB of PCM.
        part = workdir / f"part{i}.m4a"
        subprocess.run(
            ["afconvert", "-f", "m4af", "-d", "aac", str(part_wav), str(part)],
            check=True, capture_output=True, timeout=1800,
        )
        part_wav.unlink()
        pieces.append((start / sr, part))
    pcm.unlink()
    return pieces


def _segments(text: str) -> list[dict] | None:
    """Parse a VibeVoice-style JSON segment list, or None for plain text."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(data, list) and all(isinstance(s, dict) and "Start" in s for s in data):
        return data
    return None


def merge_transcripts(parts: list[tuple[float, str]]) -> str:
    """Join per-piece transcripts in order.

    JSON segment lists (VibeVoice) are merged into one list with
    ``Start``/``End`` shifted to the position in the full recording. Plain
    text (Whisper and others) is joined with blank lines.
    """
    if len(parts) == 1:
        return parts[0][1]
    parsed = [_segments(text) for _, text in parts]
    if all(p is not None for p in parsed):
        merged = []
        for (offset, _), segs in zip(parts, parsed):
            for s in segs:
                s = dict(s)
                for key in ("Start", "End"):
                    if isinstance(s.get(key), (int, float)):
                        s[key] = round(s[key] + offset, 2)
                merged.append(s)
        return json.dumps(merged, ensure_ascii=False, separators=(",", ":"))
    return "\n\n".join(text.strip() for _, text in parts if text.strip())


def model_limit(model: str) -> float | None:
    """Longest audio ``model`` transcribes before silently dropping the rest."""
    name = model.lower()
    return next((secs for key, secs in MODEL_LIMITS.items() if key in name), None)


def cut_at_limit(text: str, model: str) -> bool:
    """True when a transcript's last segment ends at the model's limit.

    Canopy splits what it can decode, but some files (webm) still go up
    whole; this catches the ones the model trimmed.
    """
    limit = model_limit(model)
    segments = _segments(text)
    if limit is None or not segments:
        return False
    end = segments[-1].get("End")
    return isinstance(end, (int, float)) and end >= limit - LIMIT_SLACK_SECONDS
