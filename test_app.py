import io
import json
import os
import subprocess
import tempfile
import unittest

from app import (
    app,
    build_speech_segments,
    build_suggested_cuts,
    map_time_after_cuts,
    merge_cuts,
)


class ReelsEditorTestCase(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()
        self.previous_api_key = os.environ.pop(
            "VIDEO_EDITOR_API_KEY",
            None,
        )

    def tearDown(self):
        if self.previous_api_key is not None:
            os.environ["VIDEO_EDITOR_API_KEY"] = (
                self.previous_api_key
            )
        else:
            os.environ.pop(
                "VIDEO_EDITOR_API_KEY",
                None,
            )

    def test_health_and_capabilities(self):
        health = self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(
            health.get_json()["status"],
            "ok",
        )

        capabilities = self.client.get(
            "/capabilities"
        )
        data = capabilities.get_json()
        self.assertEqual(
            data["edit_endpoint"],
            "/edit",
        )
        self.assertIn(
            "SUBTITLE",
            data["supported_actions"],
        )

    def test_edit_requires_video(self):
        response = self.client.post(
            "/edit",
            data={"actions": "[]"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json()["error"],
            "Video file is required",
        )

    def test_analyze_requires_video(self):
        response = self.client.post("/analyze")
        self.assertEqual(response.status_code, 400)

    def test_api_key_is_enforced_when_configured(self):
        os.environ["VIDEO_EDITOR_API_KEY"] = "secret"
        response = self.client.post(
            "/edit",
            data={"actions": "[]"},
        )
        self.assertEqual(response.status_code, 401)

    def test_invalid_action_is_rejected(self):
        response = self.client.post(
            "/edit",
            data={
                "actions": json.dumps([
                    {"action": "DELETE_EVERYTHING"}
                ]),
                "video": (
                    io.BytesIO(b"not-a-video"),
                    "source.mp4",
                ),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(
            "Unsupported action",
            response.get_json()["details"],
        )

    def test_time_and_silence_helpers(self):
        self.assertEqual(
            merge_cuts([
                (2, 4),
                (3, 5),
                (8, 9),
            ]),
            [(2, 5), (8, 9)],
        )
        self.assertEqual(
            map_time_after_cuts(
                10,
                [(2, 5), (8, 9)],
            ),
            6,
        )

        silences = [
            {"start": 0, "end": 1},
            {"start": 4, "end": 5},
            {"start": 9, "end": 10},
        ]
        cuts = build_suggested_cuts(
            silences,
            10,
        )
        self.assertEqual(len(cuts), 3)
        speech = build_speech_segments(
            silences,
            10,
        )
        self.assertEqual(
            speech,
            [
                {"start": 1.0, "end": 4.0},
                {"start": 5.0, "end": 9.0},
            ],
        )

    def test_analyze_and_edit_small_video(self):
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(
                folder,
                "source.mp4",
            )

            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=blue:s=320x568:d=1",
                    "-f",
                    "lavfi",
                    "-i",
                    "anullsrc=r=44100:cl=mono",
                    "-shortest",
                    "-c:v",
                    "libx264",
                    "-c:a",
                    "aac",
                    source,
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            with open(source, "rb") as handle:
                analyzed = self.client.post(
                    "/analyze",
                    data={
                        "video": (
                            io.BytesIO(handle.read()),
                            "source.mp4",
                        )
                    },
                    content_type="multipart/form-data",
                )

            self.assertEqual(
                analyzed.status_code,
                200,
            )
            self.assertGreater(
                analyzed.get_json()["duration"],
                0,
            )

            with open(source, "rb") as handle:
                extracted = self.client.post(
                    "/extract-audio",
                    data={
                        "video": (
                            io.BytesIO(handle.read()),
                            "source.mp4",
                        )
                    },
                    content_type="multipart/form-data",
                )

            self.assertEqual(
                extracted.status_code,
                200,
            )
            self.assertEqual(
                extracted.mimetype,
                "audio/mpeg",
            )
            self.assertGreater(
                len(extracted.data),
                100,
            )
            extracted.close()

            with open(source, "rb") as handle:
                edited = self.client.post(
                    "/edit",
                    data={
                        "actions": "[]",
                        "video": (
                            io.BytesIO(handle.read()),
                            "source.mp4",
                        ),
                    },
                    content_type="multipart/form-data",
                )

            self.assertEqual(edited.status_code, 200)
            self.assertEqual(
                edited.mimetype,
                "video/mp4",
            )
            self.assertGreater(len(edited.data), 100)
            edited.close()


if __name__ == "__main__":
    unittest.main()
