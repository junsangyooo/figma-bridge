#!/usr/bin/env python3
"""Offline checks for the pure functions: URL parsing and node normalization."""
import figma


def test_parse_target():
    cases = [
        ("https://www.figma.com/design/abc123XYZ890/My-File?node-id=12-34",
         ("abc123XYZ890", "12:34")),
        ("https://figma.com/file/abc123XYZ890/Old-Style", ("abc123XYZ890", None)),
        ("https://www.figma.com/board/abc123XYZ890/Jam?node-id=0-1&t=x",
         ("abc123XYZ890", "0:1")),
        ("abc123XYZ890", ("abc123XYZ890", None)),
    ]
    for src, want in cases:
        got = figma.parse_target(src)
        assert got == want, f"{src}: {got} != {want}"


def test_norm_node():
    raw = {
        "id": "1:2", "name": "Button", "type": "INSTANCE",
        "absoluteBoundingBox": {"x": 0, "y": 0, "width": 342.004, "height": 48},
        "layoutMode": "HORIZONTAL", "itemSpacing": 8,
        "paddingTop": 12, "paddingRight": 16, "paddingBottom": 12, "paddingLeft": 16,
        "fills": [{"type": "SOLID", "color": {"r": 0.18, "g": 0.796, "b": 0.627}}],
        "strokes": [],
        "opacity": 1,
        "boundVariables": {"fills": [{"type": "VARIABLE_ALIAS", "id": "V:1"}]},
        "children": [
            {"id": "1:3", "name": "Label", "type": "TEXT", "characters": "확인",
             "style": {"fontFamily": "Inter", "fontSize": 16, "fontWeight": 600,
                       "textCase": "UPPER"},
             "children": [{"id": "1:4", "name": "deep", "type": "VECTOR"}]},
        ],
    }

    full = figma.norm_node(raw)
    assert full["box"]["width"] == 342.0
    assert full["layout"] == {"mode": "HORIZONTAL", "gap": 8, "padding": [12, 16, 12, 16]}
    assert full["fills"] == [{"type": "SOLID", "hex": "#2ECBA0"}]
    assert "strokes" not in full, "empty paint lists are omitted, not null"
    assert "opacity" not in full, "default opacity is omitted"
    assert full["boundVariables"] == ["fills"]
    label = full["children"][0]
    assert label["text"] == "확인"
    assert label["style"] == {"fontFamily": "Inter", "fontSize": 16, "fontWeight": 600}

    # A single-child auto-layout frame reports no itemSpacing at all.
    no_gap = figma.norm_node({"id": "2:1", "name": "Badge", "type": "FRAME",
                              "layoutMode": "HORIZONTAL", "paddingTop": 6})
    assert no_gap["layout"] == {"mode": "HORIZONTAL", "padding": [6, 0, 0, 0]}, no_gap

    pruned = figma.norm_node(raw, depth=1)
    assert pruned["children"][0]["childCount"] == 1
    assert "children" not in pruned["children"][0], "pruned level reports count only"


if __name__ == "__main__":
    test_parse_target()
    test_norm_node()
    print("ok")
