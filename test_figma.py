#!/usr/bin/env python3
"""Offline checks: URL parsing, node normalization, the file list, and exports."""
import base64
import json
import os
import tempfile

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


def test_parse_tabs():
    settings = {"windows": [{"tabs": [
        {"path": "/file/wGQwH64K68FhUpn2lFw1Tr", "title": "Nomm", "editorType": "design",
         "lastViewedAt": 1789542520954, "thumbnail": {"url": "https://s3/x"}},
        # same file seen again in another window — keep the newer view
        {"path": "/file/wGQwH64K68FhUpn2lFw1Tr", "title": "Nomm", "lastViewedAt": 1},
        {"path": "/file/ABCdef123456", "title": "닫힌 탭", "isDiscarded": True},
        {"path": "/files/recent", "title": "파일 브라우저"},
    ]}]}

    tabs = figma.parse_tabs(settings)
    assert [t["key"] for t in tabs] == ["wGQwH64K68FhUpn2lFw1Tr"], tabs
    assert tabs[0]["lastViewedAt"] == 1789542520954, "newer view wins"
    assert tabs[0]["thumbnail"] == "https://s3/x"
    assert "pinnedInFigma" not in tabs[0], "absent flags stay absent"
    assert figma.parse_tabs({}) == []


def test_hidden_and_favorites(tmp):
    figma.FAVORITES_FILE = os.path.join(tmp, "favorites.json")
    figma.HIDDEN_FILE = os.path.join(tmp, "hidden.json")
    figma.FIGMA_SETTINGS = os.path.join(tmp, "settings.json")
    with open(figma.FIGMA_SETTINGS, "w", encoding="utf-8") as f:
        json.dump({"windows": [{"tabs": [
            {"path": "/file/AAAAAAAAAAAA", "title": "열린 파일", "lastViewedAt": 200},
            {"path": "/file/BBBBBBBBBBBB", "title": "지워진 파일", "lastViewedAt": 100},
        ]}]}, f)

    assert [f["key"] for f in figma.list_files()] == ["AAAAAAAAAAAA", "BBBBBBBBBBBB"]

    figma.edit_hidden({"action": "hide", "key": "BBBBBBBBBBBB"})
    assert [f["key"] for f in figma.list_files()] == ["AAAAAAAAAAAA"], "hidden drops out"
    assert figma.read_hidden() == ["BBBBBBBBBBBB"]

    figma.edit_hidden({"action": "show", "key": "BBBBBBBBBBBB"})
    assert len(figma.list_files()) == 2, "hiding is reversible"

    # A favorite sorts ahead of a more recently viewed tab.
    figma.edit_favorites({"action": "add", "key": "BBBBBBBBBBBB", "title": "고정"})
    assert figma.list_files()[0]["key"] == "BBBBBBBBBBBB"

    # A file we could not check must not be hidden by clean.
    figma.file_meta = lambda key: (False, None) if key == "BBBBBBBBBBBB" else (None, None)
    report = figma.clean_files()
    assert report["hidden"] == ["BBBBBBBBBBBB"], report
    assert report["unverified"] == ["AAAAAAAAAAAA"], report
    assert [f["key"] for f in figma.list_files()] == ["AAAAAAAAAAAA"]


def test_save_export(tmp):
    raw = b"\x89PNG\r\n\x1a\n binary"
    png = figma.save_export({"format": "PNG", "base64": base64.b64encode(raw).decode()},
                            tmp, "1:2")
    assert png.endswith("1-2.png"), png          # ':' is not usable in a filename
    with open(png, "rb") as f:
        assert f.read() == raw, "binary survives the base64 round trip"

    svg = figma.save_export({"format": "SVG", "text": "<svg/>"}, tmp, "3:4")
    assert svg.endswith("3-4.svg"), svg
    with open(svg, encoding="utf-8") as f:
        assert f.read() == "<svg/>"


def test_wants_verify():
    assert figma.wants_verify("verify") is True, "a valueless flag still counts"
    assert figma.wants_verify("verify=1") is True
    assert figma.wants_verify("") is False
    assert figma.wants_verify("other=1") is False


def test_same_file():
    assert figma.same_file("Nomm", "Nomm") is True
    assert figma.same_file(" Nomm ", "Nomm") is True, "trim both sides"
    assert figma.same_file("Nomm", "Design System") is False
    # Unknown on either side must not read as a mismatch: the caller warns instead
    # of blocking, because the plugin exposes no file key to compare against.
    assert figma.same_file(None, "Nomm") is None
    assert figma.same_file("Nomm", None) is None


if __name__ == "__main__":
    test_parse_target()
    test_norm_node()
    test_parse_tabs()
    test_wants_verify()
    test_same_file()
    with tempfile.TemporaryDirectory() as tmp:
        test_hidden_and_favorites(tmp)
        test_save_export(tmp)
    print("ok")
