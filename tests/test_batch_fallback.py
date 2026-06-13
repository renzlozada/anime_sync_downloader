"""Offline unit tests for the batch/box fallback. No network; uses canned Torrent
fixtures mirroring a real Madoka Nyaa search. Run:
    .venv\\Scripts\\python.exe tests\\test_batch_fallback.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import anime_downloader as core


def test_looks_like_single_episode():
    f = core.looks_like_single_episode
    assert f("[SubsPlease] Dandadan - 07 (1080p) [ABC123].mkv") is True
    assert f("Title - S01E07 - Name [HDTV-1080p].mkv") is True
    assert f("[MiniMTBB] Puella Magi Madoka Magica (BD 1080p)") is False  # the box
    assert f("[Group] Some Show 01-12 (BD 1080p) [Batch]") is False       # a range
    assert f("[Group] Some Show Complete Series (BD 1080p)") is False


def test_clean_torrent_title():
    c = core._clean_torrent_title
    assert c("[MiniMTBB] Puella Magi Madoka Magica (BD 1080p)") == "Puella Magi Madoka Magica"
    assert c("[LYS1TH3A] Solo Leveling Season 1 | Ore (BD 1080p HEVC)").startswith("Solo Leveling")


def _madoka_fixtures():
    """Canned results mirroring a real 'Madoka Magica 1080p' Nyaa search."""
    def T(title, trusted, seeders, res="1080p"):
        return core.Torrent(title=title, link="magnet:?x", seeders=seeders,
                            trusted=trusted, remake=False, resolution=res,
                            infohash="h" + str(seeders))
    return [
        T("[anime4life.] Puella Magi Madoka Magica LPCM BD_1080p Dual Audio", False, 22),
        T("Puella Magi Madoka Magica the Movie Part II Eternal (2012) [BD Remux 1080p]", False, 26),
        T("[ABi] Puella Magi Madoka Magica III - Rebellion (Dual Audio) [BluRay-1080p].mkv", False, 2),
        T("[MegukaMux] Magia Record Puella Magi Madoka Magica Side Story Season 1", False, 26),
        T("[LYS1TH3A] Puella Magi Madoka Magica the Movie Part III Rebellion (2013)", False, 82),
        T("[LYS1TH3A] Puella Magi Madoka Magica Season 1 (BD 1080p HEVC)", False, 101),
        T("[MiniMTBB] Puella Magi Madoka Magica the Movie: Rebellion (BD 1080p)", True, 33),
        T("[MiniMTBB] Puella Magi Madoka Magica (BD 1080p)", True, 49),  # the TV box
    ]


def test_pick_batch_torrent_madoka():
    box = core.pick_batch_torrent(
        _madoka_fixtures(),
        romaji="Mahou Shoujo Madoka Magica",
        english="Puella Magi Madoka Magica",
        season_format="TV", resolution="1080p")
    assert box is not None, "expected a box"
    assert box.title == "[MiniMTBB] Puella Magi Madoka Magica (BD 1080p)", box.title


def test_pick_batch_torrent_rejects_untrusted_only():
    only_untrusted = [t for t in _madoka_fixtures() if not t.trusted]
    assert core.pick_batch_torrent(only_untrusted, "Mahou Shoujo Madoka Magica",
                                   "Puella Magi Madoka Magica", "TV",
                                   resolution="1080p") is None


def _medaka_fixtures():
    """Canned results mirroring a real 'Medaka Box' Nyaa search — two seasons
    ('Medaka Box' S1, 'Medaka Box Abnormal' S2) with boxes at different rungs."""
    def T(title, trusted, seeders, res="1080p"):
        return core.Torrent(title=title, link="magnet:?x", seeders=seeders,
                            trusted=trusted, remake=False, resolution=res,
                            infohash="h" + res + str(seeders))
    return [
        T("[WIP] Medaka Box S1 + Medaka Box: Abnormal S2 [1080p] [BD] [Complete batch]", False, 16),
        T("[FFF] Medaka Box Abnormal [BD][1080p-FLAC]", True, 2, "1080p"),
        T("[HorribleSubs] Medaka Box S2 - 12 [1080p].mkv", True, 0, "1080p"),
        T("[CBM] Medaka Box 1-12 Complete (Dual Audio) [BDRip-720p-8bit]", True, 0, "720p"),
        T("[FFF] Medaka Box Abnormal [BD][720p-AAC]", True, 3, "720p"),
    ]


def test_medaka_s2_picks_abnormal_box():
    # Season 2 = "Medaka Box Abnormal" -> the FFF Abnormal box at 1080p.
    box = core.pick_batch_torrent(_medaka_fixtures(), "Medaka Box Abnormal",
                                  "Medaka Box Abnormal", "TV", resolution="1080p")
    assert box is not None and "Abnormal" in box.title, box and box.title


def test_medaka_s1_no_1080_box():
    # Season 1 = "Medaka Box": no trusted S1 box at 1080p (FFF is S2 'Abnormal'),
    # so the 1080p rung must return None (live ladder then drops to 720p).
    box = core.pick_batch_torrent(_medaka_fixtures(), "Medaka Box", "Medaka Box",
                                  "TV", resolution="1080p")
    assert box is None, f"S1 should have no 1080p box, got {box and box.title}"


def test_medaka_s1_720_complete_box():
    box = core.pick_batch_torrent(_medaka_fixtures(), "Medaka Box", "Medaka Box",
                                  "TV", resolution="720p")
    assert box is not None and "1-12 Complete" in box.title, box and box.title


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"ok  {name}")
    print("ALL PASS")
