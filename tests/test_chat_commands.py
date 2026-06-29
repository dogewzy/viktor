from core.chat_commands import (
    normalize_user_command_text,
    parse_manual_mount_redo_sample_ids,
    parse_repo_script_command,
)


def test_parse_manual_mount_redo_sample_ids():
    is_command, sample_ids, error = parse_manual_mount_redo_sample_ids(
        normalize_user_command_text("/重做人工识别挂载数据 123,456 123，789")
    )

    assert is_command is True
    assert sample_ids == [123, 456, 789]
    assert error is None


def test_manual_mount_missing_sample_ids_returns_usage_error():
    is_command, sample_ids, error = parse_manual_mount_redo_sample_ids(
        normalize_user_command_text("／重做人工识别挂载数据")
    )

    assert is_command is True
    assert sample_ids == []
    assert error is not None


def test_manual_mount_invalid_sample_ids_returns_error():
    is_command, sample_ids, error = parse_manual_mount_redo_sample_ids(
        normalize_user_command_text("/重做人工识别挂载数据 123 abc")
    )

    assert is_command is True
    assert sample_ids == []
    assert "abc" in (error or "")


def test_parse_wechat_short_link_command():
    command, error = parse_repo_script_command("/微信视频号短链覆盖clip_url 1719158016")

    assert error is None
    assert command is not None
    assert command.name == "微信视频号短链覆盖clip_url"
    assert command.repo_connector_id == "order-api"
    assert command.script_path == "scripts/wechat_short_link_overwrite_clip_url.py"
    assert command.execute_args == ["--execute", "--json"]
    assert command.sample_ids == [1719158016]


def test_parse_wechat_short_link_alias_and_dedupes_ids():
    command, error = parse_repo_script_command("/视频号转短链覆盖 1,2，1")

    assert error is None
    assert command is not None
    assert command.sample_ids == [1, 2]


def test_repo_script_command_requires_sample_id():
    command, error = parse_repo_script_command("/微信视频号短链覆盖clip_url")

    assert command is None
    assert error == "请在命令后提供至少一个 sample_id"


def test_non_repo_script_command_is_ignored():
    command, error = parse_repo_script_command("查一下 1719158016 为什么没有结果")

    assert command is None
    assert error is None
