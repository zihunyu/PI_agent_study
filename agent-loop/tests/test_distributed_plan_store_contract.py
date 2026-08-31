"""Strict storage/topology contracts for durable distributed Plan workers."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from pi_agent_loop import (
    AgentTool,
    AgentToolResult,
    DurableAgentHost,
    DurablePlanStoreCapabilities,
    DurablePlanStoreConfigurationError,
    DurablePlanWorker,
    DurablePlanWorkflow,
    HybridRequestPlanner,
    IdentityClaim,
    IntentPlanPolicy,
    JournalPrincipal,
    Model,
    MultiIntentPlan,
    PlanParameterContract,
    PlanStep,
    ResourceFencingLease,
    ResourceLockBackendCapabilities,
    ScriptedProvider,
    SessionJournalPlanStore,
    SQLiteSessionEventJournal,
    StaticIdentityVerifier,
    StaticJournalKeyProvider,
)


MODEL = Model(id="distributed-store-test", provider="test", api="scripted")


class _FakeMultiHostPlanStore(SessionJournalPlanStore):
    """Test adapter that declares the same contract a remote DB must provide."""

    capabilities = DurablePlanStoreCapabilities(
        backend_name="fake-shared-transactional-store",
        atomic_fenced_append=True,
        supports_cross_process=True,
        supports_multi_host=True,
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.close_calls = 0
        self.initialize_calls = 0

    async def initialize(self, plan):
        self.initialize_calls += 1
        return await super().initialize(plan)

    async def aclose(self) -> None:
        self.close_calls += 1


class _NonAtomicPlanStore(_FakeMultiHostPlanStore):
    capabilities = DurablePlanStoreCapabilities(
        backend_name="fake-read-check-append-store",
        atomic_fenced_append=False,
        supports_cross_process=True,
        supports_multi_host=True,
    )


class _FakeMultiHostResourceLocks:
    capabilities = ResourceLockBackendCapabilities(
        backend_name="fake-shared-resource-locks",
        supports_cross_process=True,
        supports_multi_host=True,
        atomic_multi_resource_acquire=True,
        supports_lease_renewal=True,
        supports_fencing_tokens=True,
    )

    def __init__(self) -> None:
        self._generation = 0

    async def acquire(
        self,
        _resource_keys,
        *,
        owner_token,
        access,
        timeout_seconds,
    ) -> ResourceFencingLease | None:
        if not (owner_token and access and timeout_seconds > 0):
            return None
        self._generation += 1
        return ResourceFencingLease(
            owner_token=owner_token,
            fencing_token=self._generation,
            fencing_scope="test-shared-tool-resources",
        )

    async def renew(self, _resource_keys, *, owner_token) -> bool:
        return bool(owner_token)

    async def release(self, _resource_keys, *, owner_token) -> None:
        del owner_token


class _NonFencingMultiHostResourceLocks(_FakeMultiHostResourceLocks):
    capabilities = ResourceLockBackendCapabilities(
        backend_name="fake-shared-resource-locks-without-fencing",
        supports_cross_process=True,
        supports_multi_host=True,
        atomic_multi_resource_acquire=True,
        supports_lease_renewal=True,
        supports_fencing_tokens=False,
    )


def _store(
    directory: str,
    *,
    store_type=SessionJournalPlanStore,
    tenant_id: str = "tenant-a",
    session_id: str = "session-a",
):
    journal = SQLiteSessionEventJournal(
        Path(directory) / f"{store_type.__name__}.sqlite3",
        key_provider=StaticJournalKeyProvider(
            {"test": b"d" * 32}, active_key_id="test"
        ),
    )
    return store_type(
        journal,
        JournalPrincipal.system(tenant_id),
        session_id=session_id,
    )


def _host_options(directory: str, **overrides):
    options = {
        "session_id": "session-a",
        "state_dir": directory,
        "model": MODEL,
        "stream_fn": ScriptedProvider([]).stream,
        "system_prompt": "distributed contract",
        "tools": [],
        "tenant_id": "tenant-a",
        "auto_recover": False,
    }
    options.update(overrides)
    return options


class DistributedPlanStoreContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_sqlite_is_single_host_and_strict_mode_fails_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = _store(directory)
            self.assertTrue(store.capabilities.supports_cross_process)
            self.assertFalse(store.capabilities.supports_multi_host)
            with self.assertRaisesRegex(
                DurablePlanStoreConfigurationError,
                "显式注入共享 plan_store",
            ):
                await DurableAgentHost.create(
                    **_host_options(directory, distributed_execution=True)
                )

    async def test_host_rejects_wrong_scope_and_non_atomic_injected_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            wrong_session = _store(
                directory,
                store_type=_FakeMultiHostPlanStore,
                session_id="another-session",
            )
            with self.assertRaisesRegex(
                DurablePlanStoreConfigurationError,
                "session 与 Host 不匹配",
            ):
                await DurableAgentHost.create(
                    **_host_options(directory, plan_store=wrong_session)
                )

            wrong_tenant = _store(
                directory,
                store_type=_FakeMultiHostPlanStore,
                tenant_id="another-tenant",
            )
            with self.assertRaisesRegex(
                DurablePlanStoreConfigurationError,
                "tenant 与 Host 不匹配",
            ):
                await DurableAgentHost.create(
                    **_host_options(directory, plan_store=wrong_tenant)
                )

            non_atomic = _store(directory, store_type=_NonAtomicPlanStore)
            with self.assertRaisesRegex(
                DurablePlanStoreConfigurationError,
                "原子 Fenced Append",
            ):
                await DurableAgentHost.create(
                    **_host_options(directory, plan_store=non_atomic)
                )

    async def test_caller_owned_multi_host_store_is_used_but_never_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = _store(directory, store_type=_FakeMultiHostPlanStore)
            host = await DurableAgentHost.create(
                **_host_options(
                    directory,
                    plan_store=store,
                    distributed_execution=True,
                    # Even an accidental duplicate here cannot transfer the
                    # explicit plan_store= ownership boundary to the Host.
                    owned_resources=(store,),
                )
            )
            self.assertIs(host.plan_store, store)
            self.assertIs(host.plan_workflow.store, store)
            self.assertTrue(host.distributed_execution)
            await host.plan_workflow.store.initialize(
                MultiIntentPlan(
                    "verify injected store",
                    (PlanStep("read", "read"),),
                    plan_id="injected-store-plan",
                )
            )
            self.assertEqual(store.initialize_calls, 1)
            await host.close()
            self.assertEqual(store.close_calls, 0)

    async def test_strict_worker_rejects_single_host_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = _store(directory)
            workflow = DurablePlanWorkflow(
                store=store,
                planner=None,
                policies={"read": IntentPlanPolicy("read")},
                step_executor=lambda *_args: None,
                approval_barrier=None,
            )
            with self.assertRaisesRegex(
                DurablePlanStoreConfigurationError,
                "supports_multi_host=True",
            ):
                DurablePlanWorker(workflow, distributed_execution=True)

    async def test_autonomous_custom_plan_store_fails_at_factory_not_first_prompt(
        self,
    ) -> None:
        policy = IntentPlanPolicy("read")
        planner = HybridRequestPlanner(
            {policy.intent: policy},
            lambda *_args: {
                "steps": [{"stepId": "read", "intent": policy.intent}]
            },
        )

        async def execute_step(_step, _token, **_kwargs):
            return "read"

        with tempfile.TemporaryDirectory() as directory:
            injected = _store(directory, store_type=_FakeMultiHostPlanStore)
            with self.assertRaisesRegex(ValueError, "同一事务"):
                await DurableAgentHost.create(
                    **_host_options(
                        directory,
                        plan_store=injected,
                        planner=planner,
                        plan_policies={policy.intent: policy},
                        plan_step_executor=execute_step,
                    )
                )

    async def test_distributed_dangerous_plan_requires_tool_bridge_and_lock(
        self,
    ) -> None:
        async def execute_with_context(
            _call_id,
            _arguments,
            _context,
            _cancellation,
            _update,
        ):
            return AgentToolResult(content=[])

        policy = IntentPlanPolicy(
            "entity.write",
            requires_approval=True,
            write=True,
            replay_policy="never",
            approval_roles=("approver",),
            parameter_contract=PlanParameterContract(allow_empty=True),
        )
        tool = AgentTool(
            "write_entity",
            "write",
            "write one entity",
            None,
            execute_with_context=execute_with_context,
            replay_policy="never",
            execution_mode="resource_locked",
            resolve_resource_keys=lambda _arguments: "entity:singleton",
            supports_resource_fencing=True,
        )
        verifier = StaticIdentityVerifier(
            {"worker": ("worker-test-credential", {"operator"})}
        )
        identity = await verifier.verify(
            IdentityClaim("worker", "worker-test-credential")
        )
        with tempfile.TemporaryDirectory() as directory:
            store = _store(directory, store_type=_FakeMultiHostPlanStore)
            options = _host_options(
                directory,
                tools=[tool],
                plan_store=store,
                distributed_execution=True,
                plan_policies={policy.intent: policy},
                plan_tool_bindings={policy.intent: tool.name},
                tool_identity=identity,
            )
            with self.assertRaisesRegex(ValueError, "resource_lock_backend"):
                await DurableAgentHost.create(
                    **{
                        **options,
                        "plan_store": None,
                        "distributed_execution": False,
                        "session_id": "local-multi-worker-dangerous",
                    }
                )
            with self.assertRaisesRegex(ValueError, "resource_lock_backend"):
                await DurableAgentHost.create(**options)

            with self.assertRaisesRegex(ValueError, "fencing token"):
                await DurableAgentHost.create(
                    **{
                        **options,
                        "resource_lock_backend": (
                            _NonFencingMultiHostResourceLocks()
                        ),
                    }
                )

            non_fencing_tool = replace(tool, supports_resource_fencing=False)
            with self.assertRaisesRegex(ValueError, "supports_resource_fencing"):
                await DurableAgentHost.create(
                    **{
                        **options,
                        "tools": [non_fencing_tool],
                        "resource_lock_backend": _FakeMultiHostResourceLocks(),
                    }
                )

            host = await DurableAgentHost.create(
                **{
                    **options,
                    "resource_lock_backend": _FakeMultiHostResourceLocks(),
                }
            )
            try:
                self.assertIs(host.plan_store, store)
            finally:
                await host.close()
            self.assertEqual(store.close_calls, 0)


if __name__ == "__main__":
    unittest.main()
