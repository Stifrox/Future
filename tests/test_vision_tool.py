from types import SimpleNamespace
import numpy as np
import pytest

from tools import vision_tool


def test_is_vision_query_detection():
    assert vision_tool.is_vision_query("look at this")
    assert vision_tool.is_vision_query("hey future, look at this")
    assert vision_tool.is_vision_query("can you see this?")
    assert vision_tool.is_vision_query("look at what I'm showing you")
    assert vision_tool.is_vision_query("look at my screen")
    assert vision_tool.is_vision_query("watch this")
    assert vision_tool.is_vision_query("check this out")
    assert vision_tool.is_vision_query("look through the camera")
    assert not vision_tool.is_vision_query("what is the weather today")
    assert not vision_tool.is_vision_query("play some music on spotify")


def test_frame_to_data_url_encoding():
    # Create a simple 100x100 dummy image
    dummy_frame = np.zeros((100, 100, 3), dtype=np.uint8)
    data_url = vision_tool.frame_to_data_url(dummy_frame)
    assert data_url is not None
    assert data_url.startswith("data:image/jpeg;base64,")


def test_analyze_visual_frames_mocked():
    captured = {}

    def fake_create(**kwargs):
        captured["messages"] = kwargs["messages"]
        captured["model"] = kwargs["model"]
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="I see a circuit board with an Arduino and LED."))]
        )

    mock_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
    )

    dummy_frame = np.zeros((50, 50, 3), dtype=np.uint8)
    frame_url = vision_tool.frame_to_data_url(dummy_frame)

    result = vision_tool.analyze_visual_frames(
        frames=[frame_url],
        user_prompt="What is this component?",
        client=mock_client,
        model="gpt-4.1-mini",
    )

    assert result == "I see a circuit board with an Arduino and LED."
    assert captured["model"] == "gpt-4.1-mini"
    assert len(captured["messages"]) == 2
    assert "Circuit board" in result or "Arduino" in result


def test_save_visual_memory_persists(monkeypatch):
    saved_entries = []

    def fake_load():
        return list(saved_entries)

    def fake_save(mem):
        saved_entries.clear()
        saved_entries.extend(list(mem))

    monkeypatch.setattr(vision_tool, "load_memory", fake_load)
    monkeypatch.setattr(vision_tool, "save_memory", fake_save)

    vision_tool.save_visual_memory(
        user_query="Look at this receipt",
        visual_analysis="Receipt from Home Depot for $42.50 with screws and lumber.",
        video_path="logs/video/test_clip.avi",
    )

    assert len(saved_entries) >= 1
    last = saved_entries[-1]
    assert "[Visual" in last["user"]
    assert "Receipt from Home Depot" in last["ai"]


def test_look_at_this_with_provided_frames(monkeypatch):
    dummy_frame = np.zeros((40, 40, 3), dtype=np.uint8)
    frame_url = vision_tool.frame_to_data_url(dummy_frame)

    monkeypatch.setattr(
        vision_tool,
        "analyze_visual_frames",
        lambda frames, user_prompt, client=None, model=None: "I see a mechanical part on a workbench.",
    )
    monkeypatch.setattr(vision_tool, "load_memory", lambda: [])
    monkeypatch.setattr(vision_tool, "save_memory", lambda mem: None)

    result = vision_tool.look_at_this(
        user_query="Hey Future look at this part",
        provided_frames=[frame_url],
    )

    assert result["success"] is True
    assert result["reply"] == "I see a mechanical part on a workbench."
    assert len(result["frames"]) == 1


def test_get_vision_status():
    status = vision_tool.get_vision_status()
    assert "opencv_available" in status
    assert "vision_model" in status
    assert "video_dir" in status
