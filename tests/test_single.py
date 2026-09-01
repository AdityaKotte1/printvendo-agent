"""One agent per machine.

Two agents on one Pi both claim work. Nothing prints twice -- the server hands
out a task once -- but they submit independently, so the shop's queue ends up
several jobs deep with no gap, and a student walking up mid-stream takes
somebody else's sheets.

It happened at a live shop. Somebody ran the agent by hand over SSH to watch it
work and left it running beside the service.
"""

import pytest

from agent.single import AlreadyRunning, only_one_agent


def test_one_agent_takes_the_lock(tmp_path):
    lock = tmp_path / "agent.lock"

    with only_one_agent(lock):
        assert lock.exists()


def test_a_second_agent_is_refused_while_the_first_holds_it(tmp_path):
    """The whole point. Without this the second one starts, prints, and the
    only symptom is students receiving each other's pages."""
    lock = tmp_path / "agent.lock"

    with only_one_agent(lock):
        with pytest.raises(AlreadyRunning):
            with only_one_agent(lock):
                pass


def test_the_lock_is_free_once_the_first_agent_stops(tmp_path):
    """A restart must not need a person to delete a file. The kernel releases
    an flock when the holder dies, which is why this is a lock and not a PID
    file -- a PID file survives a crash and the next start has to guess whether
    the process it names is alive."""
    lock = tmp_path / "agent.lock"

    with only_one_agent(lock):
        pass

    with only_one_agent(lock):
        assert True


def test_the_holder_writes_its_pid_for_somebody_reading_over_ssh(tmp_path):
    """Never read back by this code -- the lock is the truth. It is there so a
    person who finds the file can tell which process to look at."""
    import os

    lock = tmp_path / "agent.lock"

    with only_one_agent(lock):
        assert lock.read_text().strip() == str(os.getpid())


def test_run_refuses_to_start_while_another_agent_holds_the_lock(tmp_path, monkeypatch):
    """The wiring, not just the lock.

    Written after a mutation test: deleting `with only_one_agent()` from `_run`
    broke nothing, because every test here exercised the lock directly and none
    of them went through the command. A guard nothing reaches is not a guard.
    """
    from agent.__main__ import _run
    from agent.config import Config

    lock = tmp_path / "agent.lock"
    monkeypatch.setenv("PRINTVENDO_LOCK", str(lock))
    monkeypatch.setattr(
        Config, "load", classmethod(lambda cls, path=None: Config(token="t", printer="P"))
    )

    with only_one_agent(lock):
        assert _run(once=True) == 1
