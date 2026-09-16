import copy

import pytest
from music21 import pitch

from chantey.comparepage import build_page, find_audio

from .conftest import build_score, write_musicxml


@pytest.fixture
def pair(tmp_path):
    a = build_score()
    b = copy.deepcopy(a)
    b.parts[0].measure(3).notes[0].pitch = pitch.Pitch("A4")
    pa, pb = tmp_path / "orig.musicxml", tmp_path / "rev.musicxml"
    write_musicxml(a, pa)
    write_musicxml(b, pb)
    return pa, pb


def test_every_measure_numbered(pair):
    html = build_page(*pair, changed=[3], audio=[], title="T")
    # 4 measures x 2 renderings; verovio marks them class="mNum ..."
    assert html.count('class="mNum') == 8


def test_changed_measures_highlighted(pair):
    html = build_page(*pair, changed=[3], audio=[], title="T")
    assert 'g.measure[data-n="3"]' in html
    # attribute present in the SVGs too, not just our stylesheet
    assert 'data-n="3"' in html.split("</style>", 1)[1]
    assert "crimson" in html


def test_page_is_self_contained(pair):
    html = build_page(*pair, changed=[3], audio=[("Original — piano", "orig.piano.wav")], title="T")
    assert "<script" not in html
    assert 'src="http' not in html and 'href="http' not in html
    assert '<audio controls src="orig.piano.wav">' in html


def test_find_audio_lists_only_existing(tmp_path):
    score = tmp_path / "tune.musicxml"
    score.write_text("x")
    (tmp_path / "tune.piano.wav").write_bytes(b"")
    found = find_audio(score, "Original")
    assert found == [("Original — piano", "tune.piano.wav")]
