"""Every option reaches the printer, on both platforms.

This file exists because the agent it replaces did not do that. On Linux it
built a correct `lp` command; on Windows it computed a DEVMODE with copies,
duplex and colour in it, **never applied it**, and then printed by shelling out
to `cmd /c start /print`, which uses the default printer with default settings
and has no notion of a page range at all. A student was charged for colour,
double-sided, pages 4-6, and got whatever the machine felt like.

So the mapping is a pure function per platform and it is tested the same way
for both: same task, same expectations, one test body. A new option cannot be
added to one platform and forgotten on the other without a test failing.
"""

import pytest

from agent.printing import Task, build_cups_command, build_windows_command

BUILDERS = {
    "cups": build_cups_command,
    "windows": build_windows_command,
}


def task(**kwargs) -> Task:
    defaults = {
        "task_id": "tsk_1",
        "document_id": "doc_1",
        "filename": "essay.pdf",
        "page_count": 10,
        "copies": 1,
        "duplex": False,
        "colour": False,
        "page_range": None,
        "expected_sheets": 10,
    }
    return Task(**{**defaults, **kwargs})


def command(platform: str, **kwargs) -> str:
    """The built command as one string, for asking "is this in there"."""
    built = BUILDERS[platform](task(**kwargs), file_path="/tmp/essay.pdf", printer="HP-01")
    return " ".join(built)


@pytest.mark.parametrize("platform", sorted(BUILDERS))
class TestEveryOptionArrives:
    """Run against both platforms. A platform that grows an option the other
    lacks fails here, which is the point."""

    def test_the_printer_is_named(self, platform):
        """Never the default printer. A shop can have more than one, and the
        old Windows path printed to whichever Windows thought was default."""
        assert "HP-01" in command(platform)

    def test_copies_are_passed(self, platform):
        assert "3" in command(platform, copies=3)

    def test_one_copy_is_not_three(self, platform):
        """Guards the test above from passing on a coincidence."""
        assert "3" not in command(platform, copies=1)

    def test_duplex_is_asked_for(self, platform):
        built = command(platform, duplex=True).lower()
        assert "duplex" in built or "two-sided" in built

    def test_single_sided_is_asked_for_explicitly(self, platform):
        """Not left to the printer's default: a shop that leaves duplex on in
        the driver would silently halve every job's paper and charge for it."""
        built = command(platform, duplex=False).lower()
        assert "duplex" in built or "one-sided" in built

    def test_colour_is_asked_for(self, platform):
        assert command(platform, colour=True) != command(platform, colour=False)

    def test_a_page_range_is_passed(self, platform):
        """The option the old Windows path did not attempt at all."""
        built = command(platform, page_range="4-6")
        assert "4" in built and "6" in built

    def test_a_job_with_no_page_range_prints_everything(self, platform):
        """No range must not become an empty range, which prints nothing."""
        built = command(platform, page_range=None).lower()
        assert "pagelist" not in built and "page-ranges" not in built

    def test_the_file_is_the_last_word(self, platform):
        built = BUILDERS[platform](
            task(), file_path="/tmp/essay.pdf", printer="HP-01"
        )
        assert built[-1] == "/tmp/essay.pdf"


# ── the platform-specific detail ────────────────────────────────────────────


def test_cups_asks_for_mono_ink_as_well_as_mono_mode():
    """`print-color-mode=monochrome` alone is not enough on HP drivers -- the
    shop's Smart Tank prints colour anyway unless `Ink=MONO` is set too. This
    was learned on real hardware and must not be tidied away."""
    built = command("cups", colour=False)

    assert "print-color-mode=monochrome" in built
    assert "Ink=MONO" in built


def test_cups_sorts_a_jumbled_page_range():
    """CUPS requires ascending order and refuses "12-17,1"."""
    assert "page-ranges=1,12-17" in command("cups", page_range="12-17,1")


def test_windows_prints_through_ghostscript_rather_than_a_shell_verb():
    """`cmd /c start /print` hands the file to whatever app is registered for
    PDFs, with no options and no way to know when it finished. Ghostscript is
    already a dependency of this system and takes every option."""
    built = command("windows")

    assert "mswinpr2" in built
    assert "%printer%HP-01" in built


def test_windows_never_opens_a_dialog():
    """A kiosk has nobody to click OK."""
    built = command("windows")

    assert "-dNOPAUSE" in built and "-dBATCH" in built


def test_windows_expands_a_page_range_for_ghostscript():
    """`-sPageList` takes an explicit list, not CUPS's range syntax."""
    assert "-sPageList" in command("windows", page_range="4-6")
    assert "4,5,6" in command("windows", page_range="4-6")


def test_windows_runs_ghostscript_under_safer():
    """The same rule the backend's PDF pipeline follows: a PDF is somebody
    else's file, and Ghostscript will execute what is in it if allowed to."""
    assert "-dSAFER" in command("windows")
    assert "-dNOSAFER" not in command("windows")
