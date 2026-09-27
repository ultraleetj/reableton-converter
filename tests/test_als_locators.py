"""REAPER project markers -> Ableton Arrangement Locators. See PLAN.md
"Markers/locators" -- Locator element schema confirmed against a real
Ableton reference file (Chuck Sutton "Patience" factory demo, the only
one checked so far with actual locators set instead of the empty
self-closing `<Locators />` every other reference file/our own
templates have).

Requires Ableton Live 12 installed at the default templates path for the
build_liveset_xml tests; skips if not present.
"""

import re

from rpp_to_als.als_builder import TrackSpec, build_liveset_xml, build_locators_xml
from rpp_to_als.rpp_parse import project_markers

from conftest import requires_ableton


def test_project_markers_parses_position_and_name():
    rpp = """
  MARKER 0 0 "Intro" 0 0 1 R {AAAA} 0 0
  MARKER 1 32.5 "Verse 1" 0 0 1 R {BBBB} 0 0
  MARKER 2 129.34131736526948 "" 0 0 1 R {CCCC} 0 0
"""
    markers = project_markers(rpp)
    assert markers == [
        (0.0, "Intro"),
        (32.5, "Verse 1"),
        (129.34131736526948, ""),
    ]


def test_project_markers_parses_unquoted_and_alt_quoted_names():
    # Real REAPER output: names without spaces are written bare; names
    # containing `"` fall back to ' or ` quoting.
    rpp = """
  MARKER 1 0 intro 0 0 1 R {5FBAC6DD} 0 2
  MARKER 2 8.00000000002 verse 0 0 1 R {D4D380C4} 0 2
  MARKER 3 16 'say "hi"' 0 0 1 R {B267A50F} 0 2
  MARKER 4 20 `it's "x"` 0 0 1 R {C267A50F} 0 2
"""
    assert project_markers(rpp) == [
        (0.0, "intro"),
        (8.00000000002, "verse"),
        (16.0, 'say "hi"'),
        (20.0, 'it\'s "x"'),
    ]


def test_project_markers_sorted_by_position_regardless_of_file_order():
    rpp = """
  MARKER 1 50.0 "Second" 0 0 1 R {BBBB} 0 0
  MARKER 0 10.0 "First" 0 0 1 R {AAAA} 0 0
"""
    assert project_markers(rpp) == [(10.0, "First"), (50.0, "Second")]


def test_project_markers_empty_when_none_present():
    assert project_markers("some unrelated project text\n") == []


def test_build_locators_xml_shape_matches_real_reference():
    xml = build_locators_xml([(0.0, "Intro"), (32.0, "Verse 1")])
    assert xml == (
        "<Locators><Locators>"
        '<Locator Id="0"><LomId Value="0" /><Time Value="0" /><Name Value="Intro" />'
        '<Annotation Value="" /><IsSongStart Value="false" /></Locator>'
        '<Locator Id="1"><LomId Value="0" /><Time Value="32" /><Name Value="Verse 1" />'
        '<Annotation Value="" /><IsSongStart Value="false" /></Locator>'
        "</Locators></Locators>"
    )


def test_build_locators_xml_escapes_name():
    xml = build_locators_xml([(0.0, 'A & B <"quoted">')])
    assert '<Name Value="A &amp; B &lt;&quot;quoted&quot;&gt;" />' in xml


@requires_ableton
def test_build_liveset_xml_without_locators_leaves_template_empty():
    xml = build_liveset_xml([TrackSpec("Audio", "A")], tempo=120.0)
    assert re.search(r"<Locators>\s*<Locators\s*/>\s*</Locators>", xml) is not None


@requires_ableton
def test_build_liveset_xml_embeds_locators_in_order():
    xml = build_liveset_xml([TrackSpec("Audio", "A")], tempo=120.0, locators=[(16.0, "Verse"), (0.0, "Intro")])
    m = re.search(r"<Locators><Locators>(.*?)</Locators></Locators>", xml, re.DOTALL)
    assert m is not None
    names = re.findall(r'<Name Value="([^"]*)" />', m.group(1))
    assert names == ["Verse", "Intro"]
    ids = re.findall(r'<Locator Id="(\d+)">', m.group(1))
    assert ids == ["0", "1"]
