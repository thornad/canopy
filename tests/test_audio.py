"""Long recordings are transcribed in pieces and joined back up."""

import json
import shutil
import subprocess
import wave

import numpy as np
import pytest

from canopy import audio


def _tone_with_pauses(sr, seconds, pauses):
    """A loud tone with silent gaps at the given (start, end) second ranges."""
    t = np.arange(int(sr * seconds)) / sr
    samples = (np.sin(2 * np.pi * 440 * t) * 20000).astype(np.int16)
    for a, b in pauses:
        samples[int(a * sr):int(b * sr)] = 0
    return samples


def test_short_audio_is_one_piece():
    samples = np.zeros(100 * 10, dtype=np.int16)
    assert audio.plan_cuts(samples, 100, max_seconds=60) == [(0, 1000)]


def test_cuts_land_in_pauses_and_stay_under_the_limit():
    sr = 100
    samples = _tone_with_pauses(sr, 250, pauses=[(85, 87), (170, 172)])
    pieces = audio.plan_cuts(samples, sr, max_seconds=100, search_seconds=30, window_seconds=0.5)
    assert len(pieces) == 3
    assert pieces[0][0] == 0 and pieces[-1][1] == len(samples)
    for (_, end), (start, _) in zip(pieces, pieces[1:]):
        assert end == start  # nothing dropped or repeated
    for start, end in pieces:
        assert end - start <= 100 * sr
    assert 85 * sr <= pieces[0][1] <= 87 * sr
    assert 170 * sr <= pieces[1][1] <= 172 * sr


def test_vibevoice_segments_are_shifted_and_merged():
    first = json.dumps([{"Start": 0, "End": 1.5, "Speaker": 0, "Content": "こんにちは"}])
    second = json.dumps([{"Start": 0.25, "End": 2.0, "Speaker": 1, "Content": "Hi"}])
    merged = json.loads(audio.merge_transcripts([(0.0, first), (3300.5, second)]))
    assert merged == [
        {"Start": 0, "End": 1.5, "Speaker": 0, "Content": "こんにちは"},
        {"Start": 3300.75, "End": 3302.5, "Speaker": 1, "Content": "Hi"},
    ]


def test_plain_text_pieces_are_joined_in_order():
    assert audio.merge_transcripts([(0.0, "one "), (60.0, ""), (120.0, " two")]) == "one\n\ntwo"


def test_single_piece_is_returned_untouched():
    assert audio.merge_transcripts([(0.0, "  as is ")]) == "  as is "


def test_undecodable_file_is_sent_whole(tmp_path):
    src = tmp_path / "notes.m4a"
    src.write_bytes(b"not audio")
    assert audio.split_for_transcription(src, tmp_path) == [(0.0, src)]


@pytest.mark.skipif(shutil.which("afconvert") is None, reason="needs macOS afconvert")
def test_long_file_is_split_into_aac_pieces(tmp_path):
    sr = 8000
    wav = tmp_path / "in.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(_tone_with_pauses(sr, 25, pauses=[(9, 10), (18, 19)]).tobytes())
    src = tmp_path / "in.m4a"
    subprocess.run(["afconvert", "-f", "m4af", "-d", "aac", str(wav), str(src)], check=True)

    pieces = audio.split_for_transcription(src, tmp_path, max_seconds=10)

    assert len(pieces) == 3
    assert pieces[0][0] == 0.0
    assert 8.5 < pieces[1][0] < 10.5 and 17.5 < pieces[2][0] < 19.5
    durations = [audio.audio_duration(p) for _, p in pieces]
    assert all(d is not None and d <= 10.2 for d in durations)
    assert sum(durations) == pytest.approx(25, abs=0.5)
    assert not (tmp_path / "full.wav").exists()


@pytest.mark.skipif(shutil.which("afconvert") is None, reason="needs macOS afconvert")
def test_file_near_the_limit_that_fits_is_sent_whole(tmp_path):
    sr = 8000
    wav = tmp_path / "in.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(_tone_with_pauses(sr, 9, pauses=[]).tobytes())
    src = tmp_path / "in.m4a"
    subprocess.run(["afconvert", "-f", "m4af", "-d", "aac", str(wav), str(src)], check=True)

    # 9 s is past 80% of a 10 s limit, so it gets decoded, but it fits.
    assert audio.split_for_transcription(src, tmp_path, max_seconds=10) == [(0.0, src)]
    assert not (tmp_path / "full.wav").exists()


def test_only_vibevoice_has_a_hard_limit():
    assert audio.model_limit("VibeVoice-ASR-8bit") == 59 * 60
    assert audio.model_limit("whisper-large-v3-turbo-8bit") is None


def test_transcript_ending_at_the_limit_is_flagged():
    cut = json.dumps([{"Start": 3515.96, "End": 3540.0, "Content": "…"}])
    whole = json.dumps([{"Start": 1400.0, "End": 1479.05, "Content": "…"}])
    assert audio.cut_at_limit(cut, "VibeVoice-ASR-8bit")
    assert not audio.cut_at_limit(whole, "VibeVoice-ASR-8bit")
    assert not audio.cut_at_limit(cut, "whisper-large-v3-turbo-8bit")
    assert not audio.cut_at_limit("plain text", "VibeVoice-ASR-8bit")
