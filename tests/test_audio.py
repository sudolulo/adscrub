import subprocess

import httpx
import pytest

from adscrub import audio


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_download_audio_writes_within_cap(tmp_path):
    body = b"x" * 2048
    client = _client(lambda req: httpx.Response(200, content=body))
    dest = tmp_path / "ep.mp3"
    out = audio.download_audio(client, "http://feed/ep.mp3", dest, max_bytes=4096)
    assert out == dest
    assert dest.read_bytes() == body


def test_download_audio_rejects_oversized_content_length(tmp_path):
    # Server declares a huge body up front -> abort before writing anything.
    def handler(req):
        return httpx.Response(200, headers={"content-length": "999999999"}, content=b"x")

    dest = tmp_path / "ep.mp3"
    with pytest.raises(ValueError, match="over the"):
        audio.download_audio(_client(handler), "http://feed/ep.mp3", dest, max_bytes=1024)
    assert not dest.exists()
    assert not dest.with_suffix(dest.suffix + ".part").exists()


def test_download_audio_aborts_when_stream_exceeds_cap(tmp_path):
    # Chunked body with no Content-Length: the running byte cap must still fire,
    # and the partial file must be cleaned up.
    def handler(req):
        def chunks():
            for _ in range(16):
                yield b"y" * 512

        return httpx.Response(200, content=chunks())

    dest = tmp_path / "ep.mp3"
    with pytest.raises(ValueError, match="mid-stream"):
        audio.download_audio(_client(handler), "http://feed/ep.mp3", dest, max_bytes=1024)
    assert not dest.exists()
    assert not dest.with_suffix(dest.suffix + ".part").exists()


def test_probe_duration_parses_ffprobe_output(monkeypatch):
    def fake_run(cmd, **kwargs):
        assert cmd[0] == "ffprobe"
        assert cmd[-1] == "/tmp/fake.mp3"
        return subprocess.CompletedProcess(cmd, 0, stdout="123.456\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert audio.probe_duration("/tmp/fake.mp3") == 123.456
