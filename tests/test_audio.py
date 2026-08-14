import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import mido
import ndspy.soundSequence
import ndspy.soundBank
import ndspy.soundWave

from rpgds_audio import (
    NDS_SOUND_CLOCK,
    _swav_base_rate,
    _swav_loop_start_samples,
    midi_to_sequence,
    render_sequence_pcm,
    render_sequence_wav,
    sequence_to_midi,
)
from rpgds_core import load_project, load_project_audio, save_project


class AudioConversionTests(unittest.TestCase):
    def _base_sequence(self):
        return ndspy.soundSequence.SSEQ.fromEvents(
            [ndspy.soundSequence.EndTrackSequenceEvent()],
            bankID=3, volume=100, playerID=1,
        )

    def test_midi_import_preserves_bank_player_and_note(self):
        midi = mido.MidiFile(ticks_per_beat=480)
        track = mido.MidiTrack()
        midi.tracks.append(track)
        track.extend([
            mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(120), time=0),
            mido.Message("note_on", note=64, velocity=90, time=0),
            mido.Message("note_off", note=64, velocity=0, time=480),
        ])
        converted = midi_to_sequence(midi, [7], self._base_sequence())
        raw = bytes(converted.save()[0])
        reparsed = ndspy.soundSequence.SSEQ(raw, converted.unk02, converted.bankID,
                                            converted.volume, converted.channelPressure,
                                            converted.polyphonicPressure, converted.playerID)
        reparsed.parse()
        self.assertEqual(reparsed.bankID, 3)
        self.assertEqual(reparsed.playerID, 1)
        instruments = [event for event in reparsed.events
                       if isinstance(event, ndspy.soundSequence.InstrumentSwitchSequenceEvent)]
        notes = [event for event in reparsed.events
                 if isinstance(event, ndspy.soundSequence.NoteSequenceEvent)]
        self.assertEqual(instruments[0].instrumentID, 7)
        self.assertEqual((notes[0].type, notes[0].velocity, notes[0].duration), (64, 90, 48))

    def test_sseq_export_produces_standard_midi(self):
        sequence = ndspy.soundSequence.SSEQ.fromEvents([
            ndspy.soundSequence.InstrumentSwitchSequenceEvent(0, 4),
            ndspy.soundSequence.NoteSequenceEvent(60, 100, 24),
            ndspy.soundSequence.RestSequenceEvent(24),
            ndspy.soundSequence.EndTrackSequenceEvent(),
        ], bankID=0, playerID=1)
        midi = sequence_to_midi(sequence)
        messages = [message for track in midi.tracks for message in track]
        self.assertTrue(any(message.type == "program_change" and message.program == 4
                            for message in messages))
        self.assertTrue(any(message.type == "note_on" and message.note == 60
                            for message in messages))

    def test_project_round_trips_audio_replacement(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.nds"
            source.write_bytes(b"test source")
            project = root / "audio.rpgdsproj"
            replacement = b"SSEQ replacement bytes"
            save_project(project, source, [], {}, None, {"bgm:0": replacement})
            loaded_source, rows, images, embedded = load_project(project)
            self.assertEqual(loaded_source, source)
            self.assertEqual((rows, images, embedded), ({}, {}, None))
            self.assertEqual(load_project_audio(project), {"bgm:0": replacement})

    def test_native_preview_handles_one_sample_tail_note(self):
        definition = ndspy.soundBank.NoteDefinition(
            0, 0, 60, type=ndspy.soundBank.NoteType.PCM,
        )
        bank = SimpleNamespace(
            instruments=[ndspy.soundBank.SingleNoteInstrument(definition)],
            waveArchiveIDs=[0],
        )
        source_wave = ndspy.soundWave.SWAV.fromData(
            bytes([64] * 64), waveType=ndspy.soundWave.WaveType.PCM8,
            sampleRate=8000, totalLength=16,
        )
        sdat = SimpleNamespace(
            banks=[("bank", bank)],
            waveArchives=[("waves", SimpleNamespace(waves=[source_wave]))],
        )
        asset = SimpleNamespace(bank_id=0)
        sequence = self._base_sequence()
        sequence.bankID = 0
        # At 8 kHz this begins on frame 799 of an 800-frame preview, which
        # previously created a zero-length fade and a NumPy broadcast error.
        schedule = [(799 / 8000, .03, 60, 100, 0, 100, 64, 127)]
        with mock.patch("rpgds_audio._midi_note_schedule", return_value=schedule):
            output = render_sequence_wav(sdat, asset, sequence, seconds=.1, sample_rate=8000)
        self.assertEqual(output[:4], b"RIFF")

    def test_live_preview_returns_raw_stereo_pcm_without_wav_container(self):
        definition = ndspy.soundBank.NoteDefinition(
            0, 0, 60, type=ndspy.soundBank.NoteType.PCM,
        )
        bank = SimpleNamespace(
            instruments=[ndspy.soundBank.SingleNoteInstrument(definition)],
            waveArchiveIDs=[0],
        )
        source_wave = ndspy.soundWave.SWAV.fromData(
            bytes([32] * 64), waveType=ndspy.soundWave.WaveType.PCM8,
            sampleRate=8000, totalLength=16,
        )
        sdat = SimpleNamespace(
            banks=[("bank", bank)],
            waveArchives=[("waves", SimpleNamespace(waves=[source_wave]))],
        )
        asset = SimpleNamespace(bank_id=0)
        sequence = self._base_sequence(); sequence.bankID = 0
        schedule = [(0, .05, 60, 100, 0, 100, 64, 127)]
        with mock.patch("rpgds_audio._midi_note_schedule", return_value=schedule):
            pcm, rate = render_sequence_pcm(sdat, asset, sequence, seconds=.1,
                                             sample_rate=8000)
        self.assertEqual(rate, 8000)
        self.assertEqual(len(pcm), 800 * 2 * 2)
        self.assertNotEqual(pcm[:4], b"RIFF")

    def test_ds_tracks_keep_independent_instrument_channels(self):
        track_one = [
            ndspy.soundSequence.InstrumentSwitchSequenceEvent(0, 3),
            ndspy.soundSequence.NoteSequenceEvent(60, 100, 24),
            ndspy.soundSequence.EndTrackSequenceEvent(),
        ]
        track_two = [
            ndspy.soundSequence.InstrumentSwitchSequenceEvent(0, 9),
            ndspy.soundSequence.NoteSequenceEvent(67, 100, 24),
            ndspy.soundSequence.EndTrackSequenceEvent(),
        ]
        sequence = ndspy.soundSequence.SSEQ.fromEvents([
            ndspy.soundSequence.DefineTracksSequenceEvent({0, 1}),
            ndspy.soundSequence.BeginTrackSequenceEvent(1, track_two[0]),
            *track_one, *track_two,
        ], bankID=0, playerID=1)
        midi = sequence_to_midi(sequence)
        programs = [(message.channel, message.program)
                    for track in midi.tracks for message in track
                    if message.type == "program_change"]
        notes = [(message.channel, message.note)
                 for track in midi.tracks for message in track
                 if message.type == "note_on"]
        self.assertIn((0, 3), programs)
        self.assertIn((1, 9), programs)
        self.assertIn((0, 60), notes)
        self.assertIn((1, 67), notes)

    def test_transpose_and_pitch_bend_are_exported(self):
        sequence = ndspy.soundSequence.SSEQ.fromEvents([
            ndspy.soundSequence.TransposeSequenceEvent(12),
            ndspy.soundSequence.PortamentoRangeSequenceEvent(7),
            ndspy.soundSequence.PortamentoSequenceEvent(192),  # signed -64
            ndspy.soundSequence.NoteSequenceEvent(60, 100, 24),
            ndspy.soundSequence.EndTrackSequenceEvent(),
        ], bankID=0, playerID=1)
        midi = sequence_to_midi(sequence)
        messages = [message for track in midi.tracks for message in track]
        self.assertTrue(any(message.type == "note_on" and message.note == 72
                            for message in messages))
        self.assertTrue(any(message.type == "control_change" and message.control == 6
                            and message.value == 7 for message in messages))
        self.assertTrue(any(message.type == "pitchwheel" and message.pitch == -4096
                            for message in messages))

    def test_note_wait_mode_advances_following_note(self):
        sequence = ndspy.soundSequence.SSEQ.fromEvents([
            ndspy.soundSequence.MonoPolySequenceEvent(1),
            ndspy.soundSequence.NoteSequenceEvent(60, 100, 24),
            ndspy.soundSequence.NoteSequenceEvent(64, 100, 12),
            ndspy.soundSequence.EndTrackSequenceEvent(),
        ], bankID=0, playerID=1)
        midi = sequence_to_midi(sequence)
        absolute, note_ons = 0, []
        for message in midi.tracks[0]:
            absolute += message.time
            if message.type == "note_on" and message.velocity:
                note_ons.append((message.note, absolute))
        self.assertEqual(note_ons, [(60, 0), (64, 24)])

    def test_swav_pitch_uses_hardware_timer_not_rounded_rate(self):
        wav = SimpleNamespace(time=523, sampleRate=32000)
        self.assertAlmostEqual(_swav_base_rate(wav), NDS_SOUND_CLOCK / 523)
        self.assertNotEqual(round(_swav_base_rate(wav)), wav.sampleRate)

    def test_adpcm_loop_word_offset_excludes_predictor_header(self):
        wav = SimpleNamespace(loopOffset=4,
                              waveType=ndspy.soundWave.WaveType.ADPCM)
        self.assertEqual(_swav_loop_start_samples(wav), 25)


if __name__ == "__main__":
    unittest.main()
