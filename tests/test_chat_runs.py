from __future__ import annotations

import asyncio
import json
import unittest

from hermes_chat.chat_runs import ChatRunManager
from hermes_chat.gateway_session import GatewaySession, _translate_event


class FakeHistory:
    def __init__(self) -> None:
        self.messages = {
            1: {
                "id": 1,
                "chat_id": "chat-1",
                "role": "assistant",
                "content": "",
                "tool_steps": [],
                "stream_seq": 0,
            },
            2: {
                "id": 2,
                "chat_id": "chat-1",
                "role": "assistant",
                "content": "",
                "tool_steps": [],
                "stream_seq": 0,
            },
        }

    def get_message(self, message_id, user_id):
        return self.messages.get(message_id)

    def update_message(self, message_id, user_id, content, tool_steps, stream_seq=None):
        self.messages[message_id].update(
            content=content,
            tool_steps=[dict(step) for step in tool_steps],
            stream_seq=stream_seq,
        )


class FakeSessionStore:
    def __init__(self) -> None:
        self.saved = []

    def set_hermes_session_id(self, chat_id, session_id):
        self.saved.append((chat_id, session_id))


class FakeSession:
    def __init__(self) -> None:
        self.queue = asyncio.Queue()
        self.hermes_session_id = "hermes-1"
        self.submitted = []
        self.pending_gate = None
        self.gate_responses = []
        self.interrupted = False

    def submit(self, message):
        self.submitted.append(message)

    def set_pending_gate(self, gate):
        self.pending_gate = gate

    def respond_gate(self, kind, gate_id, value):
        self.gate_responses.append((kind, gate_id, value))

    def interrupt(self):
        self.interrupted = True


class ChatRunManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.history = FakeHistory()
        self.session_store = FakeSessionStore()
        self.manager = ChatRunManager(self.history, self.session_store)
        self.session = FakeSession()

    async def asyncTearDown(self):
        await self.manager.shutdown()

    async def start_run(self, message_id=1):
        return await self.manager.start(
            chat_id="chat-1",
            user_id="user-1",
            assistant_message_id=message_id,
            message="hello",
            session=self.session,
            submit=self.session.submit,
        )

    @staticmethod
    def frame(event_type, payload=None):
        return {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {"type": event_type, "payload": payload or {}},
        }

    async def wait_for(self, predicate):
        for _ in range(100):
            if predicate():
                return
            await asyncio.sleep(0.01)
        self.fail("condition was not reached")

    async def collect(self, run_id, after=0):
        payloads = []
        async for chunk in self.manager.subscribe(run_id, "user-1", after):
            data_line = next(line for line in chunk.decode().splitlines() if line.startswith("data: "))
            payloads.append(json.loads(data_line[6:]))
        return payloads

    async def test_run_persists_before_replay_and_completes(self):
        run = await self.start_run()
        await self.session.queue.put(self.frame("message.delta", {"text": "answer"}))
        await self.session.queue.put(self.frame("turn.complete"))
        await run.task

        self.assertEqual(self.session.submitted, ["hello"])
        self.assertEqual(self.history.messages[1]["content"], "answer")
        self.assertEqual(self.history.messages[1]["stream_seq"], 2)
        self.assertEqual(run.status, "complete")
        events = await self.collect(run.run_id)
        self.assertEqual([event["seq"] for event in events], [1, 2])
        self.assertEqual(events[0]["text"], "answer")

    async def test_refresh_resumes_after_persisted_sequence(self):
        run = await self.start_run()
        await self.session.queue.put(self.frame("message.delta", {"text": "first "}))
        await self.wait_for(lambda: run.seq == 1)

        first_subscription = self.manager.subscribe(run.run_id, "user-1", 0)
        first = await first_subscription.__anext__()
        await first_subscription.aclose()
        self.assertIn('"seq": 1', first.decode())

        await self.session.queue.put(self.frame("message.delta", {"text": "second"}))
        await self.session.queue.put(self.frame("turn.complete"))
        await run.task

        resumed = await self.collect(run.run_id, after=1)
        self.assertEqual([event["seq"] for event in resumed], [2, 3])
        self.assertEqual(self.history.messages[1]["content"], "first second")

    async def test_duplicate_submission_is_rejected(self):
        await self.start_run()
        with self.assertRaises(RuntimeError):
            await self.start_run(message_id=2)
        self.assertEqual(self.session.submitted, ["hello"])

    async def test_flat_gateway_event_payload_is_translated(self):
        event = _translate_event({"method": "event", "params": {"type": "message.delta", "text": "visible"}})
        self.assertEqual(event, {"type": "text", "text": "visible"})

    async def test_prompt_submit_error_is_raised(self):
        session = object.__new__(GatewaySession)
        session.last_active = 0
        session.ensure_session = lambda: "session-1"
        session._call = lambda method, params: {"error": {"message": "provider unavailable"}}

        with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
            session.submit("hello")

    async def test_cancel_interrupts_the_active_session(self):
        run = await self.start_run()

        cancelled = await self.manager.cancel("chat-1", "user-1")

        self.assertIs(cancelled, run)
        self.assertTrue(self.session.interrupted)

    async def test_completed_runs_are_reaped_after_ttl(self):
        self.manager.completed_ttl = 0
        completed = await self.start_run()
        await self.session.queue.put(self.frame("turn.complete"))
        await completed.task
        completed.updated_at -= 1

        await self.start_run(message_id=2)

        self.assertIsNone(self.manager.get(completed.run_id, "user-1"))

    async def test_gate_resolution_keeps_collector_alive(self):
        run = await self.start_run()
        await self.session.queue.put(
            self.frame(
                "approval.request",
                {"request_id": "gate-1", "prompt": "Proceed?", "choices": ["yes", "no"]},
            )
        )
        await self.wait_for(lambda: run.status == "waiting_for_gate")

        await self.manager.resolve_gate(
            chat_id="chat-1",
            user_id="user-1",
            gate_id="gate-1",
            gate_kind="approval",
            choice="yes",
        )
        await self.session.queue.put(self.frame("message.delta", {"text": "done"}))
        await self.session.queue.put(self.frame("turn.complete"))
        await run.task

        self.assertEqual(self.session.gate_responses, [("approval", "gate-1", "yes")])
        self.assertEqual(self.history.messages[1]["content"], "done")
        self.assertEqual(run.status, "complete")


if __name__ == "__main__":
    unittest.main()
