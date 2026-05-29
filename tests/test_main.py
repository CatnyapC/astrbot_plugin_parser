import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_parser.main import ParserPlugin


class DummyConfig:
    def __init__(
        self,
        group_user_whitelist: dict[str, set[str]],
        admins_id: list[str] | None = None,
    ):
        self.group_user_whitelist = group_user_whitelist
        self.admins_id = admins_id or []

    def is_admin_user(self, user_id: str) -> bool:
        uid = str(user_id or "").strip()
        return uid in {str(admin_id).strip() for admin_id in self.admins_id}

    def is_whitelist_allowed(self, group_id: str | None, user_id: str) -> bool:
        uid = str(user_id or "").strip()
        if not self.group_user_whitelist:
            return True
        gid = str(group_id or "").strip()
        if not uid:
            return False
        if not gid:
            return True
        allowed = self.group_user_whitelist.get(gid)
        if allowed is None:
            allowed = self.group_user_whitelist.get("*")
        return allowed is not None and uid in allowed


class DummyGroup:
    def __init__(self, *, owner: str = "", admins: list[str] | None = None):
        self.group_owner = owner
        self.group_admins = admins or []


class DummyEvent:
    def __init__(
        self,
        *,
        timeout_requeue: bool = False,
        sender_id: str = "10001",
        group_id: str = "123456",
        is_admin: bool = False,
        group: DummyGroup | None = None,
        group_error: Exception | None = None,
    ):
        self.timeout_requeue = timeout_requeue
        self.sender_id = sender_id
        self.group_id = group_id
        self.admin = is_admin
        self.group = group
        self.group_error = group_error

    def get_extra(self, key: str, default=""):
        if key == "_router_timeout_requeue":
            return self.timeout_requeue
        return default

    def is_admin(self):
        return self.admin

    def get_group_id(self):
        return self.group_id

    def get_sender_id(self):
        return self.sender_id

    async def get_group(self, group_id):
        if self.group_error:
            raise self.group_error
        return self.group


def can_manage(event: DummyEvent):
    plugin = object.__new__(ParserPlugin)
    return asyncio.run(plugin._can_manage_whitelist(event))


def parse_whitelist_allowed(
    event: DummyEvent,
    group_user_whitelist: dict[str, set[str]],
    admins_id: list[str] | None = None,
):
    plugin = object.__new__(ParserPlugin)
    plugin.cfg = DummyConfig(group_user_whitelist, admins_id=admins_id)
    return plugin._is_parse_whitelist_allowed(
        event,
        event.get_group_id(),
        event.get_sender_id(),
    )


def test_should_skip_router_requeue_true():
    assert ParserPlugin._should_skip_router_requeue(DummyEvent(timeout_requeue=True))


def test_should_skip_router_requeue_false():
    assert not ParserPlugin._should_skip_router_requeue(
        DummyEvent(timeout_requeue=False)
    )


def test_parse_whitelist_allows_runtime_admin_in_any_group():
    whitelist = {"123456": {"10001"}}

    assert parse_whitelist_allowed(
        DummyEvent(sender_id="admin42", group_id="123456"),
        whitelist,
        admins_id=["admin42"],
    )
    assert parse_whitelist_allowed(
        DummyEvent(sender_id="admin42", group_id="654321"),
        whitelist,
        admins_id=["admin42"],
    )


def test_parse_whitelist_keeps_non_admin_group_behavior():
    whitelist = {"123456": {"10001"}}

    assert parse_whitelist_allowed(
        DummyEvent(sender_id="10001", group_id="123456"),
        whitelist,
        admins_id=["admin42"],
    )
    assert not parse_whitelist_allowed(
        DummyEvent(sender_id="10002", group_id="123456"),
        whitelist,
        admins_id=["admin42"],
    )


def test_parse_whitelist_allows_event_admin_role():
    assert parse_whitelist_allowed(
        DummyEvent(sender_id="10002", group_id="123456", is_admin=True),
        {"123456": {"10001"}},
    )


def test_can_manage_whitelist_allows_global_admin():
    assert can_manage(DummyEvent(is_admin=True, group_id="")) == (True, "")


def test_can_manage_whitelist_allows_group_owner():
    event = DummyEvent(sender_id="10001", group=DummyGroup(owner="10001"))

    assert can_manage(event) == (True, "")


def test_can_manage_whitelist_allows_group_admin():
    event = DummyEvent(sender_id="10002", group=DummyGroup(admins=["10002"]))

    assert can_manage(event) == (True, "")


def test_can_manage_whitelist_rejects_member():
    event = DummyEvent(
        sender_id="10003",
        group=DummyGroup(owner="10001", admins=["10002"]),
    )

    allowed, reason = can_manage(event)

    assert allowed is False
    assert "群主/管理员" in reason


def test_can_manage_whitelist_rejects_private_chat():
    allowed, reason = can_manage(DummyEvent(group_id=""))

    assert allowed is False
    assert "群聊" in reason


def test_can_manage_whitelist_rejects_group_lookup_failure():
    event = DummyEvent(group_error=RuntimeError("boom"))

    allowed, reason = can_manage(event)

    assert allowed is False
    assert "读取群权限失败" in reason
