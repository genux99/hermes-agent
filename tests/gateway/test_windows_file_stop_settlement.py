"""Exercise chat /stop, the public tool and the real local Windows Shell together."""
import asyncio
import json

import threading


import pytest

from gateway.platforms.base import MessageEvent
from tests.gateway.test_agent_loop_stopped_hook import _make_runner, _make_source
from gateway.session import build_session_key


@pytest.mark.platforms('windows')
@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['staged', 'publishing', 'locked'])
async def test_windows_file_stop_settlement(tmp_path, monkeypatch, phase):
    from run_agent import AIAgent
    from tools import file_operations, file_tools, interrupt

    target = tmp_path / 'target with spaces.txt'

    runner, source = _make_runner(), _make_source()
    key = build_session_key(source)
    tasks = []
    runner._track_background_task = lambda coro: tasks.append(asyncio.create_task(coro))
    agent = AIAgent(model='fixture', provider='custom', base_url='http://127.0.0.1:9/v1',
                    api_key='fixture', enabled_toolsets=['file'], quiet_mode=True,
                    skip_context_files=True, skip_memory=True, skip_background_review=True)
    runner._running_agents[key] = agent
    ready, release = threading.Event(), threading.Event()
    execute = file_operations.ShellFileOperations._exec

    def hold():
        ready.set()
        assert release.wait(25)

    def gated(self, command, *args, **kwargs):
        if phase != 'staged' and 'mv -f' in command:
            hold()
        result = execute(self, command, *args, **kwargs)
        if phase == 'staged' and 'cat > "$tmp"' in command:
            hold()
        return result

    def worker():
        agent._execution_thread_id = threading.get_ident()
        try:
            return json.loads(file_tools.write_file_tool(str(target), 'after'))
        finally:
            interrupt.set_interrupt(False)

    monkeypatch.setattr(file_operations.ShellFileOperations, '_exec', gated)
    pending = asyncio.create_task(asyncio.to_thread(worker))
    held = None
    try:
        wait_ready = asyncio.create_task(asyncio.to_thread(ready.wait, 30))
        done, _ = await asyncio.wait({wait_ready, pending}, return_when=asyncio.FIRST_COMPLETED)
        assert pending not in done, pending.result() if pending.done() else None
        assert wait_ready.result()
        event = MessageEvent(text='/stop', source=source)
        reply = await asyncio.wait_for(runner._busy_stop_command(event, key, source), 5)
        assert ('settling' in reply.text) is (phase != 'staged')
        assert not target.exists()
        if phase == 'locked':
            staged, = tmp_path.glob('.hermes-tmp.*')
            held = staged.open('rb')
    finally:
        release.set()
        try:
            result = await asyncio.wait_for(pending, 35)
            if tasks:
                await asyncio.wait_for(asyncio.gather(*tasks), 5)
        finally:
            if held:
                held.close()
            agent._end_session_on_close = False
            agent.close()
    if phase == 'staged':
        assert result.get('error') and result.get('bytes_written') == 0
        assert not target.exists()
        assert not tasks
    elif phase == 'locked':
        assert result.get('error')
        assert not target.exists()
        assert 'unknown' in runner.adapters[source.platform].send.await_args.args[1]
    else:
        assert not result.get('error'), result
        assert result.get('bytes_written') == 5
        assert target.read_text(encoding='utf-8') == 'after'
        assert runner.adapters[source.platform].send.await_args.args == (source.chat_id, 'File write completed.')
    if phase != 'locked':
        assert not list(tmp_path.glob('.hermes-tmp.*'))
