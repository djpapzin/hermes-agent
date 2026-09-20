"""Regression tests for cua-driver get_window_state metadata parsing."""

from tools.computer_use.cua_backend_capture import _tree_and_title


def test_tree_and_title_uses_structured_window_title():
    tree, title = _tree_and_title({
        "data": "✅ Chrome — 1 element\n- [0] AXButton \"Open\"",
        "structuredContent": {
            "window_title": "Chrome test page",
            "elements": [{"element_index": 0, "role": "AXButton", "label": "Open"}],
        },
    })

    assert tree == '- [0] AXButton "Open"'
    assert title == "Chrome test page"


def test_tree_and_title_keeps_legacy_text_title_fallback():
    tree, title = _tree_and_title({
        "data": '✅ Chrome — 1 element\nAXWindow "Legacy title"\n- [0] AXButton "Open"',
        "structuredContent": {"elements": []},
    })

    assert tree == 'AXWindow "Legacy title"\n- [0] AXButton "Open"'
    assert title == "Legacy title"
