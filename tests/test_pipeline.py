"""Core contract, XML adapter, and mocked exporter regression tests."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cinesync.core.engine import (ValidationError, frames_to_smpte, image_offset,
                             smpte_to_frames, validate_fps, validate_manifest)
from cinesync.pipeline.export_prep import build
from cinesync.pipeline.premiere_xml import convert


def stamp_cameras(recipe, root, ffprobe):
    for shot in recipe['shots']:
        for camera in shot['cameras'].values():
            stat = (root / camera['media']).stat()
            camera['timecode_metadata'] = {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}


class EngineTests(unittest.TestCase):
    def test_timecode_and_rollover(self):
        for fps in (24, 25, 30, 60):
            for frame in (0, 1, 1234, 86400 * fps - 1):
                self.assertEqual(smpte_to_frames(frames_to_smpte(frame, fps), fps), frame)
        self.assertEqual(frames_to_smpte(86400 * 30, 30), "00:00:00:00")
        self.assertEqual(frames_to_smpte(smpte_to_frames("15:14:25:00", 30) + 12, 30), "15:14:25:12")

    def test_invalid_rates_and_timecodes(self):
        for value in (29.97, 23.976, 0, True, float("nan")):
            with self.assertRaises(ValidationError):
                validate_fps(value)
        for tc in ("24:00:00:00", "00:60:00:00", "00:00:00:30", "00:00:00;00"):
            with self.assertRaises(ValidationError):
                smpte_to_frames(tc, 30)

    def test_boundaries(self):
        shot = {"maya_start_frame": 1001, "duration_frames": 150}
        self.assertEqual([image_offset(f, shot) for f in (0, 1001, 1013.9, 1150, 2000)],
                         [0, 0, 12, 149, 149])

    def test_manifest_security(self):
        manifest = {"project": "P", "sequence_name": "S", "fps": 30,
                    "shots": [{"shot_id": "SH010", "timeline_in_frame": 0,
                               "timeline_out_frame": 2, "duration_frames": 2,
                               "maya_start_frame": 1001, "source_timecode": "00:00:00:00",
                               "cameras": {"front": {"label": "Front", "type": "body",
                                                      "folder": "footage", "pattern": "a.####.jpg", "start_frame": 1}}}]}
        with tempfile.TemporaryDirectory() as tmp:
            validate_manifest(manifest, Path(tmp))
            bad = copy.deepcopy(manifest)
            bad["shots"][0]["cameras"]["front"]["folder"] = "../escape"
            with self.assertRaises(ValidationError):
                validate_manifest(bad, Path(tmp))
            with self.assertRaises(ValidationError):
                validate_manifest(manifest, Path(tmp), check_files=True)


class ExportTests(unittest.TestCase):
    def test_atomic_output_and_failure_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recipe = json.loads(Path("examples/recipe.json").read_text())
            recipe["shots"][0].pop("audio")
            recipe["shots"][0]["timeline_out_frame"] = 2
            for camera in recipe["shots"][0]["cameras"].values():
                camera["media"] = "source.mov"
            (root / "source.mov").touch()
            face = recipe["shots"][0]["cameras"]["hmc_face"]
            face.update(media="face.mov", source_fps=60, source_in_frame=20)
            (root / "face.mov").touch()
            (root / "recipe.json").write_text(json.dumps(recipe))

            def fake_ffmpeg(args):
                pattern = args[-1]
                if "hmc_face" in pattern:
                    filters = args[args.index("-vf") + 1]
                    self.assertIn(r"trim=start_frame=20:end_frame=24,select=not(mod(n\,2))", filters)
                for frame in (1, 2):
                    Path(pattern % frame).write_bytes(b"test jpeg placeholder")
                return ""

            with patch("cinesync.pipeline.export_prep.verify_camera_timecodes", side_effect=stamp_cameras), \
                    patch("cinesync.pipeline.export_prep.verify_video") as verify, \
                    patch("cinesync.pipeline.export_prep.run", side_effect=fake_ffmpeg):
                manifest = build(str(root / "recipe.json"), str(root / "package"))
                self.assertTrue(manifest.is_file())
                data = json.loads(manifest.read_text())
                self.assertEqual(data["shots"][0]["duration_frames"], 2)
                self.assertEqual(data["shots"][0]["cameras"]["hmc_face"]["source_fps"], 60)
                verify.assert_any_call((root / "face.mov").resolve(), 60, "ffprobe")
            with patch("cinesync.pipeline.export_prep.verify_camera_timecodes", side_effect=stamp_cameras), patch("cinesync.pipeline.export_prep.verify_video"), patch("cinesync.pipeline.export_prep.run", side_effect=ValidationError("decode failed")):
                with self.assertRaises(ValidationError):
                    build(str(root / "recipe.json"), str(root / "failed"))
            self.assertFalse((root / "failed").exists())
            self.assertFalse(list(root.glob(".cinesync-*")))


class XMLTests(unittest.TestCase):
    def test_source_offset_and_retiming_rejection(self):
        xml = '''<xmeml><sequence><name>S</name><rate><timebase>30</timebase></rate>
        <media><video><track><clipitem><start>0</start><end>100</end><in>10</in><out>110</out>
        <file id="f"><pathurl>file://localhost/tmp/front.mov</pathurl>
        <rate><timebase>30</timebase></rate><timecode><string>15:14:25:00</string></timecode></file>
        </clipitem></track></video></media></sequence></xmeml>'''
        config = {"project": "P", "sequence_name": "S", "primary_camera": "front",
                  "cameras": {"front": {"track": 1, "label": "Front", "type": "body"}},
                  "shots": [{"shot_id": "SH010", "timeline_in_frame": 20, "timeline_out_frame": 40}]}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "input.xml").write_text(xml)
            (root / "config.json").write_text(json.dumps(config))
            result = convert(str(root / "input.xml"), str(root / "config.json"))
            shot = result["shots"][0]
            self.assertEqual(shot["cameras"]["front"]["source_in_frame"], 30)
            self.assertEqual(shot["source_timecode"], "15:14:26:00")
            high_rate = xml.replace("<in>10</in><out>110</out>", "<in>20</in><out>220</out>")
            high_rate = high_rate.replace("front.mov</pathurl>\n        <rate><timebase>30",
                                          "front.mov</pathurl>\n        <rate><timebase>60")
            for variant in (high_rate, high_rate.replace("<in>20</in><out>220</out>",
                    "<rate><timebase>30</timebase></rate><in>10</in><out>110</out>")):
                (root / "input.xml").write_text(variant)
                camera = convert(str(root / "input.xml"), str(root / "config.json"))["shots"][0]["cameras"]["front"]
                self.assertEqual(camera["source_fps"], 60)
                self.assertEqual(camera["source_in_frame"], 60)
                self.assertEqual(camera["source_timecode"], "15:14:26:00")
            (root / "input.xml").write_text(xml.replace("<out>110</out>", "<out>210</out>"))
            with self.assertRaises(ValidationError):
                convert(str(root / "input.xml"), str(root / "config.json"))


if __name__ == "__main__":
    unittest.main()
