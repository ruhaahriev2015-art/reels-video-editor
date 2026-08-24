import os
import json
import uuid
import subprocess

from flask import Flask, request, jsonify, send_file

app = Flask(__name__)

WORK_DIR = "/tmp/reels_editor"
os.makedirs(WORK_DIR, exist_ok=True)

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


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


def run_ffmpeg(command):
    subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )


def get_duration(path):
    command = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path
    ]

    return float(
        subprocess.check_output(command)
        .decode()
        .strip()
    )


def map_time_after_cuts(time_value, cuts):
    time_value = float(time_value)
    removed = 0.0

    for start, end in cuts:

        if time_value >= end:
            removed += end - start

        elif time_value > start:
            return max(
                0.0,
                start - removed
            )

        else:
            break

    return max(
        0.0,
        time_value - removed
    )


def merge_cuts(cuts):
    if not cuts:
        return []

    cuts = sorted(cuts)
    merged = [cuts[0]]

    for start, end in cuts[1:]:

        last_start, last_end = merged[-1]

        if start <= last_end:
            merged[-1] = (
                last_start,
                max(last_end, end)
            )
        else:
            merged.append(
                (start, end)
            )

    return merged


@app.route("/edit", methods=["POST"])
def edit_video():

    if "video" not in request.files:
        return jsonify({
            "error": "Video file is required"
        }), 400

    video = request.files["video"]

    actions_raw = request.form.get(
        "actions",
        "[]"
    )

    try:
        actions = json.loads(
            actions_raw
        )

        if not isinstance(
            actions,
            list
        ):
            raise ValueError(
                "actions must be list"
            )

    except Exception as e:

        return jsonify({
            "error":
                "Invalid actions JSON",
            "details":
                str(e)
        }), 400


    job_id = str(
        uuid.uuid4()
    )


    input_path = os.path.join(
        WORK_DIR,
        f"{job_id}_input.mp4"
    )

    cut_path = os.path.join(
        WORK_DIR,
        f"{job_id}_cut.mp4"
    )

    zoom_path = os.path.join(
        WORK_DIR,
        f"{job_id}_zoom.mp4"
    )

    output_path = os.path.join(
        WORK_DIR,
        f"{job_id}_output.mp4"
    )


    text_files = []
    highlight_files = []


    video.save(
        input_path
    )


    cuts = []
    text_actions = []
    zoom_actions = []


    try:

        # ==================================
        # ЧИТАЕМ МОНТАЖНЫЕ КОМАНДЫ
        # ==================================

        for action in actions:

            action_type = str(
                action.get(
                    "action",
                    ""
                )
            ).upper().strip()


            # --------------------------
            # CUT
            # --------------------------

            if action_type == "CUT":

                start = float(
                    action.get(
                        "start",
                        0
                    )
                )

                end = float(
                    action.get(
                        "end",
                        0
                    )
                )

                if end > start:

                    cuts.append(
                        (
                            start,
                            end
                        )
                    )


            # --------------------------
            # TEXT / SUBTITLE
            # --------------------------

            elif action_type in (
                "TEXT",
                "SUBTITLE"
            ):

                start = float(
                    action.get(
                        "start",
                        0
                    )
                )

                end = float(
                    action.get(
                        "end",
                        start + 2
                    )
                )

                text = str(
                    action.get(
                        "text",
                        ""
                    )
                ).strip()

                highlight = str(
                    action.get(
                        "highlight",
                        ""
                    )
                ).strip()


                if (
                    text and
                    end > start
                ):

                    text_actions.append({
                        "start": start,
                        "end": end,
                        "text": text,
                        "highlight": highlight
                    })


            # --------------------------
            # ZOOM
            # --------------------------

            elif action_type == "ZOOM":

                start = float(
                    action.get(
                        "start",
                        0
                    )
                )

                end = float(
                    action.get(
                        "end",
                        start + 1
                    )
                )

                scale = float(
                    action.get(
                        "scale",
                        1.12
                    )
                )


                scale = max(
                    1.01,
                    min(
                        scale,
                        1.35
                    )
                )


                if end > start:

                    zoom_actions.append({
                        "start":
                            start,

                        "end":
                            end,

                        "scale":
                            scale
                    })


        cuts = merge_cuts(
            cuts
        )


        # ==================================
        # ЭТАП 1 — CUT
        # ==================================

        if not cuts:

            command = [
                "ffmpeg",
                "-y",

                "-i",
                input_path,

                "-c:v",
                "libx264",

                "-preset",
                "ultrafast",

                "-threads",
                "1",

                "-c:a",
                "aac",

                "-b:a",
                "128k",

                "-movflags",
                "+faststart",

                cut_path
            ]

            run_ffmpeg(
                command
            )


        else:

            duration = get_duration(
                input_path
            )


            keep_segments = []
            current = 0.0


            for start, end in cuts:

                start = max(
                    0.0,
                    min(
                        start,
                        duration
                    )
                )

                end = max(
                    0.0,
                    min(
                        end,
                        duration
                    )
                )


                if start > current:

                    keep_segments.append(
                        (
                            current,
                            start
                        )
                    )


                current = max(
                    current,
                    end
                )


            if current < duration:

                keep_segments.append(
                    (
                        current,
                        duration
                    )
                )


            if not keep_segments:

                return jsonify({
                    "error":
                    "CUT actions remove the entire video"
                }), 400


            filters = []
            concat_inputs = []


            for index, (
                start,
                end
            ) in enumerate(
                keep_segments
            ):

                filters.append(
                    f"[0:v]"
                    f"trim=start={start}:end={end},"
                    f"setpts=PTS-STARTPTS"
                    f"[v{index}]"
                )

                filters.append(
                    f"[0:a]"
                    f"atrim=start={start}:end={end},"
                    f"asetpts=PTS-STARTPTS"
                    f"[a{index}]"
                )

                concat_inputs.append(
                    f"[v{index}]"
                    f"[a{index}]"
                )


            filters.append(
                "".join(
                    concat_inputs
                )
                +
                f"concat=n={len(keep_segments)}:"
                f"v=1:a=1"
                f"[outv][outa]"
            )


            command = [
                "ffmpeg",
                "-y",

                "-i",
                input_path,

                "-filter_complex",
                ";".join(
                    filters
                ),

                "-map",
                "[outv]",

                "-map",
                "[outa]",

                "-c:v",
                "libx264",

                "-preset",
                "ultrafast",

                "-threads",
                "1",

                "-c:a",
                "aac",

                "-b:a",
                "128k",

                "-movflags",
                "+faststart",

                cut_path
            ]


            run_ffmpeg(
                command
            )


        # ==================================
        # ЭТАП 2 — ZOOM
        # ==================================

        mapped_zooms = []

        cut_duration = get_duration(
            cut_path
        )


        for item in zoom_actions:

            start = map_time_after_cuts(
                item["start"],
                cuts
            )

            end = map_time_after_cuts(
                item["end"],
                cuts
            )


            start = max(
                0.0,
                min(
                    start,
                    cut_duration
                )
            )

            end = max(
                0.0,
                min(
                    end,
                    cut_duration
                )
            )


            if end > start:

                mapped_zooms.append({
                    "start":
                        start,

                    "end":
                        end,

                    "scale":
                        item["scale"]
                })


        if mapped_zooms:

            zoom_filters = []


            for item in mapped_zooms:

                start = item["start"]
                end = item["end"]
                scale = item["scale"]


                zoom_filters.append(

                    "scale="

                    f"w='if(between(t,{start},{end}),"
                    f"trunc(iw*{scale}/2)*2,iw)':"

                    f"h='if(between(t,{start},{end}),"
                    f"trunc(ih*{scale}/2)*2,ih)':"

                    "eval=frame"
                )


                zoom_filters.append(

                    "crop="

                    "w='iw/"
                    f"if(between(t,{start},{end}),"
                    f"{scale},1)':"

                    "h='ih/"
                    f"if(between(t,{start},{end}),"
                    f"{scale},1)':"

                    "x='(iw-ow)/2':"

                    "y='(ih-oh)/2'"
                )


            zoom_filters.append(
                "setsar=1"
            )


            zoom_command = [
                "ffmpeg",
                "-y",

                "-i",
                cut_path,

                "-vf",
                ",".join(
                    zoom_filters
                ),

                "-c:v",
                "libx264",

                "-preset",
                "ultrafast",

                "-threads",
                "1",

                "-c:a",
                "copy",

                "-movflags",
                "+faststart",

                zoom_path
            ]


            run_ffmpeg(
                zoom_command
            )


        else:

            os.replace(
                cut_path,
                zoom_path
            )


        # ==================================
        # ЭТАП 3 — СУБТИТРЫ + HIGHLIGHT
        # ==================================

        drawtext_filters = []


        for index, item in enumerate(
            text_actions
        ):

            start = map_time_after_cuts(
                item["start"],
                cuts
            )

            end = map_time_after_cuts(
                item["end"],
                cuts
            )


            if end <= start:
                continue


            # --------------------------
            # Основной субтитр
            # --------------------------

            text_file = os.path.join(
                WORK_DIR,
                f"{job_id}_text_{index}.txt"
            )


            with open(
                text_file,
                "w",
                encoding="utf-8"
            ) as f:

                f.write(
                    item["text"]
                )


            text_files.append(
                text_file
            )


            drawtext_filters.append(

                "drawtext="

                f"fontfile={FONT_PATH}:"

                f"textfile={text_file}:"

                "fontsize=h*0.022:"

                "fontcolor=white:"

                "borderw=1:"

                "bordercolor=black@0.85:"

                "box=1:"

                "boxcolor=black@0.30:"

                "boxborderw=4:"

                "x=(w-text_w)/2:"

                "y=h*0.72:"

                f"enable='between(t,{start},{end})'"
            )


            # --------------------------
            # HIGHLIGHT
            # --------------------------

            highlight = str(
                item.get(
                    "highlight",
                    ""
                )
            ).strip()


            if highlight:

                highlight_file = os.path.join(
                    WORK_DIR,
                    f"{job_id}_highlight_{index}.txt"
                )


                with open(
                    highlight_file,
                    "w",
                    encoding="utf-8"
                ) as f:

                    f.write(
                        highlight
                    )


                highlight_files.append(
                    highlight_file
                )


                drawtext_filters.append(

                    "drawtext="

                    f"fontfile={FONT_PATH}:"

                    f"textfile={highlight_file}:"

                    # чуть крупнее основного текста
                    "fontsize=h*0.026:"

                    # яркий акцент
                    "fontcolor=yellow:"

                    "borderw=1:"

                    "bordercolor=black@0.90:"

                    "box=1:"

                    "boxcolor=black@0.35:"

                    "boxborderw=4:"

                    "x=(w-text_w)/2:"

                    # располагаем над субтитром
                    "y=h*0.665:"

                    f"enable='between(t,{start},{end})'"
                )


        # ==================================
        # РЕНДЕР ТЕКСТА
        # ==================================

        if drawtext_filters:

            text_command = [
                "ffmpeg",
                "-y",

                "-i",
                zoom_path,

                "-vf",
                ",".join(
                    drawtext_filters
                ),

                "-c:v",
                "libx264",

                "-preset",
                "ultrafast",

                "-threads",
                "1",

                "-c:a",
                "copy",

                "-movflags",
                "+faststart",

                output_path
            ]


            run_ffmpeg(
                text_command
            )


        else:

            os.replace(
                zoom_path,
                output_path
            )


        # ==================================
        # ОТПРАВЛЯЕМ VIDEO
        # ==================================

        return send_file(
            output_path,

            mimetype=
                "video/mp4",

            as_attachment=
                True,

            download_name=
                "edited_reel.mp4"
        )


    except subprocess.CalledProcessError as e:

        error_text = (

            e.stderr.decode(
                errors="ignore"
            )

            if e.stderr

            else str(e)
        )


        print(
            "FFMPEG ERROR:",
            error_text
        )


        return jsonify({

            "error":
                "FFmpeg processing failed",

            "details":
                error_text[-6000:]

        }), 500


    except Exception as e:

        print(
            "SERVER ERROR:",
            str(e)
        )


        return jsonify({
            "error":
                str(e)
        }), 500


    finally:

        cleanup_paths = [

            input_path,
            cut_path,
            zoom_path,

            *text_files,
            *highlight_files
        ]


        for path in cleanup_paths:

            if os.path.exists(
                path
            ):

                try:

                    os.remove(
                        path
                    )

                except Exception:

                    pass


if __name__ == "__main__":

    port = int(

        os.environ.get(
            "PORT",
            10000
        )
    )


    app.run(
        host="0.0.0.0",
        port=port
    )
