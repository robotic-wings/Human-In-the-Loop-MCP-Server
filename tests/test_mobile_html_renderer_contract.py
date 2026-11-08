"""Accessibility contract for the production iPhone HTML renderer."""

from __future__ import annotations

import pytest

from central_http_service import mobile_html_renderer


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"tool": "get_text_input"}, "请输入回答"),
        ({"tool": "get_number_input"}, "请输入数字"),
        ({"tool": "get_user_choice", "choices": ["one", "two"]}, "请选择一个选项"),
        ({"tool": "get_user_choice", "choices": ["one", "two"], "multiple": True}, "请选择一个或多个选项"),
        ({"tool": "get_multiline_input"}, "请输入完整回答"),
        ({"tool": "show_confirmation_dialog"}, "请选择确认结果"),
        ({"tool": "show_info_message"}, "我已阅读并确认此信息"),
        ({"tool": "get_feedback_from_user"}, "反馈内容"),
        ({"tool": "assign_task_to_human"}, "任务状态"),
    ],
)
def test_mobile_submission_forms_label_every_interaction_family(payload: dict[str, object], expected: str) -> None:
    page = mobile_html_renderer(
        {"call_id": "call-1", "can_submit": True, "payload": {**payload, "prompt": "请作答"}}, "csrf-1"
    )
    assert expected in page
    assert "<label" in page or "<legend" in page
    assert 'name="result"' in page
    assert "提交回答" in page


def test_mobile_accept_page_names_both_human_actions() -> None:
    page = mobile_html_renderer({"call_id": "call-1", "can_accept": True, "payload": {"prompt": "请决定"}}, "csrf-1")
    assert "接听" in page
    assert "拒绝本次呼叫" in page
    assert "请明确选择接听或拒绝" in page
