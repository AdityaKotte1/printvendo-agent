"""The PIN that gets somebody back to the desktop."""

import json

import pytest

from display import lock


@pytest.fixture
def where(tmp_path):
    return tmp_path / "display.json"


def test_the_right_pin_opens_it():
    made = lock.make("2468")

    assert made.opens_with("2468") is True


def test_a_wrong_pin_does_not():
    made = lock.make("2468")

    assert made.opens_with("2469") is False
    assert made.opens_with("") is False
    assert made.opens_with("24680") is False


def test_the_pin_is_never_written_down(where):
    """This file sits on a shop PC that students stand in front of. A plaintext
    PIN in it is a PIN written on the counter."""
    lock.save(lock.make("2468"), where)

    written = where.read_text(encoding="utf-8")

    assert "2468" not in written
    assert set(json.loads(written)) == {"salt", "digest"}


def test_two_machines_with_the_same_pin_do_not_share_a_digest():
    """Salted, so one leaked config file does not read across the estate."""
    a = lock.make("2468")
    b = lock.make("2468")

    assert a.digest != b.digest
    assert a.opens_with("2468") and b.opens_with("2468")


def test_it_survives_being_written_and_read_back(where):
    lock.save(lock.make("2468"), where)

    assert lock.load(where).opens_with("2468") is True


def test_no_file_means_no_lock_rather_than_a_lock_nobody_can_open(where):
    """A screen that cannot be dismissed is a shop PC that has to be
    power-cycled to use."""
    assert lock.load(where) is None


def test_a_damaged_file_is_the_same_as_no_file(where):
    where.write_text("{not json", encoding="utf-8")

    assert lock.load(where) is None


# ── how slowly it may be guessed ────────────────────────────────────────────


def test_the_first_mistypes_are_free():
    """Somebody who has just fat-fingered a four-digit PIN should not be
    punished for it."""
    assert lock.wait_after(0) == 0
    assert lock.wait_after(1) == 0


def test_the_wait_grows_with_the_guessing():
    assert lock.wait_after(2) < lock.wait_after(3) < lock.wait_after(4)


def test_the_wait_stops_growing_rather_than_locking_the_shop_out_for_ever():
    """The shopkeeper has to get in too, and they mistype like anyone else."""
    assert lock.wait_after(50) == lock.wait_after(len(lock.BACKOFF) - 1)
