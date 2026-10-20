"""Tests for homelab_maint/netwatch/plex.py: exploit-attempt lines, codec downloads checked against their log lines, and the log reader."""
import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint.core is imported)

import os
import time

from homelab_maint.netwatch.plex import exploit_attempts, plex_log_lines, unexplained_codecs

EXPLOIT = (
    "Oct 17, 2026 15:17:04.668 [130939384544056] WARN - [Req#176eb82/CompanionProxy/Subscribe/poc_23919] "
    "wanted to subscribe with an unsupported protocol "
    "'http://127.0.0.1:32400/system/agents/z/searchOne?mediaType=1&lang=en&identifier=PLACEHOLDER'"
)
TIMELINE = (
    "Oct 17, 2026 15:18:00.001 [130939384544099] DEBUG - [Req#2b3c4d/Plex] GET "
    "/player/timeline/poll?protocol=http&token=SECRET-TOKEN-VALUE"
)
CODEC_OTHER = "Oct 19, 2026 04:27:02.353 [130939096709944] INFO - [VoiceActivityDetectionManager/TPU] CodecManager: obtaining decoder 'aac'"


def _plex_stamp(epoch: float) -> str:
    """A Plex log timestamp (local time, millisecond precision) for an epoch."""
    t = time.localtime(epoch)
    return time.strftime("%b %d, %Y %H:%M:%S", t) + f".{int((epoch % 1) * 1000):03d}"


def _codec_line(epoch: float, name: str, kind: str = "decoder") -> str:
    return (
        f"{_plex_stamp(epoch)} [130939096709944] INFO - [VoiceActivityDetectionManager/TPU] "
        f"CodecManager: obtaining {kind} '{name}'"
    )


def test_exploit_line_gives_one_plex_exploit_with_request_id_only():
    (ev,) = exploit_attempts([EXPLOIT])
    assert ev.kind == "plex_exploit"
    assert (ev.src, ev.dst, ev.dport) == ("unknown", "plex", 32400)
    assert ev.detail == "Req#176eb82"
    assert "searchOne" not in ev.detail and "PLACEHOLDER" not in ev.detail


def test_timeline_line_is_matched_and_token_never_leaks():
    (ev,) = exploit_attempts([TIMELINE])
    assert ev.kind == "plex_exploit"
    assert ev.detail == "Req#2b3c4d"
    assert "SECRET-TOKEN-VALUE" not in ev.detail


def test_unrelated_lines_are_ignored():
    assert exploit_attempts([CODEC_OTHER, "", "garbage"]) == []


def test_codec_written_shortly_after_its_log_line_is_explained(tmp_path):
    codec_dir = tmp_path / "codecs"
    (codec_dir / "1.2.3").mkdir(parents=True)
    so = codec_dir / "1.2.3" / "libaac_latm_decoder.so"
    so.write_bytes(b"x")
    logged = 1798761600.25
    os.utime(so, (logged + 0.4, logged + 0.4))
    lines = [_codec_line(logged, "aac_latm")]
    assert unexplained_codecs(codec_dir, lines, since=logged - 10) == []


def test_codec_with_no_log_line_is_reported(tmp_path):
    codec_dir = tmp_path / "codecs"
    (codec_dir / "1.2.3").mkdir(parents=True)
    so = codec_dir / "1.2.3" / "libaac_latm_decoder.so"
    so.write_bytes(b"x")
    mtime = 1798761600.0
    os.utime(so, (mtime, mtime))
    (ev,) = unexplained_codecs(codec_dir, [], since=mtime - 10)
    assert ev.kind == "codec_unexplained"
    assert ev.ts == mtime
    assert ev.detail == "libaac_latm_decoder.so"


def test_codec_whose_log_line_names_a_different_decoder_is_reported(tmp_path):
    codec_dir = tmp_path / "codecs"
    (codec_dir / "1.2.3").mkdir(parents=True)
    so = codec_dir / "1.2.3" / "libaac_latm_decoder.so"
    so.write_bytes(b"x")
    logged = 1798761600.0
    os.utime(so, (logged + 0.4, logged + 0.4))
    (ev,) = unexplained_codecs(codec_dir, [_codec_line(logged, "aac")], since=logged - 10)
    assert ev.detail == "libaac_latm_decoder.so"


def test_codec_older_than_since_is_ignored(tmp_path):
    codec_dir = tmp_path / "codecs"
    (codec_dir / "1.2.3").mkdir(parents=True)
    so = codec_dir / "1.2.3" / "libaac_latm_decoder.so"
    so.write_bytes(b"x")
    os.utime(so, (1798761600.0, 1798761600.0))
    assert unexplained_codecs(codec_dir, [], since=1798761700.0) == []


def test_missing_codec_dir_gives_nothing(tmp_path):
    assert unexplained_codecs(tmp_path / "absent", [], since=0.0) == []


def test_plex_log_lines_keeps_lines_at_or_after_since(tmp_path):
    plex_dir = tmp_path / "Plex Media Server"
    (plex_dir / "Logs").mkdir(parents=True)
    early = _plex_stamp(1798761600.0) + " [1] INFO - early line"
    late = _plex_stamp(1798761700.0) + " [1] INFO - late line"
    (plex_dir / "Logs" / "Plex Media Server.log").write_text(early + "\n" + late + "\n")
    got = plex_log_lines(1798761650.0, cfg={"plex_dir": str(plex_dir)})
    assert got == [late]


def test_plex_log_missing_file_returns_nothing(tmp_path):
    assert plex_log_lines(0.0, cfg={"plex_dir": str(tmp_path / "nope")}) == []
