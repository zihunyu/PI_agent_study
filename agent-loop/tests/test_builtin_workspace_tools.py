from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from pi_agent_loop import (
    Agent,
    CancellationToken,
    Model,
    OutputAccumulator,
    OutputPolicy,
    ScriptedProvider,
    ToolDispatchRuntime,
    ToolServices,
    VerifiedIdentity,
    WorkspacePathPolicy,
    WorkspaceToolError,
    assistant_message,
    create_builtin_tools,
)
from pi_agent_loop.tools.atomic_writer import AtomicFileWriter


async def invoke(tool, arguments, *, cancellation=None):
    assert tool.execute is not None
    validated = tool.validate_args(arguments)
    updates = []
    result = await tool.execute(
        "call-1",
        validated,
        cancellation or CancellationToken(),
        updates.append,
    )
    return result, updates


def python_command(source: str) -> str:
    arguments = [sys.executable, "-c", source]
    if os.name == "nt":
        return subprocess.list2cmdline(arguments)
    return shlex.join(arguments)


def create_directory_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError as symlink_error:
        if os.name != "nt":
            raise symlink_error
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise OSError(result.stderr or result.stdout or "mklink /J failed")


class BuiltinWorkspaceToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_profiles_are_fail_closed_and_shell_is_double_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            readonly = ToolServices.create(directory)
            self.assertEqual(
                [tool.name for tool in create_builtin_tools(readonly)],
                ["read", "list_dir", "find", "grep"],
            )
            with self.assertRaisesRegex(ValueError, "不允许工具"):
                create_builtin_tools(readonly, ["write"])

            writable = ToolServices.create(
                directory,
                security_profile="workspace-write",
            )
            self.assertEqual(
                [tool.name for tool in create_builtin_tools(writable)],
                ["read", "list_dir", "find", "grep", "write", "edit"],
            )
            for tool in create_builtin_tools(writable, ["write", "edit"]):
                self.assertTrue(tool.requires_approval)
                self.assertEqual(tool.replay_policy, "never")
            with self.assertRaisesRegex(ValueError, "显式"):
                ToolServices.create(directory, security_profile="full-access")
            with self.assertRaisesRegex(ValueError, "有限正数"):
                ToolServices.create(
                    directory,
                    security_profile="full-access",
                    allow_trusted_shell=True,
                    shell_timeout_seconds=float("nan"),
                )

            full = ToolServices.create(
                directory,
                security_profile="full-access",
                allow_trusted_shell=True,
            )
            shell = create_builtin_tools(full, ["shell"])[0]
            self.assertTrue(shell.requires_approval)
            self.assertEqual(shell.replay_policy, "never")
            self.assertIn("不是安全沙箱", shell.description)

    async def test_path_escape_absolute_and_symlink_new_parent_are_rejected(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as workspace,
            tempfile.TemporaryDirectory() as outside,
        ):
            policy = WorkspacePathPolicy(workspace)
            with self.assertRaises(WorkspaceToolError) as relative:
                policy.resolve("../outside.txt", must_exist=False)
            self.assertEqual(relative.exception.code, "path_outside_workspace")
            with self.assertRaises(WorkspaceToolError) as absolute:
                policy.resolve(str(Path(outside) / "secret.txt"), must_exist=False)
            self.assertEqual(absolute.exception.code, "path_outside_workspace")

            link = Path(workspace) / "escape"
            try:
                create_directory_link(link, Path(outside))
            except OSError as error:
                self.skipTest(f"当前环境不能创建目录 symlink/junction：{error}")
            with self.assertRaises(WorkspaceToolError) as existing_link:
                policy.resolve("escape", must_exist=True)
            self.assertEqual(
                existing_link.exception.code,
                "path_outside_workspace",
            )
            with self.assertRaises(WorkspaceToolError) as new_child:
                policy.resolve("escape/new/child.txt", must_exist=False)
            self.assertEqual(new_child.exception.code, "path_outside_workspace")
            if os.name == "nt":
                with self.assertRaises(WorkspaceToolError) as ads:
                    policy.resolve("file.txt:secret", must_exist=False)
                self.assertEqual(ads.exception.code, "path_invalid")

    async def test_read_observation_is_required_and_external_change_is_stale(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.txt"
            path.write_text("v1\n", encoding="utf-8")
            services = ToolServices.create(
                directory,
                security_profile="workspace-write",
            )
            tools = {tool.name: tool for tool in create_builtin_tools(services)}

            with self.assertRaises(WorkspaceToolError) as missing:
                await invoke(tools["write"], {"path": "state.txt", "content": "v2\n"})
            self.assertEqual(missing.exception.code, "observation_required")

            read, _ = await invoke(tools["read"], {"path": "state.txt"})
            version = read.details["observation"]
            path.write_text("external\n", encoding="utf-8")
            with self.assertRaises(WorkspaceToolError) as stale:
                await invoke(
                    tools["write"],
                    {
                        "path": "state.txt",
                        "content": "v2\n",
                        "expectedVersion": version,
                    },
                )
            self.assertEqual(stale.exception.code, "stale_observation")
            self.assertEqual(path.read_text(encoding="utf-8"), "external\n")

            refreshed, _ = await invoke(tools["read"], {"path": "state.txt"})
            written, _ = await invoke(
                tools["write"],
                {
                    "path": "state.txt",
                    "content": "v2\n",
                    "expectedVersion": refreshed.details["observation"],
                },
            )
            self.assertTrue(written.details["atomic"])
            self.assertEqual(path.read_text(encoding="utf-8"), "v2\n")

    async def test_concurrent_same_file_writes_are_serialized_and_one_goes_stale(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "counter.txt"
            path.write_text("zero", encoding="utf-8")
            entered = threading.Event()
            release = threading.Event()
            base_writer = AtomicFileWriter()

            class BlockingWriter:
                calls = 0

                def write(self, target, data, *, create_only):
                    self.calls += 1
                    entered.set()
                    release.wait(timeout=5)
                    base_writer.write(target, data, create_only=create_only)

            blocking = BlockingWriter()
            services = ToolServices.create(
                directory,
                security_profile="workspace-write",
                atomic_writer=blocking,  # type: ignore[arg-type]
            )
            tools = {tool.name: tool for tool in create_builtin_tools(services)}
            read, _ = await invoke(tools["read"], {"path": "counter.txt"})
            version = read.details["observation"]

            async def write_value(value: str):
                try:
                    result, _ = await invoke(
                        tools["write"],
                        {
                            "path": "counter.txt",
                            "content": value,
                            "expectedVersion": version,
                        },
                    )
                    return result
                except WorkspaceToolError as error:
                    return error

            first = asyncio.create_task(write_value("one"))
            await asyncio.to_thread(entered.wait, 5)
            second = asyncio.create_task(write_value("two"))
            release.set()
            outcomes = await asyncio.gather(first, second)

            errors = [item for item in outcomes if isinstance(item, WorkspaceToolError)]
            self.assertEqual([error.code for error in errors], ["stale_observation"])
            self.assertEqual(blocking.calls, 1)
            self.assertEqual(path.read_text(encoding="utf-8"), "one")

    async def test_edit_preserves_utf8_bom_and_crlf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "windows.txt"
            path.write_bytes(b"\xef\xbb\xbfalpha\r\nbeta\r\n")
            services = ToolServices.create(
                directory,
                security_profile="workspace-write",
            )
            tools = {tool.name: tool for tool in create_builtin_tools(services)}
            read, _ = await invoke(tools["read"], {"path": "windows.txt"})
            edited, _ = await invoke(
                tools["edit"],
                {
                    "path": "windows.txt",
                    "expectedVersion": read.details["observation"],
                    "edits": [{"oldText": "beta", "newText": "gamma"}],
                },
            )
            self.assertTrue(edited.details["bomPreserved"])
            self.assertEqual(
                path.read_bytes(),
                b"\xef\xbb\xbfalpha\r\ngamma\r\n",
            )

    async def test_read_list_find_grep_and_output_limits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "src" / "a.py").write_text(
                "needle\nline2\nline3\nline4\n",
                encoding="utf-8",
            )
            (root / "src" / "b.txt").write_text("other", encoding="utf-8")
            services = ToolServices.create(
                directory,
                output_policy=OutputPolicy(max_lines=2, max_bytes=20),
            )
            tools = {tool.name: tool for tool in create_builtin_tools(services)}
            listing, _ = await invoke(tools["list_dir"], {"path": "src"})
            self.assertEqual(
                [item["name"] for item in listing.details["entries"]], ["a.py", "b.txt"]
            )
            found, _ = await invoke(tools["find"], {"pattern": "*.py"})
            self.assertEqual(found.details["matches"], ["src/a.py"])
            grep, _ = await invoke(
                tools["grep"],
                {"pattern": "needle", "fileGlob": "*.py", "literal": True},
            )
            self.assertEqual(grep.details["matches"][0]["line"], 1)
            read, _ = await invoke(tools["read"], {"path": "src/a.py"})
            self.assertTrue(read.details["truncated"])
            self.assertLessEqual(len(read.content[0]["text"].encode("utf-8")), 20)

    async def test_output_accumulator_spills_exact_bytes_and_preserves_utf8_boundary(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            accumulator = OutputAccumulator(
                OutputPolicy(max_lines=3, max_bytes=11),
                mode="tail",
                spill_directory=Path(directory),
                prefix="test-",
            )
            payload = ("头一\n头二\n尾三\n尾四").encode()
            accumulator.feed(payload[:7])
            accumulator.feed(payload[7:])
            result = accumulator.finish()
            self.assertTrue(result.truncated)
            self.assertIsNotNone(result.spill_path)
            self.assertEqual(Path(result.spill_path).read_bytes(), payload)
            result.text.encode("utf-8")

    async def test_shell_cleans_env_limits_streams_times_out_and_cancels_tree(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            services = ToolServices.create(
                directory,
                security_profile="full-access",
                allow_trusted_shell=True,
                output_policy=OutputPolicy(max_lines=100, max_bytes=8 * 1024),
                shell_timeout_seconds=5,
                shell_maximum_timeout_seconds=10,
            )
            shell = create_builtin_tools(services, ["shell"])[0]

            with patch.dict(os.environ, {"PI_AGENT_TEST_SECRET": "do-not-leak"}):
                clean, _ = await invoke(
                    shell,
                    {
                        "command": python_command(
                            "import os; print(os.getenv('PI_AGENT_TEST_SECRET'))"
                        )
                    },
                )
            self.assertEqual(clean.details["stdout"].strip(), "None")
            self.assertFalse(clean.details["sandboxed"])

            large, updates = await invoke(
                shell,
                {
                    "command": python_command(
                        "import sys; sys.stdout.write('O'*20000); "
                        "sys.stderr.write('E'*20000)"
                    )
                },
            )
            self.assertTrue(large.details["stdoutTruncated"])
            self.assertTrue(large.details["stderrTruncated"])
            self.assertEqual(
                Path(large.details["stdoutSpillPath"]).read_bytes(),
                b"O" * 20_000,
            )
            self.assertEqual(
                Path(large.details["stderrSpillPath"]).read_bytes(),
                b"E" * 20_000,
            )
            self.assertTrue(updates)

            with self.assertRaises(WorkspaceToolError) as timeout:
                await invoke(
                    shell,
                    {
                        "command": python_command("import time; time.sleep(5)"),
                        "timeout": 0.2,
                    },
                )
            self.assertEqual(timeout.exception.code, "timeout")

            root = Path(directory)
            marker = root / "escaped.txt"
            (root / "child.py").write_text(
                "import time\nfrom pathlib import Path\n"
                "time.sleep(1.2)\nPath('escaped.txt').write_text('escaped')\n",
                encoding="utf-8",
            )
            (root / "parent.py").write_text(
                "import subprocess, sys, time\n"
                "subprocess.Popen([sys.executable, 'child.py'])\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            token = CancellationToken()
            running = asyncio.create_task(
                invoke(
                    shell,
                    {"command": python_command("exec(open('parent.py').read())")},
                    cancellation=token,
                )
            )
            await asyncio.sleep(0.3)
            token.cancel("test cancellation")
            with self.assertRaises(WorkspaceToolError) as aborted:
                await running
            self.assertEqual(aborted.exception.code, "aborted")
            await asyncio.sleep(1.5)
            self.assertFalse(marker.exists())

            with patch(
                "pi_agent_loop.tools.process_runner.OutputAccumulator.feed",
                side_effect=OSError("injected spill failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "输出管道读取失败"):
                    await asyncio.wait_for(
                        invoke(
                            shell,
                            {
                                "command": python_command(
                                    "import time; print('x', flush=True); time.sleep(5)"
                                )
                            },
                        ),
                        # Windows task-tree cleanup itself has a five-second
                        # deadline. The test guard must allow that cleanup to
                        # finish before asserting the injected pipe failure.
                        timeout=10,
                    )
            spill_directory = services.spill_directory
            services.close()
            self.assertFalse(spill_directory.exists())

    async def test_write_requires_runtime_approval_and_preserves_precommit_code(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            services = ToolServices.create(
                directory,
                security_profile="workspace-write",
            )
            write = create_builtin_tools(services, ["write"])[0]
            model = Model(id="test-model", provider="fake", api="fake")
            provider = ScriptedProvider(
                [
                    assistant_message(
                        model=model,
                        stop_reason="toolUse",
                        content=[
                            {
                                "type": "toolCall",
                                "id": "call-write",
                                "name": "write",
                                "arguments": {
                                    "path": "blocked.txt",
                                    "content": "must not be written",
                                },
                            }
                        ],
                    ),
                    assistant_message(
                        model=model,
                        content=[{"type": "text", "text": "done"}],
                    ),
                ]
            )
            agent = Agent(model=model, stream_fn=provider.stream, tools=[write])

            await agent.prompt("write")

            self.assertFalse((Path(directory) / "blocked.txt").exists())
            result = next(
                message
                for message in agent.state.messages
                if message["role"] == "toolResult"
            )
            self.assertEqual(result["details"]["code"], "permission_denied")

            identity = VerifiedIdentity(
                principal_id="test-worker",
                roles=frozenset({"writer"}),
                issuer="test",
                verification_id="test-verification",
            )
            runtime = ToolDispatchRuntime(
                [write],
                authorization=lambda _tool, _args, _context: True,
            )
            outcome = await runtime.dispatch(
                {
                    "type": "toolCall",
                    "id": "call-cas",
                    "name": "write",
                    "arguments": {
                        "path": "existing.txt",
                        "content": "new",
                    },
                },
                identity=identity,
            )
            # Missing file is a create and therefore valid. Make it existing and
            # dispatch a second unobserved overwrite to exercise precommit proof.
            self.assertFalse(outcome.is_error)
            overwrite = await runtime.dispatch(
                {
                    "type": "toolCall",
                    "id": "call-cas-2",
                    "name": "write",
                    "arguments": {
                        "path": "existing.txt",
                        "content": "newer",
                    },
                },
                identity=identity,
            )
            self.assertTrue(overwrite.is_error)
            self.assertEqual(overwrite.result.details["code"], "observation_required")
            self.assertTrue(overwrite.result.details["definitelyNotCommitted"])

    async def test_security_services_are_sealed_and_contract_binds_workspace(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as first,
            tempfile.TemporaryDirectory() as second,
        ):
            first_services = ToolServices.create(first)
            second_services = ToolServices.create(second)
            first_read = create_builtin_tools(first_services, ["read"])[0]
            second_read = create_builtin_tools(second_services, ["read"])[0]
            with self.assertRaises(FrozenInstanceError):
                first_services.path_policy = WorkspacePathPolicy(second)  # type: ignore[misc]
            with self.assertRaises(FrozenInstanceError):
                first_services.path_policy.workspace_root = Path(second)  # type: ignore[misc]
            self.assertNotEqual(
                first_read.security_policy_version,
                second_read.security_policy_version,
            )

    async def test_read_is_bounded_reserved_spill_is_hidden_and_regex_is_safe(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            oversized = root / "oversized.txt"
            with oversized.open("wb") as stream:
                stream.seek(20 * 1024 * 1024)
                stream.write(b"x")
            reserved = root / ".pi-agent-output"
            reserved.mkdir()
            (reserved / "secret.log").write_text("secret", encoding="utf-8")
            services = ToolServices.create(directory)
            tools = {tool.name: tool for tool in create_builtin_tools(services)}

            with self.assertRaises(WorkspaceToolError) as too_large:
                await invoke(tools["read"], {"path": "oversized.txt"})
            self.assertEqual(too_large.exception.code, "file_too_large")
            with self.assertRaises(WorkspaceToolError) as hidden:
                await invoke(
                    tools["read"],
                    {"path": ".pi-agent-output/secret.log"},
                )
            self.assertEqual(hidden.exception.code, "path_reserved")
            listing, _ = await invoke(tools["list_dir"], {"path": "."})
            self.assertNotIn(".pi-agent-output/", listing.content[0]["text"])
            with self.assertRaisesRegex(ValueError, "不允许量词"):
                await invoke(
                    tools["grep"],
                    {"pattern": ".*.*.*.*X", "literal": False},
                )

    async def test_output_spill_is_exact_for_every_utf8_chunk_boundary(self) -> None:
        payload = "头一\n头二\n尾三".encode()
        with tempfile.TemporaryDirectory() as directory:
            for mode in ("head", "tail"):
                for split in range(1, len(payload)):
                    accumulator = OutputAccumulator(
                        OutputPolicy(max_lines=2, max_bytes=7),
                        mode=mode,
                        spill_directory=Path(directory),
                        prefix=f"{mode}-{split}-",
                    )
                    accumulator.feed(payload[:split])
                    accumulator.feed(payload[split:])
                    result = accumulator.finish()
                    self.assertTrue(result.truncated)
                    self.assertEqual(Path(result.spill_path).read_bytes(), payload)
                    result.text.encode("utf-8")

    async def test_output_policy_rejects_limit_bypass_and_caps_spill(self) -> None:
        invalid_values = [True, 1.5, float("nan"), float("inf"), 10**100]
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    OutputPolicy(max_bytes=value)  # type: ignore[arg-type]

        with tempfile.TemporaryDirectory() as directory:
            accumulator = OutputAccumulator(
                OutputPolicy(max_lines=10, max_bytes=4, max_spill_bytes=8),
                mode="tail",
                spill_directory=Path(directory),
                prefix="bounded-",
            )
            accumulator.feed(b"0123456789abcdef")
            result = accumulator.finish()
            self.assertFalse(result.spill_complete)
            self.assertEqual(result.dropped_bytes, 8)
            self.assertEqual(Path(result.spill_path).read_bytes(), b"01234567")

            failing = OutputAccumulator(
                OutputPolicy(max_lines=10, max_bytes=4, max_spill_bytes=8),
                mode="head",
                spill_directory=Path(directory),
                prefix="failure-",
            )
            failing.feed(b"01234567")
            with patch(
                "pi_agent_loop.tools.output.os.fsync",
                side_effect=OSError("injected fsync failure"),
            ):
                with self.assertRaises(OSError):
                    failing.finish()
            self.assertIsNone(failing._spill)

    async def test_atomic_writer_reports_post_publish_durability_unknown(self) -> None:
        class FailingDirectorySyncWriter(AtomicFileWriter):
            @staticmethod
            def _sync_directory(_directory: Path) -> None:
                raise OSError("injected fsync failure")

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target.txt"
            target.write_text("old", encoding="utf-8")
            with self.assertRaises(WorkspaceToolError) as failure:
                FailingDirectorySyncWriter().write(
                    target,
                    b"new",
                    create_only=False,
                )
            self.assertEqual(failure.exception.code, "durability_unknown")
            self.assertTrue(failure.exception.details["outcomeUnknown"])
            self.assertEqual(target.read_bytes(), b"new")


if __name__ == "__main__":
    unittest.main()
