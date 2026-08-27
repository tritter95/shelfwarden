"""Audiobook detection.

Plex has no audiobook section type, so this is a judgement -- which is why the
verdict carries its signals, its thresholds, and how much it looked at.
"""

import pytest

from shelfwarden.library.audiobook import (
    LONG_TRACK_MS,
    AudiobookVerdict,
    Signal,
    TrackSample,
    classify_section,
)
from shelfwarden.library.plex import build_samples, configure_plexapi
from tests.library.conftest import load_fixture

AUDIOBOOK_AGENT = "com.plexapp.agents.audnexus"
MUSIC_AGENT = "com.plexapp.agents.lastfm"


def chapter(index: int, extension: str = ".m4b", minutes: int = 45) -> TrackSample:
    return TrackSample(
        album_id="6",
        path=f"/media/Audiobooks/Book/Chapter {index:02d}{extension}",
        container="mp4",
        duration_ms=minutes * 60 * 1000,
    )


def song(index: int) -> TrackSample:
    return TrackSample(
        album_id="800",
        path=f"/media/Music/Album/{index:02d} Track.flac",
        container="flac",
        duration_ms=4 * 60 * 1000,
    )


class TestDecisiveAgent:
    def test_the_agent_identifier_decides_on_its_own(self):
        """The operator has already told Plex what the section is."""
        verdict = classify_section(AUDIOBOOK_AGENT, [], population=0)
        assert verdict.is_audiobook
        assert Signal.AGENT_IDENTIFIER in verdict.fired

    @pytest.mark.parametrize("agent", ["com.plexapp.agents.audnexus", "tv.plex.agents.audiobook"])
    def test_both_markers_are_recognised(self, agent):
        assert classify_section(agent, [], population=0).is_audiobook

    def test_the_agent_wins_even_over_music_shaped_content(self):
        verdict = classify_section(AUDIOBOOK_AGENT, [song(i) for i in range(20)], population=20)
        assert verdict.is_audiobook


class TestContentSignals:
    def test_a_music_section_is_not_audiobooks(self):
        verdict = classify_section(MUSIC_AGENT, [song(i) for i in range(20)], population=2000)
        assert not verdict.is_audiobook
        assert verdict.fired == ()

    def test_a_generic_agent_with_audiobook_content_is_detected(self):
        verdict = classify_section(MUSIC_AGENT, [chapter(i) for i in range(20)], population=90)
        assert verdict.is_audiobook
        assert set(verdict.fired) == {Signal.CONTAINER_SHARE, Signal.ALBUM_STRUCTURE}

    def test_long_tracks_alone_are_not_enough(self):
        """A classical or live-recording library has long tracks. Container and
        structure must both fire, or every symphony collection is a book."""
        classical = [
            TrackSample(
                album_id="1", path=f"/m/{i}.flac", container="flac", duration_ms=25 * 60_000
            )
            for i in range(10)
        ]
        verdict = classify_section(MUSIC_AGENT, classical, population=10)
        assert not verdict.is_audiobook
        assert verdict.fired == (Signal.ALBUM_STRUCTURE,)

    def test_short_m4a_files_alone_are_not_enough(self):
        """Spoken-word podcasts and AAC music both ship as .m4a."""
        short_aac = [
            TrackSample(album_id="1", path=f"/m/{i}.m4a", container="m4a", duration_ms=3 * 60_000)
            for i in range(10)
        ]
        verdict = classify_section(MUSIC_AGENT, short_aac, population=10)
        assert not verdict.is_audiobook
        assert verdict.fired == (Signal.CONTAINER_SHARE,)

    def test_the_container_share_threshold_is_a_share_not_a_count(self):
        mixed = [chapter(i) for i in range(6)] + [song(i) for i in range(4)]
        verdict = classify_section(MUSIC_AGENT, mixed, population=10)
        assert Signal.CONTAINER_SHARE in verdict.fired  # 60% >= 50%

    def test_m4b_is_detected_by_extension_when_the_container_says_mp4(self):
        """Plex reports .m4b as container 'mp4' often enough that the extension
        has to be checked too."""
        sample = TrackSample(
            album_id="6", path="/m/Chapter 01.m4b", container="mp4", duration_ms=LONG_TRACK_MS
        )
        verdict = classify_section(MUSIC_AGENT, [sample], population=1)
        assert Signal.CONTAINER_SHARE in verdict.fired


class TestTheVerdictExplainsItself:
    def test_every_signal_is_recorded_whether_or_not_it_fired(self):
        verdict = classify_section(MUSIC_AGENT, [song(1)], population=1)
        assert {result.signal for result in verdict.signals} == set(Signal)

    def test_each_signal_records_what_was_observed_and_the_threshold(self):
        verdict = classify_section(MUSIC_AGENT, [song(1)], population=1)
        for result in verdict.signals:
            assert result.observed
            assert result.threshold

    def test_the_sample_size_and_population_are_both_reported(self):
        """Detection samples rather than walking a large section; a truncation
        nobody can see reads as full coverage."""
        verdict = classify_section(MUSIC_AGENT, [song(i) for i in range(40)], population=12000)
        assert (verdict.sampled, verdict.population) == (40, 12000)
        assert "40/12000" in verdict.explain()

    def test_explain_names_the_verdict(self):
        assert "not audiobooks" in classify_section(MUSIC_AGENT, [song(1)], 1).explain()
        assert "audiobooks" in classify_section(AUDIOBOOK_AGENT, [], 0).explain()

    def test_an_empty_sample_does_not_crash(self):
        verdict = classify_section(MUSIC_AGENT, [], population=0)
        assert isinstance(verdict, AudiobookVerdict)
        assert not verdict.is_audiobook


class TestAgainstCommittedFixtures:
    """The roadmap's gate: detection passes against committed fixtures."""

    def test_an_audiobook_chapter_fixture_classifies_as_audiobooks(self):
        configure_plexapi()
        samples = build_samples([load_fixture("audiobook_part")])
        verdict = classify_section(MUSIC_AGENT, samples, population=90)
        assert verdict.is_audiobook, verdict.explain()

    def test_a_music_track_fixture_does_not(self):
        configure_plexapi()
        samples = build_samples([load_fixture("music_track")])
        verdict = classify_section(MUSIC_AGENT, samples, population=2000)
        assert not verdict.is_audiobook, verdict.explain()

    def test_the_fixtures_produce_usable_samples(self):
        configure_plexapi()
        (sample,) = build_samples([load_fixture("audiobook_part")])
        assert sample.path.endswith(".m4b")
        assert sample.duration_ms == 2700000
        assert sample.album_id == "6"
