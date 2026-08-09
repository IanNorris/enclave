"""Tests for the per-turn Auto Fusion routing directive."""

from types import SimpleNamespace

import pytest

from enclave.agent import main as agent_main
from enclave.common.protocol import Message, MessageType


class _FakeSession:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, content: str, **_: object) -> None:
        self.sent.append(content)


@pytest.mark.asyncio
async def test_auto_fusion_directive_is_silent_internal_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The router must not make grading look like part of the user request."""
    sdk_session = _FakeSession()
    state = SimpleNamespace(
        mimir_enabled=False,
        sdk_session=sdk_session,
        listener_ctl=None,
        turn_active=False,
    )
    monkeypatch.setattr(
        agent_main._fusion_mod,
        "read_fusion_mode",
        lambda _: agent_main._fusion_mod.AUTO_FUSION_MODEL_ID,
    )

    await agent_main.handle_user_message(
        state,
        Message(
            type=MessageType.USER_MESSAGE,
            payload={"content": "Design the deployment API."},
        ),
    )

    assert len(sdk_session.sent) == 1
    routed = sdk_session.sent[0]
    directive, user_request = routed.split("</auto_fusion>\n\n", 1)
    assert "Internal routing policy" in directive
    assert "not part of the user's request" in directive
    assert "do not mention, explain, or ask the user about grading" in directive
    assert "exactly once" in directive
    assert user_request == "Design the deployment API."
