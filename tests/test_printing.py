"""Finding the things this machine prints with.

The command builders are exercised elsewhere. What is here is the part that
depends on how a machine happens to be set up, which is where the surprises
live.
"""

# ── finding Ghostscript when PATH does not have it ──────────────────────────


def test_ghostscript_is_found_under_program_files_without_path(tmp_path, monkeypatch):
    """Its Windows installer does not add itself to PATH, so a perfectly good
    install was invisible to `which` and the agent reported "not installed"
    about software sitting right there."""
    from agent import printing

    gs = tmp_path / "gs" / "gs10.07.0" / "bin"
    gs.mkdir(parents=True)
    (gs / "gswin64c.exe").write_text("")

    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "nothing"))

    assert printing._ghostscript_in_program_files() == str(gs / "gswin64c.exe")


def test_the_newest_ghostscript_wins(tmp_path, monkeypatch):
    """An upgraded machine keeps the old directory. Running the older binary is
    a subtler fault than running none: it prints, slightly differently, and
    nobody knows why."""
    from agent import printing

    for version in ("gs9.55.0", "gs10.07.0", "gs10.02.1"):
        where = tmp_path / "gs" / version / "bin"
        where.mkdir(parents=True)
        (where / "gswin64c.exe").write_text("")

    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "nothing"))

    assert "gs10.07.0" in printing._ghostscript_in_program_files()


def test_nothing_is_found_when_it_is_genuinely_absent(tmp_path, monkeypatch):
    from agent import printing

    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path))

    assert printing._ghostscript_in_program_files() is None
