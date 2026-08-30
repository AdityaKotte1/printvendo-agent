"""Choosing between several machines at one shop.

A SaaS xerox shop runs two mono printers and a colour one off a single agent.
These are the rules that decide which one a job goes to; they are pure so they
can be exercised without a printer, and the queue depth is injected for the
same reason.
"""

from agent.pools import Pools, pick, pool_for

TWO_MONO_ONE_COLOUR = Pools(bw=["Mono-1", "Mono-2"], colour=["Colour-1"])


def idle(_name: str) -> int:
    return 0


# ── which pool ──────────────────────────────────────────────────────────────


def test_a_mono_job_goes_to_the_mono_machines():
    assert pool_for(TWO_MONO_ONE_COLOUR, colour=False) == ["Mono-1", "Mono-2"]


def test_a_colour_job_goes_to_the_colour_machines():
    assert pool_for(TWO_MONO_ONE_COLOUR, colour=True) == ["Colour-1"]


def test_colour_work_never_falls_back_to_a_mono_machine():
    """It would come out grey on a job the student paid colour prices for --
    a refund and a complaint, where waiting is merely a wait."""
    mono_only = Pools(bw=["Mono-1"], colour=[])

    assert pool_for(mono_only, colour=True) == []


def test_mono_work_may_use_a_colour_machine_when_that_is_all_there_is():
    """A shop that listed only colour machines has only colour machines. It
    prints correctly; it just costs the owner more toner than they charged
    for, which is their business and not a wrong print."""
    colour_only = Pools(bw=[], colour=["Colour-1"])

    assert pool_for(colour_only, colour=False) == ["Colour-1"]


def test_a_shop_with_one_printer_is_unchanged():
    """Every kiosk in the field is configured this way and none of them should
    have to be touched."""
    single = Pools(fallback="EPSON_L6460")

    assert pool_for(single, colour=False) == ["EPSON_L6460"]
    assert pool_for(single, colour=True) == ["EPSON_L6460"]


def test_pools_win_over_the_single_printer_once_they_are_set():
    both = Pools(bw=["Mono-1"], colour=["Colour-1"], fallback="Old-Default")

    assert pool_for(both, colour=False) == ["Mono-1"]


# ── which machine in the pool ───────────────────────────────────────────────


def test_the_second_printer_takes_the_job_while_the_first_is_busy():
    """The whole point of a pool: a rush does not queue behind one machine."""
    depths = {"Mono-1": 4, "Mono-2": 0}

    assert pick(["Mono-1", "Mono-2"], depths.__getitem__) == "Mono-2"


def test_two_idle_machines_go_to_the_one_listed_first():
    """Deterministic, and it lets an owner say which machine they would rather
    wear out first."""
    assert pick(["Mono-1", "Mono-2"], idle) == "Mono-1"


def test_the_least_loaded_wins_even_when_all_are_busy():
    depths = {"Mono-1": 9, "Mono-2": 3, "Mono-3": 5}

    assert pick(["Mono-1", "Mono-2", "Mono-3"], depths.__getitem__) == "Mono-2"


def test_a_printer_whose_queue_cannot_be_read_is_passed_over():
    """Not an empty queue -- `waiting` already holds that rule for one machine.
    A printer that cannot be asked is the last resort, not the first choice."""

    def unreadable(name: str) -> int:
        if name == "Mono-1":
            raise OSError("lpstat exploded")
        return 2

    assert pick(["Mono-1", "Mono-2"], unreadable) == "Mono-2"


def test_an_empty_pool_chooses_nothing():
    """The caller refuses the job rather than inventing a machine for it."""
    assert pick([], idle) is None
