"""Проверка нового длинного громкого WAV-сигнала «такси ждёт»."""
import io
import main
import struct
import unittest
import wave


class SoundWavTests(unittest.TestCase):
    def test_wav_valid_and_long(self):
        data = main._no_driver_sound_wav()
        w = wave.open(io.BytesIO(data))
        self.assertEqual(w.getnchannels(), 1)
        self.assertEqual(w.getsampwidth(), 2)
        self.assertGreaterEqual(w.getnframes() / w.getframerate(), 3.0)  # длиннее 3 секунд
        n = w.getnframes()
        samples = struct.unpack("<%dh" % n, data[44:44 + n * 2])
        peak = max(abs(s) for s in samples)
        # громко: пик > 80% от максимума (32767 * 0.8 ≈ 26200)
        self.assertGreater(peak, 26000, f"пик сигнала слишком тихий: {peak}")


if __name__ == "__main__":
    unittest.main()