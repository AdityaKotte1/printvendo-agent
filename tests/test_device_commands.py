"""Doing what the console asked, and admitting when the printer is stuck.

The properties that matter are about what the *operator* and the *student* end
up seeing, not about which subprocess ran:

* a command this machine cannot do is reported failed, never silently dropped;
* restarting the agent is reported **before** it happens, because the process
  that would report it afterwards is the one being killed;
* a jammed printer closes the shop once, not once per failed job;
* a file that will not print is not a jammed printer.
"""

from pathlib import Path

import pytest

from agent import commands as commands_module
from agent.commands import HANDLERS, run_commands
from agent.printing import PrinterStuck, Task
from agent.runner import PrinterHealth, run_once


class FakeBackend:
    """A server that records what it was told."""

    def __init__(self, *, commands=None, tasks=None):
        self._commands = list(commands or [])
        self._tasks = list(tasks or [])
        self.reported_commands = []
        self.health = []
        self.reports = []

    # ── commands ──
    def next_commands(self):
        taken, self._commands = self._commands, []
        return taken

    def report_command(self, command_id, *, succeeded, error_message=None):
        self.reported_commands.append((command_id, succeeded, error_message))

    def report_printer_health(self, *, stuck, detail=None):
        self.health.append((stuck, detail))

    # ── work ──
    def next_task(self):
        return self._tasks.pop(0) if self._tasks else None

    def download(self, task, into: Path) -> Path:
        path = into / "file.pdf"
        path.write_bytes(b"%PDF-1.4\n")
        return path

    def report(self, task_id, state, *, sheets_printed=None):
        self.reports.append((task_id, state, sheets_printed))


def a_task(task_id="tsk_1") -> dict:
    return {
        "task_id": task_id,
        "document_id": "doc_1",
        "filename": "file.pdf",
        "file_url": "/v1/device/tasks/x/file",
        "page_count": 1,
        "copies": 1,
        "duplex": False,
        "colour": False,
        "page_range": None,
        "expected_sheets": 1,
        "lease_expires_at": None,
    }


# ── running commands ────────────────────────────────────────────────────────


def test_a_command_this_machine_cannot_do_is_reported_failed():
    """Never silently dropped. An estate is not upgraded all at once, and a
    request that vanishes is one an operator keeps making."""
    backend = FakeBackend()

    run_commands(backend, [{"id": "cmd_1", "command": "reticulate_splines"}])

    assert backend.reported_commands == [
        ("cmd_1", False, "this agent does not know how to reticulate_splines")
    ]


def test_a_successful_command_is_reported(monkeypatch):
    backend = FakeBackend()
    monkeypatch.setitem(
        HANDLERS, "restart_printing", (lambda: "cups was restarted", False)
    )

    run_commands(backend, [{"id": "cmd_1", "command": "restart_printing"}])

    assert backend.reported_commands == [("cmd_1", True, None)]


def test_a_failed_command_carries_what_the_machine_said(monkeypatch):
    def explode():
        raise commands_module.CommandFailed("Access denied")

    backend = FakeBackend()
    monkeypatch.setitem(HANDLERS, "restart_printing", (explode, False))

    run_commands(backend, [{"id": "cmd_1", "command": "restart_printing"}])

    assert backend.reported_commands == [("cmd_1", False, "Access denied")]


def test_restarting_the_agent_is_reported_before_it_happens(monkeypatch):
    """The process that would report it afterwards is the one being killed, so
    a command that waited would always end in silence and read as a failure."""
    said_when = []

    def restart():
        said_when.append(len(backend.reported_commands))
        return "this agent is restarting"

    backend = FakeBackend()
    monkeypatch.setitem(HANDLERS, "restart_agent", (restart, True))

    run_commands(backend, [{"id": "cmd_1", "command": "restart_agent"}])

    assert said_when == [1], "the report must already have been sent"
    assert backend.reported_commands == [("cmd_1", True, None)]


def test_one_failure_does_not_stop_the_rest(monkeypatch):
    """An operator who asked for both because printing is broken should get the
    second even if the first is what is broken."""

    def explode():
        raise commands_module.CommandFailed("no")

    backend = FakeBackend()
    monkeypatch.setitem(HANDLERS, "restart_printing", (explode, False))
    monkeypatch.setitem(HANDLERS, "restart_agent", (lambda: "ok", False))

    run_commands(
        backend,
        [
            {"id": "cmd_1", "command": "restart_printing"},
            {"id": "cmd_2", "command": "restart_agent"},
        ],
    )

    assert [c[0] for c in backend.reported_commands] == ["cmd_1", "cmd_2"]


def test_a_server_that_cannot_be_asked_does_not_stop_printing():
    """Work is the job; commands are the favour."""

    class Rude(FakeBackend):
        def next_commands(self):
            raise RuntimeError("connection refused")

    backend = Rude(tasks=[a_task()])

    handled = run_once(backend, printer="p", printer_fn=lambda *a, **k: None)

    assert handled == 1
    assert ("tsk_1", "printed", 1) in backend.reports


