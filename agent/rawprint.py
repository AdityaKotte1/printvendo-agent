"""Handing bytes straight to the Windows spooler.

The reason this exists is one line in `printing.py`: Ghostscript's `mswinpr2`
device asks the printer *driver* for a DEVMODE, and a driver may want to show
UI to do it. A scheduled task running as SYSTEM lives in session 0, where there
is no desktop to show anything on -- so the call blocks until Ghostscript's own
timeout, having never reached the spooler. A shop claimed jobs, downloaded
them, and printed nothing, for ten minutes at a time.

Everything that has been done about that since -- running as the signed-in
user, at logon, with automatic sign-in, with a repeat trigger for a missed
logon -- is a workaround for that one call. This is the way out: render the PDF
to PCL with Ghostscript, which needs no driver and no desktop, and write the
bytes to the printer as a RAW job. No DEVMODE, no driver UI, nothing that cares
which session it is in.

**It is not universal.** RAW means the printer's own firmware interprets the
bytes, so the printer must understand PCL. Office lasers do -- the Kyocera
ECOSYS in the field is a PCL6 machine -- but a host-based inkjet, which expects
the driver to rasterise everything, would print pages of garbage. That is a
student's money and a shop's paper, so this is opt-in per kiosk and there is a
one-page test to run before trusting it.
"""

import logging

log = logging.getLogger("agent")

# What a RAW job is called in the spooler's queue. Deliberately the task's own
# id and never the student's filename: `lp` and the spooler both keep job
# history long after the document is gone, so a shop's completed-jobs list
# would otherwise read "Medical Results Ravi Kumar.pdf" for as long as the
# machine runs.
DATATYPE = "RAW"


def spool(printer: str, data: bytes, *, job_name: str, copies: int = 1) -> list[int]:
    """Send `data` to `printer` as `copies` separate jobs. Returns their ids.

    **Copies are separate jobs on purpose.** PCL carries a copies attribute and
    printers honour it inconsistently -- some ignore it entirely. One copy of a
    job somebody paid three copies for is exactly the silent wrongness this
    whole module exists to end, and sending the bytes three times is right on
    every printer that can print them at all.

    The cost is three rows in the shop's queue instead of one, which nobody has
    ever complained about.
    """
    import win32print

    handle = win32print.OpenPrinter(printer)
    ids: list[int] = []
    try:
        for copy in range(max(1, copies)):
            # Numbered when there is more than one, so a shopkeeper looking at
            # the queue can tell three copies of one job from three jobs.
            name = job_name if copies == 1 else f"{job_name} ({copy + 1}/{copies})"
            job = win32print.StartDocPrinter(handle, 1, (name, None, DATATYPE))
            try:
                win32print.StartPagePrinter(handle)
                win32print.WritePrinter(handle, data)
                win32print.EndPagePrinter(handle)
            finally:
                # Ended inside the loop, and in a finally: a document left open
                # holds the queue and the next job never starts.
                win32print.EndDocPrinter(handle)
            ids.append(job)
    finally:
        win32print.ClosePrinter(handle)

    log.info("spooled %s as %s job(s) %s", job_name, len(ids), ids)
    return ids
