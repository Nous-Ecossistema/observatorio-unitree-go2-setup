"""Correção integrada à captura: frames, arquivos reais e entrega à GUI."""

import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from queue import Queue
from unittest.mock import patch

import av
import cv2
import numpy as np

from foto_webrtc.calibration import CameraCorrection
from foto_webrtc.config import Config
from foto_webrtc.models import Command, VideoFrame
from foto_webrtc.video import VideoProcessor


class CalibrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "calibracao.npz"
        self.K = np.array([[60., 0, 40], [0, 61., 30], [0, 0, 1]])
        self.D = np.array([-.25, .08, .001, -.002, 0])
        self.save_calibration()
        y, x = np.indices((60, 80))
        self.original = np.stack((x * 3, y * 4, x + y), axis=-1).astype(np.uint8)

    def save_calibration(self, **changes):
        data = dict(model="pinhole", K=self.K, D=self.D, image_size=[80, 60])
        data.update(changes)
        np.savez(self.path, **data)

    def processor(self):
        queues = [Queue(2) for _ in range(6)]
        return VideoProcessor(Config(output_dir=self.root, calibration_path=self.path),
                              threading.Event(), *queues)

    def packet(self, image):
        return VideoFrame(av.VideoFrame.from_ndarray(image, format="bgr24"))

    def test_mapping_matches_direct_undistort_reuses_maps_and_preserves_original(self):
        with patch.object(cv2, "initUndistortRectifyMap", wraps=cv2.initUndistortRectifyMap) as maps:
            correction = CameraCorrection(self.path)
            for _ in range(2):
                before = self.original.copy()
                actual = correction.apply(self.original)
                expected = cv2.undistort(self.original, self.K, self.D, None, correction.new_K)
                self.assertLess(np.abs(actual.astype(float) - expected.astype(float)).mean(), .1)
                np.testing.assert_array_equal(self.original, before)
            self.assertEqual(maps.call_count, 1)

    def test_bad_calibration_is_rejected_before_workers_start(self):
        for changes in ({"model": "fisheye"}, {"D": [1, 2, 3, 4]},
                        {"image_size": [80.5, 60]}, {"K": np.full((3, 3), np.nan)}):
            with self.subTest(changes=changes):
                self.save_calibration(**changes)
                with self.assertRaises(ValueError):
                    self.processor()
        with self.assertRaises(ValueError):
            CameraCorrection(self.root / "ausente.npz")

    def test_output_preserves_focal_ratio_instead_of_stretching_axes_separately(self):
        for alpha in (0, .5, 1):
            with self.subTest(alpha=alpha):
                correction = CameraCorrection(self.path, alpha)
                self.assertAlmostEqual(correction.new_K[0, 0] / self.K[0, 0],
                                       correction.new_K[1, 1] / self.K[1, 1], places=12)
                self.assertEqual(correction.new_K[0, 2], 39.5)
                self.assertEqual(correction.new_K[1, 2], 29.5)

    def test_photo_saves_corrected_pixels_lossless_original_and_updates_notification(self):
        processor = self.processor()
        for original in (self.original, 255 - self.original):
            self.assertTrue(processor._receive_image(self.packet(original)))
            corrected = processor.image.copy()
            self.assertFalse(np.array_equal(corrected, original))
            processor._photo(Command("Foto"))
            photo = processor.photos.get_nowait()
            self.assertTrue(photo.path.name.startswith("foto_corrigida_"))
            self.assertEqual(photo.path.parent, self.root / "fotos" / "corrigidas")
            self.assertEqual(photo.original_path.parent, self.root / "fotos" / "originais")
            np.testing.assert_array_equal(cv2.imread(str(photo.original_path)), original)
            # Confere que o JPG é da imagem corrigida, sem overlay nem segunda correção.
            _, encoded = cv2.imencode(".jpg", corrected)
            np.testing.assert_array_equal(cv2.imread(str(photo.path)),
                                          cv2.imdecode(encoded, cv2.IMREAD_COLOR))
            expected_preview = cv2.imdecode(np.frombuffer(processor._ppm(corrected, (960, 540)),
                                                          np.uint8), cv2.IMREAD_COLOR)
            actual_preview = cv2.imdecode(np.frombuffer(photo.ppm, np.uint8), cv2.IMREAD_COLOR)
            np.testing.assert_array_equal(actual_preview, expected_preview)
        self.assertEqual(len(list((self.root / "fotos" / "corrigidas").glob("*.jpg"))), 2)

    def test_resolution_change_refuses_stale_capture_and_recovers_with_original_size(self):
        processor = self.processor()
        self.assertTrue(processor._receive_image(self.packet(self.original)))
        self.assertFalse(processor._receive_image(self.packet(self.original[::2, ::2])))
        self.assertIsNone(processor.image)
        processor._photo(Command("Foto"))
        processor._start_video(Command("Video"))
        self.assertFalse(list(self.root.rglob("*.jpg")))
        self.assertIsNone(processor.container)
        self.assertIn("80x60", processor.state.message)
        self.assertTrue(processor._receive_image(self.packet(self.original)))
        self.assertIsNone(processor.correction_error)
        processor._photo(Command("Foto"))
        self.assertTrue(processor.photos.get_nowait().path.is_file())

    def test_custom_photo_folder_receives_both_versions_without_changing_video_folder(self):
        processor = self.processor()
        destination = self.root / "minhas_fotos"
        processor.config = replace(processor.config, photo_dir=destination)
        self.assertTrue(processor._receive_image(self.packet(self.original)))
        processor._photo(Command("Foto"))
        photo = processor.photos.get_nowait()
        self.assertEqual(photo.path.parent, destination / "corrigidas")
        self.assertEqual(photo.original_path.parent, destination / "originais")
        self.assertEqual(processor.config.output_dir, self.root)
        self.assertFalse((self.root / "fotos").exists())

    def test_photo_notification_runs_on_gui_thread_with_application_workers(self):
        from foto_webrtc.application import Application
        from test_concurrency import FakeTransport, FakeWindow

        window = FakeWindow()
        delivered = []

        def show(photo):
            delivered.append((photo, threading.get_ident()))
            window.on_close()

        window.mostrar_foto = show
        app = Application(Config(output_dir=self.root, calibration_path=self.path), window, FakeTransport)
        deadline = time.monotonic() + 2

        def capture_when_ready():
            if app.workers[0]._fresh():
                app.action("Foto")
            elif time.monotonic() < deadline:
                window.root.after(10, capture_when_ready)
            else:
                window.on_close()

        window.root.after(20, capture_when_ready)
        window.root.after(2500, window.on_close)
        app.run()
        self.assertEqual(len(delivered), 1)
        photo, owner = delivered[0]
        self.assertEqual(owner, threading.get_ident())
        self.assertTrue(photo.path.is_file())
        self.assertTrue(photo.original_path.is_file())
        self.assertFalse(any(worker.is_alive() for worker in app.workers))
