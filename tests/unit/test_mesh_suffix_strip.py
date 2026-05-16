"""
Hubitat appends ' on Home N' to the LABEL of any device that is shared into
a hub via Hub Mesh. The router strips this suffix before looking up the
canonical row so the mesh-mirror filter can find the native row to compare
hub_ip against.
"""

import pytest

from services.webhook_router import _MESH_SUFFIX_RE


@pytest.mark.unit
class TestMeshSuffixRegex:
    def test_strips_on_home_1_suffix(self):
        assert _MESH_SUFFIX_RE.sub("", "Light Office on Home 1") == "Light Office"

    def test_strips_on_home_10_suffix(self):
        assert _MESH_SUFFIX_RE.sub("", "Light Office on Home 10") == "Light Office"

    def test_does_not_strip_on_home_in_middle(self):
        # "...on Home X" must be a suffix, not anywhere in the string
        assert _MESH_SUFFIX_RE.sub("", "On Home Plate Lamp") == "On Home Plate Lamp"

    def test_does_not_strip_when_no_suffix(self):
        assert _MESH_SUFFIX_RE.sub("", "Plain Label") == "Plain Label"

    def test_does_not_strip_partial_match(self):
        # 'on Home' without trailing number is not a mesh suffix
        assert _MESH_SUFFIX_RE.sub("", "Light on Home") == "Light on Home"

    def test_case_sensitive(self):
        # The Hubitat suffix is exactly ' on Home N' — case-sensitive
        assert _MESH_SUFFIX_RE.sub("", "Light ON HOME 2") == "Light ON HOME 2"

    def test_trailing_whitespace_handled_by_caller(self):
        # Hubitat sometimes adds trailing spaces in labels; the regex itself
        # doesn't trim them — that's the caller's job.
        # This test documents the responsibility split.
        assert _MESH_SUFFIX_RE.sub("", "Light Office on Home 2 ") == "Light Office on Home 2 "
