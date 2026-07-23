"""The model should only be billed for transcript it hasn't already had explained to it.

Whatever chapters/fingerprint/repeats/dai already covered is omitted from the prompt. The
delicate part is that indices stay GLOBAL across the omission, so a span found after an elided
run still grounds to the right place in the full transcript.
"""
import pytest

from adscrub import db, detect

TX = [{"start": float(i), "end": float(i + 1), "text": f"segment number {i}"} for i in range(10)]


def test_covered_segments_are_omitted_from_the_prompt():
    body = "\n".join(detect._chunks(TX, skip=frozenset({2, 3, 4})))
    for i in (2, 3, 4):
        assert f"[{i}] " not in body
    for i in (0, 1, 5, 9):
        assert f"[{i}] " in body


def test_an_elision_marker_replaces_what_was_removed():
    """Without it, segments 1 and 5 read as adjacent and the model sees a seam that isn't there."""
    body = "\n".join(detect._chunks(TX, skip=frozenset({2, 3, 4})))
    assert "3 segment(s) already identified as ads, omitted" in body


def test_indices_stay_global_across_an_elision():
    """A span the model reports after the gap must still land on the right segment."""
    body = "\n".join(detect._chunks(TX, skip=frozenset({2, 3, 4})))
    assert "[5] 5.0-6.0: segment number 5" in body
    spans = detect.spans_from_segment_indices(TX, [{"start_segment": 5, "end_segment": 6,
                                                    "reason": "ad"}])
    assert spans[0].start_second == 5.0 and spans[0].end_second == 7.0


def test_no_coverage_renders_everything():
    assert detect._chunks(TX) == detect._chunks(TX, skip=frozenset())
    body = "\n".join(detect._chunks(TX))
    assert "omitted" not in body


def test_full_coverage_sends_nothing_but_the_marker():
    body = "\n".join(detect._chunks(TX, skip=frozenset(range(10))))
    assert "10 segment(s) already identified as ads, omitted" in body
    assert "segment number" not in body


def test_prompt_shrinks_with_coverage():
    full = len("\n".join(detect._chunks(TX)))
    half = len("\n".join(detect._chunks(TX, skip=frozenset(range(5)))))
    assert half < full


# --- coverage comes from ad_segments, any source ---


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    c.execute("INSERT INTO feeds (source_url) VALUES ('http://f')")
    c.execute("INSERT INTO episodes (feed_id, guid, title) VALUES (1, 'g', 'Ep')")
    c.commit()
    return c


def test_covered_indices_span_every_source(conn):
    for source, (s, e) in [("chapter", (0.0, 2.0)), ("fpmatch", (5.0, 6.0)), ("dai", (8.0, 9.0))]:
        conn.execute("INSERT INTO ad_segments (episode_id, start_second, end_second, source) "
                     "VALUES (1, ?, ?, ?)", (s, e, source))
    conn.commit()
    assert detect.covered_segment_indices(conn, 1, TX) == frozenset({0, 1, 5, 8})


def test_no_ad_segments_covers_nothing(conn):
    assert detect.covered_segment_indices(conn, 1, TX) == frozenset()


def test_layered_detector_passes_skip_through():
    seen = {}

    class Spy:
        def detect(self, transcript, skip=frozenset()):
            seen["skip"] = skip
            return []

    detect.LayeredDetector([Spy()]).detect(TX, frozenset({1, 2}))
    assert seen["skip"] == frozenset({1, 2})