def test_commands_are_run_before_work_is_claimed(monkeypatch):
    """A restart asked for because nothing prints must not queue behind the
    twenty jobs that exist because nothing prints."""
    order = []
    monkeypatch.setitem(
        HANDLERS, "restart_printing", (lambda: order.append("restarted") or "ok", False)
    )

    class Watching(FakeBackend):
        def next_task(self):
            order.append("asked for work")
            return super().next_task()

    backend = Watching(commands=[{"id": "cmd_1", "command": "restart_printing"}])

    run_once(backend, printer="p", printer_fn=lambda *a, **k: None)

    assert order[0] == "restarted"


# ── a stuck printer closes the shop ─────────────────────────────────────────


def jams(*args, **kwargs):
    raise PrinterStuck("the printer still has this job after a long wait")


def refuses(*args, **kwargs):
    raise RuntimeError("Ghostscript could not read that file")


def test_a_jammed_printer_closes_the_shop():
    backend = FakeBackend(tasks=[a_task()])

    run_once(backend, printer="p", printer_fn=jams)

    assert backend.health == [
        (True, "the printer still has this job after a long wait")
    ]
    assert ("tsk_1", "failed", None) in backend.reports


def test_a_file_that_will_not_print_is_not_a_jammed_printer():
    """One student's bad PDF must not close a shop that is working."""
    backend = FakeBackend(tasks=[a_task()])

    run_once(backend, printer="p", printer_fn=refuses)

    assert backend.health == []
    assert ("tsk_1", "failed", None) in backend.reports


def test_the_shop_is_closed_once_not_once_per_job():
    """Otherwise the server raises and stands down one alert all afternoon."""
    backend = FakeBackend(tasks=[a_task("tsk_1"), a_task("tsk_2"), a_task("tsk_3")])

    run_once(backend, printer="p", printer_fn=jams)

    assert [stuck for stuck, _ in backend.health] == [True]


def test_paper_coming_out_reopens_the_shop():
    health = PrinterHealth()
    backend = FakeBackend(tasks=[a_task("tsk_1")])
    run_once(backend, printer="p", printer_fn=jams, health=health)

    backend._tasks = [a_task("tsk_2")]
    run_once(backend, printer="p", printer_fn=lambda *a, **k: None, health=health)

    assert [stuck for stuck, _ in backend.health] == [True, False]


def test_a_working_shop_is_not_told_it_is_working():
    """A request per print saying nothing new."""
    backend = FakeBackend(tasks=[a_task("tsk_1"), a_task("tsk_2")])

    run_once(backend, printer="p", printer_fn=lambda *a, **k: None)

    assert backend.health == []


def test_a_health_report_that_fails_is_tried_again_next_job():
    """A shop left selling because one request failed is the whole thing this
    exists to prevent."""

    class Deaf(FakeBackend):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.attempts = 0

        def report_printer_health(self, *, stuck, detail=None):
            self.attempts += 1
            raise RuntimeError("connection refused")

    backend = Deaf(tasks=[a_task("tsk_1"), a_task("tsk_2")])

    run_once(backend, printer="p", printer_fn=jams)

    assert backend.attempts == 2


def test_the_task_is_still_a_task():
    """`Task.from_response` is what the loop feeds the printer, and a jam must
    not change that contract."""
    assert Task.from_response(a_task()).task_id == "tsk_1"


@pytest.mark.parametrize("kind", ["restart_printing", "restart_agent"])
def test_both_documented_commands_have_a_handler(kind):
    """The server can send exactly these two; a machine that knew only one
    would report the other as unknown and nobody would notice until a shop
    needed it."""
    assert kind in HANDLERS


# ── a restart that cannot happen must not report success ────────────────────
#
# `restart_agent` reports success before it acts, because the process that
# would report it afterwards is the one being killed. That is only honest if
# the supervisor is actually there: a machine whose unit or scheduled task is
# missing would otherwise detach a command that does nothing and leave the
# console saying "succeeded" about a shop that never came back.


def test_restart_agent_refuses_when_no_supervisor_knows_it(monkeypatch):
    monkeypatch.setattr(commands_module, "_supervises_us", lambda: False)

    with pytest.raises(commands_module.CommandFailed) as raised:
        commands_module.restart_agent.precheck()

    assert "not running as a service" in str(raised.value)


def test_restart_agent_detaches_when_the_supervisor_is_there(monkeypatch):
    detached = []
    monkeypatch.setattr(commands_module, "_supervises_us", lambda: True)
    monkeypatch.setattr(commands_module, "_detach", lambda cmd: detached.append(cmd))

    commands_module.restart_agent()

    assert detached, "the restart must actually be started"


def test_an_unsupervised_agent_is_reported_failed(monkeypatch):
    """The whole point: the operator is told, rather than told it worked."""
    monkeypatch.setattr(commands_module, "_supervises_us", lambda: False)
    backend = FakeBackend()

    run_commands(backend, [{"id": "cmd_1", "command": "restart_agent"}])

    assert [(c[0], c[1]) for c in backend.reported_commands] == [("cmd_1", False)]
    assert "not running as a service" in backend.reported_commands[-1][2]
