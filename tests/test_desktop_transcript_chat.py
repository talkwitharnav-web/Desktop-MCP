import pytest

from desktop_mcp.transcript_chat import (
    ARRIVAL_SECONDS,
    ChatEntry,
    ReadingAnchor,
    anchor_at,
    anchor_position,
    arrival_offset,
    bubble_size,
    content_height,
    layout_bubbles,
    native_text,
    remap_anchor,
    utf16_length,
    validate_entries,
)


def test_role_labels_and_unicode_text_are_real_message_data():
    entries = validate_entries(
        (
            (1, "Résumé 👩‍💻", "漢字\nA\r\nB\rC\t😀 & <text>", "assistant"),
            (2, "Not the displayed role", "شكراً", "user"),
        )
    )
    assert entries[0].label == "Assistant · Résumé 👩‍💻"
    assert entries[1].label == "You"
    assert entries[0].text == "漢字\r\nA\r\nB\r\nC\t😀 & <text>"
    assert entries[1].text == "شكراً"
    assert ChatEntry(3, "Assistant", "", "assistant").label == "Assistant"


@pytest.mark.parametrize(
    "entries",
    [
        tuple((i, "Assistant", "x", "assistant") for i in range(33)),
        ((1, "Assistant", "x" * 16_001, "assistant"),),
        ((1, "x" * 257, "hello", "assistant"),),
        ((1, "Assistant", "a\0b", "assistant"),),
        ((1, "Assistant", "\ud800", "assistant"),),
        ((True, "Assistant", "hello", "assistant"),),
        ((-1, "Assistant", "hello", "assistant"),),
        ((1, "Assistant", "hello", "system"),),
        ((1, "Assistant", "hello", "assistant"), (1, "You", "reply", "user")),
        ((2, "Assistant", "hello", "assistant"), (1, "You", "reply", "user")),
    ],
)
def test_out_of_contract_entries_fail_instead_of_silently_truncating(entries):
    with pytest.raises(ValueError):
        validate_entries(entries)


def test_full_conversation_can_exceed_65535_utf16_units_without_global_offsets():
    messages = tuple((i, "Assistant", "😀" * 16_000, "assistant") for i in range(32))
    entries = validate_entries(messages)
    assert len(entries) == 32
    assert sum(utf16_length(entry.text) for entry in entries) == 1_024_000
    assert all(entry.text == "😀" * 16_000 for entry in entries)
    assert utf16_length(native_text("\n" * 16_000)) == 32_000


@pytest.mark.parametrize("width", [1, 17, 96, 320, 800, 1800])
@pytest.mark.parametrize("height", [1, 47, 200, 900])
@pytest.mark.parametrize("scale", [0.5, 1, 1.5, 2.5, 4])
def test_bubbles_stay_inside_width_and_native_body_height_is_bounded(width, height, scale):
    pitch = max(1, round(22 * scale))
    entries = tuple(
        ChatEntry(i, "Assistant", "text", "user" if i % 2 else "assistant") for i in range(32)
    )
    boxes = layout_bubbles(
        entries, dict.fromkeys(range(32), 10_000_000), width, height, scale, pitch
    )
    for box in boxes:
        assert 0 <= box.x < box.x + box.width <= width
        assert 1 <= box.body_height <= max(pitch, round(300 * scale))
        assert 1 <= box.size.body_width <= box.width
        assert box.size.padding * 2 + box.size.body_width == box.width
        assert box.height < 32768
    assert all(a.bottom < b.y for a, b in zip(boxes, boxes[1:]))
    assert content_height(boxes, scale) >= boxes[-1].bottom
    if width >= 96:
        assert boxes[0].x < boxes[1].x


def test_pruning_preserves_retained_sequence_and_falls_forward_for_a_pruned_anchor():
    entries = tuple(ChatEntry(i, "Assistant", "text", "assistant") for i in range(6))
    old = layout_bubbles(entries, dict.fromkeys(range(6), 80), 500, 240, 1, 22)
    anchor = anchor_at(old, old[3].y + 29)
    assert anchor == ReadingAnchor(3, 29)
    new = layout_bubbles(entries[2:], dict.fromkeys(range(6), 80), 500, 240, 1, 22)
    assert anchor_position(new, anchor) == new[1].y + 29
    assert remap_anchor(new, ReadingAnchor(1, 40)) == ReadingAnchor(2, 0)
    assert remap_anchor((), anchor) is None
    assert anchor_position((), anchor) == 0


def test_anchor_keeps_top_padding_and_desired_offset_through_temporary_height_clamping():
    entries = (ChatEntry(1, "Assistant", "text", "assistant"),)
    large = layout_bubbles(entries, {1: 280}, 500, 500, 1, 22)
    small = layout_bubbles(entries, {1: 30}, 500, 100, 1, 22)
    first = anchor_at(large, 0)
    assert anchor_position(large, first) == 0
    desired = ReadingAnchor(1, 250)
    assert remap_anchor(small, desired) == desired
    assert anchor_position(small, desired) == small[0].bottom
    assert anchor_position(large, remap_anchor(small, desired)) == large[0].y + 250


@pytest.mark.parametrize("scale", [0.75, 1, 1.5, 3])
def test_arrival_is_short_eased_monotonic_motion_not_a_text_reveal(scale):
    offsets = [arrival_offset(10 + i * ARRIVAL_SECONDS / 12, 10, scale) for i in range(13)]
    assert offsets[0] == round(6 * scale)
    assert offsets[-1] == 0
    assert all(a >= b >= 0 for a, b in zip(offsets, offsets[1:]))
    assert arrival_offset(9, 10, scale) == offsets[0]
    assert arrival_offset(20, 10, scale) == 0


@pytest.mark.parametrize(
    "width,height,scale,pitch",
    [
        (0, 20, 1, 22),
        (20, 0, 1, 22),
        (20, 20, 0, 22),
        (20, 20, float("nan"), 22),
        (20, 20, 1, 0),
        (32768, 100, 1, 22),
        (100, 32768, 1, 22),
        (100, 100, 17, 22),
        (100, 100, 1, 1025),
    ],
)
def test_invalid_geometry_is_rejected(width, height, scale, pitch):
    with pytest.raises(ValueError):
        bubble_size(width, height, scale, pitch, "assistant")
