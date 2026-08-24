import os
import json
import uuid
import subprocess

from flask import Flask, request, jsonify, send_file

app = Flask(__name__)

WORK_DIR = "/tmp/reels_editor"
os.makedirs(WORK_DIR, exist_ok=True)


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "ok",
        "service": "reels-video-editor"
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok"
    })


@app.route("/edit", methods=["POST"])
def edit_video():

    if "video" not in request.files:
        return jsonify({
            "error": "Video file is required"
        }), 400

    video = request.files["video"]

    actions_raw = request.form.get("actions", "[]")

    try:
        actions = json.loads(actions_raw)
    except Exception:
        return jsonify({
            "error": "Invalid actions JSON"
        }), 400

    job_id = str(uuid.uuid4())

    input_path = os.path.join(
        WORK_DIR,
        f"{job_id}_input.mp4"
    )

    output_path = os.path.join(
        WORK_DIR,
        f"{job_id}_output.mp4"
    )

    video.save(input_path)

    # -----------------------------
    # Собираем CUT-команды
    # -----------------------------

    cuts = []

    for action in actions:

        if action.get("action") == "CUT":

            start = float(action.get("start", 0))
            end = float(action.get("end", 0))

            if end > start:
                cuts.append((start, end))

    cuts.sort()

    try:

        # Пока выполняем первый этап:
        # физически удаляем CUT-участки.
        #
        # TEXT / ZOOM / GRAPHIC добавим
        # после проверки API.

        if not cuts:

            command = [
                "ffmpeg",
                "-y",
                "-i", input_path,
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-c:a", "aac",
                "-movflags", "+faststart",
                output_path
            ]

        else:

            # Получаем длительность видео

            probe = [
                "ffprobe",
                "-v", "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                input_path
            ]

            duration = float(
                subprocess.check_output(probe)
                .decode()
                .strip()
            )

            keep_segments = []

            current = 0.0

            for start, end in cuts:

                if start > current:
                    keep_segments.append(
                        (current, start)
                    )

                current = max(current, end)

            if current < duration:
                keep_segments.append(
                    (current, duration)
                )

            if not keep_segments:
                return jsonify({
                    "error":
                    "CUT actions remove the entire video"
                }), 400

            filters = []
            concat_inputs = []

            for index, (start, end) in enumerate(
                keep_segments
            ):

                filters.append(
                    f"[0:v]trim=start={start}:end={end},"
                    f"setpts=PTS-STARTPTS[v{index}]"
                )

                filters.append(
                    f"[0:a]atrim=start={start}:end={end},"
                    f"asetpts=PTS-STARTPTS[a{index}]"
                )

                concat_inputs.append(
                    f"[v{index}][a{index}]"
                )

            filters.append(
                "".join(concat_inputs)
                + f"concat=n={len(keep_segments)}:"
                  "v=1:a=1[outv][outa]"
            )

            command = [
                "ffmpeg",
                "-y",
                "-i", input_path,
                "-filter_complex",
                ";".join(filters),
                "-map", "[outv]",
                "-map", "[outa]",
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-c:a", "aac",
                "-movflags", "+faststart",
                output_path
            ]

        subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

        return send_file(
            output_path,
            mimetype="video/mp4",
            as_attachment=True,
            download_name="edited_reel.mp4"
        )

    except subprocess.CalledProcessError as e:

        error_text = (
            e.stderr.decode(errors="ignore")
            if e.stderr
            else str(e)
        )

        return jsonify({
            "error": "FFmpeg processing failed",
            "details": error_text[-3000:]
        }), 500

    except Exception as e:

        return jsonify({
            "error": str(e)
        }), 500

    finally:

        # input удаляем сразу;
        # output Render сможет удалить позднее
        if os.path.exists(input_path):
            try:
                os.remove(input_path)
            except Exception:
                pass


if __name__ == "__main__":

    port = int(
        os.environ.get("PORT", 10000)
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
