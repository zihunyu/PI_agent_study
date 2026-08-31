"""Project/session catalog, durable context resume, fork and writer lease tests."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pi_agent_loop import (  # noqa: E402
    DurableAgentWorkspace,
    JsonlOperationEventStore,
    Model,
    ScriptedProvider,
    SessionAlreadyOpenError,
    SessionConfigurationMismatchError,
    SessionJournalOperationEventStore,
    assistant_message,
)


class WorkspaceSessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.model = Model(id="session-model", provider="fake", api="fake")

    async def test_catalog_project_session_lifecycle_and_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_path = root / "project"
            project_path.mkdir()
            workspace = DurableAgentWorkspace.open(root / "state")

            project = await workspace.create_project(
                project_path,
                title="订单项目",
                project_id="project-orders",
            )
            first = await workspace.create_session(
                project.project_id,
                title="首次会话",
                session_id="session-first",
            )
            second = await workspace.create_session(
                project.project_id,
                title="第二次会话",
                session_id="session-second",
            )
            await workspace.catalog.reorder_sessions(
                project.project_id,
                [second.session_id, first.session_id],
            )

            sessions = await workspace.catalog.list_sessions(
                project_id=project.project_id
            )
            self.assertEqual(
                [item.session_id for item in sessions],
                ["session-second", "session-first"],
            )
            renamed = await workspace.catalog.rename_session(
                first.session_id,
                "订单排障",
            )
            self.assertEqual(renamed.title, "订单排障")
            await workspace.catalog.archive_session(second.session_id)
            active = await workspace.catalog.list_sessions(
                project_id=project.project_id
            )
            self.assertEqual([item.session_id for item in active], [first.session_id])
            await workspace.catalog.unarchive_session(second.session_id)

            await workspace.catalog.delete_project(project.project_id)
            detached = await workspace.catalog.get_session(first.session_id)
            self.assertIsNone(detached.project_id)
            self.assertEqual(await workspace.catalog.list_projects(), [])

            raw_database = (root / "state" / "agent-state.sqlite3").read_bytes()
            self.assertNotIn("订单排障".encode(), raw_database)

    async def test_two_catalog_instances_use_cas_without_lost_updates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_path = root / "project"
            project_path.mkdir()
            first = DurableAgentWorkspace.open(root / "state")
            project = await first.create_project(project_path, title="Demo")
            second = DurableAgentWorkspace.open(root / "state")

            await asyncio.gather(*(
                (first if index % 2 == 0 else second).create_session(
                    project.project_id,
                    title=f"Session {index}",
                    session_id=f"session-{index}",
                )
                for index in range(8)
            ))

            reopened = DurableAgentWorkspace.open(root / "state")
            sessions = await reopened.catalog.list_sessions(
                project_id=project.project_id
            )
            self.assertEqual(
                {item.session_id for item in sessions},
                {f"session-{index}" for index in range(8)},
            )

    async def test_reopening_session_restores_previous_conversation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_path = root / "project"
            project_path.mkdir()
            workspace = DurableAgentWorkspace.open(root / "state")
            project = await workspace.create_project(project_path, title="Demo")
            session = await workspace.create_session(
                project.project_id,
                title="上下文测试",
            )

            first_provider = ScriptedProvider([
                assistant_message(
                    model=self.model,
                    content=[{"type": "text", "text": "第一个答案"}],
                )
            ])
            first_host = await workspace.open_session(
                session.session_id,
                model=self.model,
                stream_fn=first_provider.stream,
                system_prompt="保持上下文",
                tools=[],
            )
            await first_host.prompt("我的第一个问题")
            await first_host.close()

            def assert_history(context, _options):
                texts = [
                    block.get("text")
                    for message in context["messages"]
                    for block in message.get("content", [])
                    if block.get("type") == "text"
                ]
                self.assertIn("我的第一个问题", texts)
                self.assertIn("第一个答案", texts)
                self.assertIn("我上个问题是什么", texts)
                return assistant_message(
                    model=self.model,
                    content=[{"type": "text", "text": "你问了我的第一个问题"}],
                )

            second_provider = ScriptedProvider([assert_history])
            second_host = await workspace.open_session(
                session.session_id,
                model=self.model,
                stream_fn=second_provider.stream,
                system_prompt="保持上下文",
                tools=[],
            )
            self.assertEqual(
                [message["role"] for message in second_host.agent.state.messages],
                ["user", "assistant"],
            )
            await second_host.prompt("我上个问题是什么")
            await second_host.close()

    async def test_legacy_jsonl_operations_are_merged_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_path = root / "project"
            project_path.mkdir()
            legacy_path = root / "operation-events.jsonl"
            legacy = JsonlOperationEventStore(legacy_path)
            for index in range(2):
                await legacy.append_batch(
                    "legacy-session",
                    f"old-operation-{index}",
                    [
                        ("operation_started", {"configuration": {}, "tools": []}),
                        (
                            "message_appended",
                            {
                                "message": {
                                    "role": "user",
                                    "content": [
                                        {"type": "text", "text": f"旧问题 {index}"}
                                    ],
                                }
                            },
                        ),
                        (
                            "message_appended",
                            {
                                "message": assistant_message(
                                    model=self.model,
                                    content=[
                                        {"type": "text", "text": f"旧回答 {index}"}
                                    ],
                                )
                            },
                        ),
                        ("operation_finished", {"outcome": "completed"}),
                    ],
                    expected_last_sequence=-1,
                )

            workspace = DurableAgentWorkspace.open(root / "state")
            project = await workspace.create_project(project_path, title="Demo")
            session = await workspace.create_session(
                project.project_id,
                title="旧会话",
                session_id="legacy-session",
            )
            imported = await workspace.import_legacy_jsonl_session(
                legacy_path,
                session_id=session.session_id,
            )
            self.assertTrue(imported.imported)
            self.assertEqual(imported.message_count, 4)
            repeated = await workspace.import_legacy_jsonl_session(
                legacy_path,
                session_id=session.session_id,
            )
            self.assertFalse(repeated.imported)
            self.assertTrue(legacy_path.is_file())

            host = await workspace.open_session(
                session.session_id,
                model=self.model,
                stream_fn=ScriptedProvider([]).stream,
                system_prompt="legacy-test",
                tools=[],
            )
            transcript = str(host.agent.state.messages)
            self.assertIn("旧问题 0", transcript)
            self.assertIn("旧回答 1", transcript)
            await host.close()

    async def test_fork_freezes_parent_history_at_fork_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_path = root / "project"
            project_path.mkdir()
            workspace = DurableAgentWorkspace.open(root / "state")
            project = await workspace.create_project(project_path, title="Demo")
            source = await workspace.create_session(project.project_id, title="主线")

            provider = ScriptedProvider([
                assistant_message(
                    model=self.model,
                    content=[{"type": "text", "text": "主线早期回答"}],
                )
            ])
            host = await workspace.open_session(
                source.session_id,
                model=self.model,
                stream_fn=provider.stream,
                system_prompt="fork-test",
                tools=[],
            )
            await host.prompt("主线早期问题")
            await host.close()

            child = await workspace.fork_session(source.session_id, title="分支")

            later_provider = ScriptedProvider([
                assistant_message(
                    model=self.model,
                    content=[{"type": "text", "text": "主线后期回答"}],
                )
            ])
            later_host = await workspace.open_session(
                source.session_id,
                model=self.model,
                stream_fn=later_provider.stream,
                system_prompt="fork-test",
                tools=[],
            )
            await later_host.prompt("主线后期问题")
            await later_host.close()

            child_provider = ScriptedProvider([
                assistant_message(
                    model=self.model,
                    content=[{"type": "text", "text": "分支回答"}],
                )
            ])
            child_host = await workspace.open_session(
                child.session_id,
                model=self.model,
                stream_fn=child_provider.stream,
                system_prompt="fork-test",
                tools=[],
            )
            inherited_text = str(child_host.agent.state.messages)
            self.assertIn("主线早期问题", inherited_text)
            self.assertIn("主线早期回答", inherited_text)
            self.assertNotIn("主线后期问题", inherited_text)
            await child_host.close()

    async def test_product_session_allows_only_one_open_writer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_path = root / "project"
            project_path.mkdir()
            workspace = DurableAgentWorkspace.open(root / "state")
            project = await workspace.create_project(project_path, title="Demo")
            session = await workspace.create_session(project.project_id, title="锁测试")
            first = await workspace.open_session(
                session.session_id,
                model=self.model,
                stream_fn=ScriptedProvider([]).stream,
                system_prompt="same",
                tools=[],
            )
            with self.assertRaises(SessionAlreadyOpenError):
                await workspace.open_session(
                    session.session_id,
                    model=self.model,
                    stream_fn=ScriptedProvider([]).stream,
                    system_prompt="same",
                    tools=[],
                )
            await first.close()
            reopened = await workspace.open_session(
                session.session_id,
                model=self.model,
                stream_fn=ScriptedProvider([]).stream,
                system_prompt="same",
                tools=[],
            )
            await reopened.close()

    async def test_configuration_mismatch_requires_new_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_path = root / "project"
            project_path.mkdir()
            workspace = DurableAgentWorkspace.open(root / "state")
            project = await workspace.create_project(project_path, title="Demo")
            session = await workspace.create_session(project.project_id, title="配置测试")
            host = await workspace.open_session(
                session.session_id,
                model=self.model,
                stream_fn=ScriptedProvider([]).stream,
                system_prompt="version-1",
                tools=[],
            )
            await host.close()

            with self.assertRaises(SessionConfigurationMismatchError):
                await workspace.open_session(
                    session.session_id,
                    model=self.model,
                    stream_fn=ScriptedProvider([]).stream,
                    system_prompt="version-2",
                    tools=[],
                )
            correctly_configured = await workspace.open_session(
                session.session_id,
                model=self.model,
                stream_fn=ScriptedProvider([]).stream,
                system_prompt="version-1",
                tools=[],
            )
            await correctly_configured.close()

    async def test_explicit_configuration_migration_preserves_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_path = root / "project"
            project_path.mkdir()
            workspace = DurableAgentWorkspace.open(root / "state")
            project = await workspace.create_project(project_path, title="Demo")
            session = await workspace.create_session(
                project.project_id,
                title="配置迁移测试",
            )
            old_host = await workspace.open_session(
                session.session_id,
                model=self.model,
                stream_fn=ScriptedProvider(
                    [
                        assistant_message(
                            model=self.model,
                            content=[{"type": "text", "text": "旧配置回答"}],
                        )
                    ]
                ).stream,
                system_prompt="version-1",
                tools=[],
            )
            await old_host.prompt("旧配置问题")
            await old_host.close()
            old_metadata = await workspace.catalog.get_session(session.session_id)

            migrated_host = await workspace.open_session(
                session.session_id,
                model=self.model,
                stream_fn=ScriptedProvider([]).stream,
                system_prompt="version-2",
                tools=[],
                allow_configuration_migration=True,
            )
            try:
                migrated_metadata = await workspace.catalog.get_session(
                    session.session_id
                )
                self.assertNotEqual(
                    old_metadata.configuration_hash,
                    migrated_metadata.configuration_hash,
                )
                self.assertEqual(
                    [message["role"] for message in migrated_host.agent.state.messages],
                    ["user", "assistant"],
                )
                self.assertEqual(
                    migrated_host.agent.state.messages[0]["content"][0]["text"],
                    "旧配置问题",
                )
            finally:
                await migrated_host.close()

    async def test_configuration_migration_rejects_unfinished_operation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_path = root / "project"
            project_path.mkdir()
            workspace = DurableAgentWorkspace.open(root / "state")
            project = await workspace.create_project(project_path, title="Demo")
            session = await workspace.create_session(
                project.project_id,
                title="未完成任务迁移测试",
            )
            old_host = await workspace.open_session(
                session.session_id,
                model=self.model,
                stream_fn=ScriptedProvider([]).stream,
                system_prompt="version-1",
                tools=[],
            )
            await old_host.close()
            old_metadata = await workspace.catalog.get_session(session.session_id)
            operation_store = SessionJournalOperationEventStore(
                workspace.catalog.journal,
                workspace.principal,
            )
            await operation_store.append(
                "operation_started",
                session.session_id,
                "unfinished-operation",
                {"configuration": {}, "tools": []},
            )

            with self.assertRaisesRegex(
                SessionConfigurationMismatchError,
                "未完成 Operation",
            ):
                await workspace.open_session(
                    session.session_id,
                    model=self.model,
                    stream_fn=ScriptedProvider([]).stream,
                    system_prompt="version-2",
                    tools=[],
                    allow_configuration_migration=True,
                )
            unchanged = await workspace.catalog.get_session(session.session_id)
            self.assertEqual(
                unchanged.configuration_hash,
                old_metadata.configuration_hash,
            )


if __name__ == "__main__":
    unittest.main()
