"""JPG e MP4 reais com frames sintéticos; não requer câmera nem display."""

import tempfile
import threading
import time
import unittest
from fractions import Fraction
from pathlib import Path
from queue import Queue

import av
import cv2
import numpy as np

from foto_webrtc.channels import put_latest
from foto_webrtc.config import Config
from foto_webrtc.models import Command, VideoFrame
from foto_webrtc.video import VideoProcessor


def packet(slot, width=80, height=60, received_at=None):
    frame = av.VideoFrame.from_ndarray(np.full((height, width, 3), 128, dtype=np.uint8), format="bgr24")
    frame.pts, frame.time_base = slot, Fraction(1, 30)
    return VideoFrame(frame, received_at=received_at) if received_at is not None else VideoFrame(frame)


class VideoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.frames, self.previews, self.actions, self.states, self.sizes = (Queue(2) for _ in range(5))
        self.processor = VideoProcessor(Config(output_dir=Path(self.temp.name)), threading.Event(),
            self.frames, self.previews, self.actions, self.states, self.sizes)
        self.processor.image = np.full((60, 80, 3), 128, dtype=np.uint8)
        self.processor.last_received = time.monotonic()

    def test_mp4_uses_arrival_timestamps_preserves_gaps_and_does_not_mutate_source(self):
        self.processor._start_video(Command("Video"))
        started = time.monotonic()
        for slot, received_slot in zip((100, 101, 101, 105, 109), (0, 1, 5, 7, 9)):
            frame = packet(slot, received_at=started + received_slot / 30)
            self.processor._record(frame)
            self.assertEqual(frame.frame.pts, slot)
        self.processor._stop_video()
        with av.open(str(self.processor.video_path)) as container:
            decoded = list(container.decode(video=0))
        self.assertEqual(len(decoded), 5)
        times = [float(frame.pts * frame.time_base) for frame in decoded]
        self.assertAlmostEqual(times[0], 0)
        self.assertAlmostEqual(times[-1], 0.3, places=3)
        self.assertEqual((decoded[0].width, decoded[0].height), (80, 60))

    def test_mp4_keeps_long_arrival_gaps_even_when_source_pts_does_not(self):
        self.processor._start_video(Command("Video"))
        started = time.monotonic()
        for slot, elapsed in enumerate((0, 1, 2)):
            self.processor._record(packet(slot, received_at=started + elapsed))
        self.processor._stop_video()
        with av.open(str(self.processor.video_path)) as container:
            decoded = list(container.decode(video=0))
            duration = float(container.streams.video[0].duration *
                             container.streams.video[0].time_base)
        self.assertEqual(len(decoded), 3)
        self.assertAlmostEqual(duration, 2 + 1 / 30, places=2)

    def test_photo_during_recording_and_overlay_only_on_preview(self):
        self.processor._start_video(Command("Video"))
        self.processor._record(packet(0))
        self.processor._photo(Command("Foto"))
        self.processor._preview()
        self.assertTrue(np.all(self.processor.image == 128))
        self.processor._stop_video()
        images = list(Path(self.temp.name).glob("fotos/originais/*.jpg"))
        self.assertEqual(len(images), 1)
        image = cv2.imread(str(images[0]))
        self.assertEqual(image.shape, (60, 80, 3))

    def test_preview_fits_viewport_without_crop_or_distortion(self):
        # Cor em cada borda permite detectar corte, além de conferir a proporção.
        self.processor.image = np.full((90, 160, 3), 128, dtype=np.uint8)
        self.processor.image[:8, :] = (0, 0, 255)
        self.processor.image[-8:, :] = (255, 0, 0)
        self.processor.image[8:-8, :8] = (0, 255, 0)
        self.processor.image[8:-8, -8:] = (0, 255, 255)
        original = self.processor.image.copy()
        for viewport, expected in (((640, 480), (640, 360)), ((80, 90), (80, 45)),
                                   ((160, 40), (71, 40))):
            with self.subTest(viewport=viewport):
                self.processor.size = viewport
                self.processor._preview()
                preview = self.previews.get_nowait()
                decoded = cv2.imdecode(np.frombuffer(preview.ppm, dtype=np.uint8), cv2.IMREAD_COLOR)
                height, width = decoded.shape[:2]
                self.assertEqual((width, height), expected)
                self.assertLessEqual(width, viewport[0])
                self.assertLessEqual(height, viewport[1])
                np.testing.assert_array_equal(decoded[0, width // 2], (0, 0, 255))
                np.testing.assert_array_equal(decoded[-1, width // 2], (255, 0, 0))
                np.testing.assert_array_equal(decoded[height // 2, 0], (0, 255, 0))
                np.testing.assert_array_equal(decoded[height // 2, -1], (0, 255, 255))
                np.testing.assert_array_equal(self.processor.image, original)

    def test_resolution_change_closes_mp4_safely(self):
        self.processor._start_video(Command("Video"))
        self.processor._record(packet(0))
        self.processor.image = np.zeros((60, 82, 3), dtype=np.uint8)
        self.processor._record(packet(1, width=82))
        self.assertIsNone(self.processor.container)
        with av.open(str(self.processor.video_path)) as container:
            self.assertEqual(len(list(container.decode(video=0))), 1)

    def test_stale_frame_does_not_create_photo_or_video(self):
        self.processor.last_received -= 10
        self.processor._photo(Command("Foto"))
        self.processor._start_video(Command("Video"))
        self.assertFalse(list(Path(self.temp.name).rglob("*.jpg")))
        self.assertIsNone(self.processor.container)

    def test_shutdown_during_recording_flushes_encoder_and_joins_thread(self):
        self.processor._start_video(Command("Video"))
        self.processor.start()
        for slot in (0, 1, 2, 3):
            put_latest(self.frames, packet(slot))
            time.sleep(0.03)
        self.processor.stop_event.set()
        self.processor.join(2)
        self.assertFalse(self.processor.is_alive())
        self.assertIsNone(self.processor.failure)
        self.assertIsNone(self.processor.container)
        with av.open(str(self.processor.video_path)) as container:
            self.assertGreaterEqual(len(list(container.decode(video=0))), 1)

    def test_capture_reads_track_on_rtc_event_loop(self):
        # receive_frame é a ponte usada pelo worker, não um segundo loop aiortc.
        from foto_webrtc.application import Application
        from test_concurrency import FakeTransport, FakeWindow

        owner = []

        class Track:
            async def recv(self):
                import asyncio
                owner.append(threading.current_thread().name)
                await asyncio.sleep(0.005)
                return packet(0).frame

        class Transport(FakeTransport):
            async def connect(self):
                put_latest(self.tracks, Track())

        window = FakeWindow()
        app = Application(Config(), window, Transport)
        deadline = time.monotonic() + 2

        def close_after_receiving():
            if owner or time.monotonic() >= deadline:
                window.on_close()
            else:
                window.root.after(10, close_after_receiving)

        window.root.after(10, close_after_receiving)
        app.run()
        self.assertTrue(owner)
        self.assertEqual(set(owner), {"go2-rtc"})
