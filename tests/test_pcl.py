"""Rendering to PCL, so printing needs no printer driver and no desktop.

`mswinpr2` asks the driver for a DEVMODE, and a driver that wants to show UI to
do it blocks for ever in a session with no desktop. Everything the agent does
to avoid that -- running as the signed-in user, at logon, with automatic
sign-in, with a repeat trigger -- is a workaround for that one call.

A `pxl` device needs neither. These tests hold the mapping to the same standard
as the other two: every option a student paid for reaches the printer.
"""

from types import SimpleNamespace

import pytest

from agent.printing import build_pcl_command


def task(**kwargs):
    defaults = {
        "task_id": "prt_abc",
        "colour": False,
        "duplex": False,
        "copies": 1,
        "page_range": None,
    }
    return SimpleNamespace(**{**defaults, **kwargs})


def build(**kwargs) -> list[str]:
    return build_pcl_command(
        task(**kwargs), file_path="job.pdf", out_path="out.pcl", ghostscript="gs"
    )


# ── colour is the device, not a hint ────────────────────────────────────────


def test_a_colour_job_renders_on_the_colour_device():
    """`mswinpr2` asked for it with `<< /BitsPerPixel 24 >>`, which its own
    comment admits some drivers ignore -- so a student could pay colour prices
    and collect grey. The device cannot be ignored."""
    assert "-sDEVICE=pxlcolor" in build(colour=True)


def test_a_black_and_white_job_renders_on_the_mono_device():
    assert "-sDEVICE=pxlmono" in build(colour=False)


def test_colour_is_decided_once_and_not_also_asked_for():
    """Two ways of saying the same thing is how they come to disagree."""
    assert not any(part.startswith("<< /BitsPerPixel") for part in build(colour=True))


# ── duplex, stated in both directions ───────────────────────────────────────


def test_a_double_sided_job_says_so():
    assert "<< /Duplex true /Tumble false >> setpagedevice" in build(duplex=True)


def test_a_single_sided_job_says_so_too():
    """Never left unstated: a printer in whatever mode the last job used is how
    somebody's single-sided job comes out on both sides."""
    assert "<< /Duplex false >> setpagedevice" in build(duplex=False)


# ── the pages that were paid for ────────────────────────────────────────────


def test_a_page_range_becomes_an_explicit_list():
    """-sPageList does not take CUPS's range syntax."""
    assert "-sPageList=1,2,3,7" in build(page_range="1-3,7")


def test_no_range_selects_nothing_and_therefore_everything():
    assert not any(part.startswith("-sPageList") for part in build())


# ── copies are deliberately absent ──────────────────────────────────────────


def test_copies_are_not_in_the_command_at_all():
    """PCL carries a copies attribute printers honour inconsistently, so
    `rawprint.spool` sends the document once per copy instead. One copy of a
    job somebody paid three copies for is the silent wrongness this module
    exists to end -- and asking twice, here and there, is how the two come to
    disagree."""
    rendered = " ".join(build(copies=3))

    assert "NumCopies" not in rendered
    assert "-dNumCopies" not in rendered


# ── the rest ────────────────────────────────────────────────────────────────


def test_the_output_goes_where_it_was_asked_to():
    assert "-sOutputFile=out.pcl" in build()


def test_the_file_comes_after_a_dash_f():
    """-f ends the -c postscript and says the rest is input, so a filename
    beginning with a dash cannot be read as a switch."""
    parts = build()

    assert parts[-2:] == ["-f", "job.pdf"]


def test_somebody_elses_pdf_is_rendered_under_safer():
    """This is the machine sitting in a shop, so it matters more here."""
    assert "-dSAFER" in build()


def test_nothing_waits_for_a_person():
    for switch in ("-dNOPAUSE", "-dBATCH", "-dNoCancel"):
        assert switch in build()


# ── the reason this exists ──────────────────────────────────────────────────


def test_no_driver_and_no_window_station_are_involved():
    """The whole point. `mswinpr2` is what needs a desktop; a pxl device writes
    PCL that the printer's own firmware reads."""
    rendered = " ".join(build())

    assert "mswinpr2" not in rendered
    assert "%printer%" not in rendered


@pytest.mark.parametrize("colour", [True, False])
def test_every_job_names_a_pxl_device_whatever_it_is(colour):
    devices = [part for part in build(colour=colour) if part.startswith("-sDEVICE=")]

    assert len(devices) == 1
    assert devices[0].startswith("-sDEVICE=pxl")


# ── the options reach the bytes, not just the command line ──────────────────
#
# Everything above asserts what is in the argument list. That is how a flag
# comes to be present and do nothing -- `pi-agent` filled in a DEVMODE it never
# applied, and every option a student paid for was silently dropped on a job
# they had been charged for.
#
# These render a real PDF with a real Ghostscript and compare the output, so
# "duplex was asked for" and "duplex changed what the printer will be sent" are
# not the same claim. Skipped where Ghostscript is absent, which is CI.


def _ghostscript():
    try:
        from agent.printing import ghostscript_path

        return ghostscript_path()
    except RuntimeError:
        return None


needs_ghostscript = pytest.mark.skipif(
    _ghostscript() is None, reason="Ghostscript is not installed here"
)


@pytest.fixture
def two_page_pdf(tmp_path):
    import subprocess

    pdf = tmp_path / "two.pdf"
    subprocess.run(
        [
            _ghostscript(), "-dNOPAUSE", "-dBATCH", "-sDEVICE=pdfwrite",
            f"-sOutputFile={pdf}", "-c",
            "72 700 moveto showpage 72 700 moveto showpage",
        ],
        check=True, capture_output=True, timeout=120,
    )
    return pdf


def _render(pdf, tmp_path, name, **options) -> bytes:
    import subprocess

    out = tmp_path / f"{name}.pcl"
    subprocess.run(
        build_pcl_command(
            task(**options),
            file_path=str(pdf),
            out_path=str(out),
            ghostscript=_ghostscript(),
        ),
        check=True, capture_output=True, timeout=120,
    )
    return out.read_bytes()


@needs_ghostscript
def test_what_comes_out_is_actually_pcl(two_page_pdf, tmp_path):
    data = _render(two_page_pdf, tmp_path, "plain")

    assert b"HP-PCL XL" in data[:200]


@needs_ghostscript
def test_asking_for_duplex_changes_what_the_printer_is_sent(two_page_pdf, tmp_path):
    """It is carried in the PCL-XL body rather than the PJL header, so reading
    the first few hundred bytes says nothing. Compare the whole stream."""
    both = _render(two_page_pdf, tmp_path, "duplex", duplex=True)
    one = _render(two_page_pdf, tmp_path, "single", duplex=False)

    assert both != one


@needs_ghostscript
def test_colour_and_mono_render_differently(two_page_pdf, tmp_path):
    colour = _render(two_page_pdf, tmp_path, "colour", colour=True)
    mono = _render(two_page_pdf, tmp_path, "mono", colour=False)

    assert colour != mono
    assert b"RENDERMODE=COLOR" in colour[:200]
    assert b"RENDERMODE=GRAYSCALE" in mono[:200]


@needs_ghostscript
def test_a_page_range_sends_fewer_pages(two_page_pdf, tmp_path):
    """The student paid for one page of a two-page document."""
    one = _render(two_page_pdf, tmp_path, "first", page_range="1")
    everything = _render(two_page_pdf, tmp_path, "all")

    assert len(one) < len(everything)
