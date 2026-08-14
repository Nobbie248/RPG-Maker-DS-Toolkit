import tempfile
import unittest
import io
import math
import wave
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import mido
import ndspy.soundSequence
import ndspy.soundBank
import ndspy.soundWave
import numpy as np
import rpgds_audio

from rpgds_audio import (
    NDS_SOUND_CLOCK,
    _swav_base_rate,
    _swav_loop_start_samples,
    _decode_swav,
    _private_sdat,
    midi_to_sequence,
    midi_loop_ticks,
    midi_tick_to_time,
    midi_time_to_tick,
    format_audio_time,
    parse_audio_time,
    render_sequence_pcm,
    render_sequence_wav,
    sequence_to_midi,
    sequence_loop_ticks,
    sequence_loop_times,
    wav_bytes_to_swav,
)
from rpgds_core import (load_project, load_project_audio,
                        load_project_audio_samples, save_project)


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

    def test_preview_clone_uses_untouched_sdat_bytes_after_sequence_browsing(self):
        loaded = SimpleNamespace(_rpgds_source_data=b"original SDAT bytes")
        private = object()
        with mock.patch("rpgds_audio.ndspy.soundArchive.SDAT",
                        return_value=private) as constructor:
            self.assertIs(_private_sdat(loaded), private)
        constructor.assert_called_once_with(b"original SDAT bytes")

    def test_project_round_trips_swav_replacement(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.nds"
            source.write_bytes(b"test source")
            project = root / "samples.rpgdsproj"
            replacement = b"SWAV replacement bytes"
            save_project(project, source, [], {}, None, {}, {"swar:60:0": replacement})
            self.assertEqual(load_project_audio_samples(project),
                             {"swar:60:0": replacement})

    def test_wav_import_converts_stereo_pcm_to_normalized_ds_adpcm(self):
        rate = 22050
        time = np.arange(rate // 10) / rate
        mono = (np.sin(time * 2 * math.pi * 440) * 8000).astype("<i2")
        stereo = np.column_stack((mono, mono)).astype("<i2").tobytes()
        source = io.BytesIO()
        with wave.open(source, "wb") as wav_file:
            wav_file.setnchannels(2)
            wav_file.setsampwidth(2)
            wav_file.setframerate(rate)
            wav_file.writeframes(stereo)
        converted = ndspy.soundWave.SWAV(wav_bytes_to_swav(source.getvalue()))
        self.assertEqual(converted.waveType, ndspy.soundWave.WaveType.ADPCM)
        self.assertEqual(converted.sampleRate, rate)
        self.assertFalse(converted.isLooped)
        decoded = _decode_swav(converted)
        self.assertGreater(float(np.max(np.abs(decoded))), 0.75)

    def test_midi_setup_applies_track_volume_and_tempo_override(self):
        midi = mido.MidiFile(ticks_per_beat=480)
        track = mido.MidiTrack([
            mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(90), time=0),
            mido.Message("note_on", note=60, velocity=100, time=0),
            mido.Message("note_off", note=60, velocity=0, time=480),
        ])
        midi.tracks.append(track)
        converted = midi_to_sequence(midi, [2], self._base_sequence(), [77], 140)
        converted.parse()
        self.assertTrue(any(isinstance(event, ndspy.soundSequence.TrackVolumeSequenceEvent)
                            and event.value == 77 for event in converted.events))
        self.assertTrue(any(isinstance(event, ndspy.soundSequence.TempoSequenceEvent)
                            and event.value == 140 for event in converted.events))

    def test_audio_time_text_round_trip(self):
        self.assertEqual(format_audio_time(83.5), "1:23.500")
        self.assertAlmostEqual(parse_audio_time("1:23.500"), 83.5)
        self.assertAlmostEqual(parse_audio_time("2.25"), 2.25)
        with self.assertRaises(ValueError):
            parse_audio_time("not a time")

    def test_midi_time_to_tick_follows_tempo_changes(self):
        midi = mido.MidiFile(ticks_per_beat=480)
        track = mido.MidiTrack([
            mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(120), time=0),
            mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(60), time=480),
            mido.MetaMessage("end_of_track", time=480),
        ])
        midi.tracks.append(track)
        self.assertEqual(midi_time_to_tick(midi, 0.5), 480)
        self.assertEqual(midi_time_to_tick(midi, 1.5), 960)

    def test_midi_loop_is_embedded_as_backward_sseq_jump(self):
        midi = mido.MidiFile(ticks_per_beat=480)
        for note in (60, 67):
            midi.tracks.append(mido.MidiTrack([
                mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(120), time=0),
                mido.Message("note_on", note=note, velocity=100, time=0),
                mido.Message("note_off", note=note, velocity=0, time=480),
                mido.Message("note_on", note=note + 2, velocity=100, time=480),
                mido.Message("note_off", note=note + 2, velocity=0, time=480),
            ]))
        converted = midi_to_sequence(
            midi, [1, 2], self._base_sequence(), [100, 100], 120,
            loop_start_tick=480, loop_end_tick=1440,
        )
        raw = bytes(converted.save()[0])
        reparsed = ndspy.soundSequence.SSEQ(
            raw, converted.unk02, converted.bankID, converted.volume,
            converted.channelPressure, converted.polyphonicPressure,
            converted.playerID,
        )
        reparsed.parse()
        jumps = [event for event in reparsed.events
                 if isinstance(event, ndspy.soundSequence.JumpSequenceEvent)]
        self.assertEqual(len(jumps), 2)
        self.assertEqual(sequence_loop_ticks(reparsed), (48, 144))
        self.assertEqual(sequence_loop_times(reparsed), (0.5, 1.5))
        exported = sequence_to_midi(reparsed)
        self.assertEqual(midi_loop_ticks(exported), (48, 144))
        self.assertAlmostEqual(midi_tick_to_time(exported, 144), 1.5)
        self.assertAlmostEqual(exported.length, 1.5)

    def test_midi_export_clips_sustained_note_at_sseq_loop_end(self):
        loop_target = ndspy.soundSequence.NoteSequenceEvent(60, 100, 10000)
        sequence = ndspy.soundSequence.SSEQ.fromEvents([
            ndspy.soundSequence.TempoSequenceEvent(120),
            ndspy.soundSequence.RestSequenceEvent(24),
            loop_target,
            ndspy.soundSequence.RestSequenceEvent(48),
            ndspy.soundSequence.JumpSequenceEvent(loop_target),
        ], bankID=0, playerID=1)
        midi = sequence_to_midi(sequence)
        self.assertEqual(midi_loop_ticks(midi), (24, 72))
        self.assertAlmostEqual(midi.length, 0.75)

    def test_music_preview_requests_continuous_loop(self):
        channel = mock.Mock()
        sound = mock.Mock()
        sound.play.return_value = channel
        mixer = mock.Mock()
        mixer.get_init.return_value = (32768, -16, 2)
        mixer.Sound.return_value = sound
        pygame = SimpleNamespace(mixer=mixer)
        with mock.patch.dict(sys.modules, {"pygame": pygame}), \
                mock.patch.object(rpgds_audio, "_preview_channel", None):
            rpgds_audio.play_pcm_bytes(b"\0" * 16, 32768, loop=True)
        sound.play.assert_called_once_with(loops=-1)

    def test_music_preview_plays_intro_then_only_selected_loop(self):
        channel = mock.Mock()
        intro_sound = mock.Mock()
        loop_sound = mock.Mock()
        intro_sound.play.return_value = channel
        mixer = mock.Mock()
        mixer.get_init.return_value = (10, -16, 2)
        mixer.Sound.side_effect = [intro_sound, loop_sound]
        pygame = SimpleNamespace(mixer=mixer)
        timer = mock.Mock()
        callbacks = []

        def make_timer(delay, callback):
            callbacks.append((delay, callback))
            return timer

        with mock.patch.dict(sys.modules, {"pygame": pygame}), \
                mock.patch("rpgds_audio.threading.Timer", side_effect=make_timer), \
                mock.patch.object(rpgds_audio, "_preview_channel", None), \
                mock.patch.object(rpgds_audio, "_preview_timer", None):
            # At 10 Hz stereo/16-bit, 400 bytes is ten seconds.  Play 0..8
            # once, queue 2..8 gaplessly, then keep repeating 2..8.
            rpgds_audio.play_pcm_bytes(
                b"\0" * 400, 10, loop=True,
                loop_start_seconds=2.0, loop_end_seconds=8.0,
            )
            intro_sound.play.assert_called_once_with(loops=0)
            channel.queue.assert_called_once_with(loop_sound)
            self.assertAlmostEqual(callbacks[0][0], 14.0)
            callbacks[0][1]()
            channel.play.assert_called_once_with(loop_sound, loops=-1)

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
