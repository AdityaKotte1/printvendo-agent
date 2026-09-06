"""The document the shop screen is given.

Pure, so it is tested without a browser, a printer or a screen -- the same
reason `build_windows_command` is a function rather than a side effect.
"""

from datetime import UTC, datetime

from agent.status import Job, Snapshot, document

NOW = datetime(2026, 9, 7, 10, 30, tzinfo=UTC)


def test_a_shop_that_can_take_work_says_it_is_online():
    doc = document(
        Snapshot(
            kiosk_name="SRK Xerox",
            sheets_remaining=90,
            paper_capacity=250,
            queue_depth=0,
            connected=True,
            printer_ok=True,
            updated_at=NOW,
        ),
        now=NOW,
    )

    assert doc["online"] is True
    assert doc["kiosk_name"] == "SRK Xerox"
    assert doc["paper"] == {"remaining": 90, "capacity": 250, "fraction": 0.36}


def test_a_printer_that_is_stuck_is_not_online_even_with_a_line():
    """Online is the question a student in front of it is asking -- can this
    take my job -- not whether the network is up."""
    doc = document(Snapshot(connected=True, printer_ok=False), now=NOW)

    assert doc["online"] is False
    assert doc["connected"] is True
    assert doc["printer_ok"] is False


def test_a_lost_connection_keeps_the_last_figures():
    """A shop that has been printing all day and lost its line for a minute
    shows its real paper count, greyed by the page. A blank reads as broken."""
    doc = document(
        Snapshot(
            kiosk_name="SRK Xerox",
            sheets_remaining=90,
            paper_capacity=250,
            connected=False,
            updated_at=NOW,
        ),
        now=NOW,
    )

    assert doc["online"] is False
    assert doc["paper"]["remaining"] == 90
    assert doc["as_of"] == NOW.isoformat()


def test_before_the_first_heartbeat_nothing_is_invented():
    """The screen goes up when the agent starts, which is before the first
    heartbeat returns. Every field is optional so the page can render a
    starting shop rather than a black rectangle."""
    doc = document(Snapshot(), now=NOW)

    assert doc["kiosk_name"] is None
    assert doc["paper"] is None
    assert doc["queue_depth"] is None
    assert doc["job"] is None
    assert doc["online"] is False


def test_a_tray_with_no_capacity_draws_no_bar_rather_than_a_full_one():
    """A shop whose tray size was never set has no denominator. Full is the one
    reading that stops somebody refilling, so the bar is absent instead."""
    doc = document(Snapshot(sheets_remaining=40, paper_capacity=0), now=NOW)

    assert doc["paper"] is None


def test_more_paper_than_the_tray_holds_does_not_overflow_the_bar():
    """Somebody refilling past the recorded capacity is ordinary. A fraction
    above one would draw a bar out of its own track."""
    doc = document(Snapshot(sheets_remaining=400, paper_capacity=250), now=NOW)

    assert doc["paper"]["fraction"] == 1.0
    assert doc["paper"]["remaining"] == 400


def test_a_job_on_the_machine_is_reported_by_state_and_sheets():
    doc = document(Snapshot(job=Job(state="printing", sheets=12)), now=NOW)

    assert doc["job"] == {"state": "printing", "sheets": 12}


# ── the rule that matters ───────────────────────────────────────────────────


def test_the_document_carries_no_filename_anywhere():
    """`api.download` already saves a job as `prt_abc.pdf` rather than under the
    student's own name. A forty-inch screen above a counter reading "Medical
    Results Ravi Kumar.pdf" is that same leak, enlarged and in public.

    Asserted against the whole document rather than against a named field,
    because the point is that no such field exists -- a display cannot render
    what it is never sent, however the page is later edited.
    """
    doc = document(
        Snapshot(
            kiosk_name="SRK Xerox",
            job=Job(state="printing", sheets=12),
            connected=True,
            updated_at=NOW,
        ),
        now=NOW,
    )

    flat = str(doc).lower()
    assert "filename" not in flat
    assert ".pdf" not in flat
    assert "document_id" not in flat


# ── reaching the screen with a release ──────────────────────────────────────


def test_the_document_says_which_build_served_it():
    """`update_agent` from the console replaces the display's page along with
    the agent -- they ship in one wheel. But Edge is already holding the old
    page, and nobody is driving to the shop to press F5, so the page watches
    this and reloads itself when it changes."""
    doc = document(Snapshot(agent_version="1.7.0"), now=NOW)

    assert doc["agent_version"] == "1.7.0"


def test_a_version_that_is_not_known_is_absent_rather_than_guessed():
    """A page told "unknown" once and "1.7.0" next would reload for no reason,
    on a screen above a counter, in front of a student."""
    doc = document(Snapshot(), now=NOW)

    assert doc["agent_version"] is None
