import httpx

from adscrub import dai

URL = "https://example.com/audio/ep1.mp3"


def client_returning(by_user_agent: dict[str, bytes]) -> httpx.Client:
    def handler(request):
        ua = request.headers.get("user-agent", "")
        return httpx.Response(200, content=by_user_agent[ua])

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_identical_responses_report_no_divergence():
    body = b"same audio bytes " * 100
    with client_returning({dai.USER_AGENTS[0]: body, dai.USER_AGENTS[1]: body}) as client:
        result = dai.probe_variance(client, URL, max_bytes=10_000)
    assert result.diverged is False
    assert result.divergence_byte is None
    assert result.bytes_compared == len(body)


def test_divergent_responses_report_the_first_differing_byte():
    prefix = b"intro audio " * 50  # 600 bytes, identical in both
    a = prefix + b"AAAA ad content here for stream a"
    b = prefix + b"BBBB completely different ad content"
    with client_returning({dai.USER_AGENTS[0]: a, dai.USER_AGENTS[1]: b}) as client:
        result = dai.probe_variance(client, URL, max_bytes=10_000)
    assert result.diverged is True
    assert result.divergence_byte == len(prefix)


def test_reconvergence_found_via_content_anchor_not_position():
    prefix = b"intro " * 50
    ad_a = b"XAD " * 30  # different length ad in each stream, diverges at byte 0
    ad_b = b"YAD-LONGER " * 40
    suffix = b"editorial content resumes here " * 200
    a = prefix + ad_a + suffix
    b = prefix + ad_b + suffix
    with client_returning({dai.USER_AGENTS[0]: a, dai.USER_AGENTS[1]: b}) as client:
        result = dai.probe_variance(
            client, URL, max_bytes=len(a) + 1000, anchor_skip=150, anchor_size=64
        )
    assert result.diverged is True
    assert result.divergence_byte == len(prefix)
    # Different ad lengths mean the suffix lands at different absolute offsets
    # in each stream — reconvergence must still be found via content, not position.
    assert result.reconverged is True
    assert b"editorial content resumes" in b[result.reconvergence_byte : result.reconvergence_byte + 100]


def test_no_reconvergence_when_divergence_never_ends_within_window():
    prefix = b"intro " * 50
    a = prefix + b"A" * 5000
    b = prefix + b"B" * 5000
    with client_returning({dai.USER_AGENTS[0]: a, dai.USER_AGENTS[1]: b}) as client:
        result = dai.probe_variance(client, URL, max_bytes=10_000)
    assert result.diverged is True
    assert result.reconverged is False
    assert result.reconvergence_byte is None


def test_short_window_with_no_room_for_an_anchor_reports_no_reconvergence():
    """Divergence near the end of the fetched window leaves no room for a full
    ANCHOR_SIZE anchor — must report "not found", not crash on a short slice."""
    a = b"same " * 10 + b"A" * 100
    b = b"same " * 10 + b"B" * 100
    with client_returning({dai.USER_AGENTS[0]: a, dai.USER_AGENTS[1]: b}) as client:
        result = dai.probe_variance(client, URL, max_bytes=10_000)
    assert result.diverged is True
    assert result.reconverged is False
