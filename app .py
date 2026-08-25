import os
import re
import json
import uuid
import subprocess
import hmac

from flask import Flask, request, jsonify, send_file
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

app = Flask(__name__)

MAX_UPLOAD_MB = int(
    os.environ.get(
        "MAX_UPLOAD_MB",
        "250"
    )
)

app.config["MAX_CONTENT_LENGTH"] = (
    MAX_UPLOAD_MB * 1024 * 1024
)

WORK_DIR = "/tmp/reels_editor"
os.makedirs(WORK_DIR, exist_ok=True)

SERVICE_VERSION = "2.0.2"
OUTPUT_MAX_WIDTH = int(
    os.environ.get("OUTPUT_MAX_WIDTH", "720")
)
OUTPUT_MAX_HEIGHT = int(
    os.environ.get("OUTPUT_MAX_HEIGHT", "1280")
)
SUPPORTED_ACTIONS = {
    "CUT",
    "TEXT",
    "SUBTITLE",
    "ZOOM"
}


def remove_file(path):
    if not path or not os.path.exists(path):
        return

    try:
        os.remove(path)
    except OSError:
        pass


def check_api_key():
    expected = os.environ.get(
        "VIDEO_EDITOR_API_KEY",
        ""
    ).strip()

    # Backwards compatible for local development. On Render, set
    # VIDEO_EDITOR_API_KEY to require X-API-Key on editing requests.
    if not expected:
        return None

    provided = request.headers.get(
        "X-API-Key",
        ""
    ).strip()

    if hmac.compare_digest(
        expected,
        provided
    ):
        return None

    return jsonify({
        "error": "Unauthorized"
    }), 401


@app.errorhandler(RequestEntityTooLarge)
def handle_file_too_large(_error):
    return jsonify({
        "error": "Video file is too large",
        "max_upload_mb": MAX_UPLOAD_MB
    }), 413


# =========================================================
# HEALTH
# =========================================================

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "ok",
        "service": "reels-video-editor",
        "version": SERVICE_VERSION
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "version": SERVICE_VERSION
    })


@app.route("/capabilities", methods=["GET"])
def capabilities():
    return jsonify({
        "service": "reels-video-editor",
        "version": SERVICE_VERSION,
        "analyze_endpoint": "/analyze",
        "extract_audio_endpoint": "/extract-audio",
        "edit_endpoint": "/edit",
        "content_type": "multipart/form-data",
        "video_field": "video",
        "actions_field": "actions",
        "supported_actions": sorted(
            SUPPORTED_ACTIONS
        ),
        "max_upload_mb": MAX_UPLOAD_MB,
        "output_max_width": OUTPUT_MAX_WIDTH,
        "output_max_height": OUTPUT_MAX_HEIGHT,
        "api_key_required": bool(
            os.environ.get(
                "VIDEO_EDITOR_API_KEY",
                ""
            ).strip()
        )
    })


# =========================================================
# FFmpeg helpers
# =========================================================

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


def get_video_size(path):
    command = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "json",
        path
    ]

    raw = subprocess.check_output(command).decode()
    data = json.loads(raw)

    stream = data["streams"][0]

    return int(stream["width"]), int(stream["height"])


def get_safe_video_extension(filename):
    extension = os.path.splitext(
        secure_filename(filename or "")
    )[1].lower()

    if extension not in (
        ".mp4",
        ".mov",
        ".m4v",
        ".webm"
    ):
        return None

    return extension


def detect_silences(
    path,
    duration,
    noise_db=-35,
    minimum_duration=0.35
):
    command = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-i",
        path,
        # Silence detection only needs the audio stream. Disabling video
        # decoding makes analysis substantially faster on small Render
        # instances and keeps longer Reels below upstream request limits.
        "-map",
        "0:a:0",
        "-vn",
        "-af",
        (
            f"silencedetect=noise={noise_db}dB:"
            f"d={minimum_duration}"
        ),
        "-f",
        "null",
        "-"
    ]

    process = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )

    log = process.stderr.decode(
        errors="ignore"
    )

    starts = [
        float(value)
        for value in re.findall(
            r"silence_start:\s*([0-9.]+)",
            log
        )
    ]

    ends = [
        float(value)
        for value in re.findall(
            r"silence_end:\s*([0-9.]+)",
            log
        )
    ]

    silences = []

    for index, start in enumerate(starts):
        end = (
            ends[index]
            if index < len(ends)
            else duration
        )

        start = max(
            0.0,
            min(start, duration)
        )

        end = max(
            start,
            min(end, duration)
        )

        if end > start:
            silences.append({
                "start": round(start, 3),
                "end": round(end, 3),
                "duration": round(
                    end - start,
                    3
                )
            })

    return silences


def build_suggested_cuts(
    silences,
    duration
):
    suggestions = []

    for silence in silences:
        start = float(silence["start"])
        end = float(silence["end"])
        silence_duration = end - start

        if start <= 0.15 and end > 0.25:
            cut_start = 0.0
            cut_end = max(
                0.0,
                end - 0.12
            )
        elif end >= duration - 0.15 and silence_duration > 0.25:
            cut_start = min(
                duration,
                start + 0.12
            )
            cut_end = duration
        elif silence_duration >= 0.65:
            cut_start = start + 0.18
            cut_end = end - 0.18
        else:
            continue

        if cut_end > cut_start:
            suggestions.append({
                "action": "CUT",
                "start": round(
                    cut_start,
                    3
                ),
                "end": round(
                    cut_end,
                    3
                ),
                "reason": "detected_silence"
            })

    return suggestions


def build_speech_segments(
    silences,
    duration
):
    segments = []
    current = 0.0

    for silence in silences:
        start = float(silence["start"])
        end = float(silence["end"])

        if start - current >= 0.15:
            segments.append({
                "start": round(current, 3),
                "end": round(start, 3)
            })

        current = max(current, end)

    if duration - current >= 0.15:
        segments.append({
            "start": round(current, 3),
            "end": round(duration, 3)
        })

    return segments


@app.route("/analyze", methods=["POST"])
def analyze_video():
    auth_error = check_api_key()

    if auth_error:
        return auth_error

    if "video" not in request.files:
        return jsonify({
            "error": "Video file is required"
        }), 400

    video = request.files["video"]
    extension = get_safe_video_extension(
        video.filename
    )

    if not extension:
        return jsonify({
            "error": "Unsupported video format",
            "supported_extensions": [
                ".mp4",
                ".mov",
                ".m4v",
                ".webm"
            ]
        }), 400

    job_id = str(uuid.uuid4())
    input_path = os.path.join(
        WORK_DIR,
        f"{job_id}_analyze{extension}"
    )

    try:
        video.save(input_path)
        duration = get_duration(input_path)
        width, height = get_video_size(
            input_path
        )
        silences = detect_silences(
            input_path,
            duration
        )

        return jsonify({
            "job_id": job_id,
            "duration": round(duration, 3),
            "width": width,
            "height": height,
            "aspect_ratio": round(
                width / height,
                4
            ) if height else None,
            "silences": silences,
            "speech_segments": build_speech_segments(
                silences,
                duration
            ),
            "suggested_cuts": build_suggested_cuts(
                silences,
                duration
            )
        })

    except subprocess.CalledProcessError as error:
        details = (
            error.stderr.decode(errors="ignore")
            if error.stderr
            else str(error)
        )

        return jsonify({
            "error": "Video analysis failed",
            "details": details[-3000:]
        }), 500

    except Exception as error:
        return jsonify({
            "error": "Video analysis failed",
            "details": str(error)
        }), 500

    finally:
        remove_file(input_path)


@app.route("/extract-audio", methods=["POST"])
def extract_audio():
    auth_error = check_api_key()

    if auth_error:
        return auth_error

    if "video" not in request.files:
        return jsonify({
            "error": "Video file is required"
        }), 400

    video = request.files["video"]
    extension = get_safe_video_extension(
        video.filename
    )

    if not extension:
        return jsonify({
            "error": "Unsupported video format"
        }), 400

    job_id = str(uuid.uuid4())
    input_path = os.path.join(
        WORK_DIR,
        f"{job_id}_audio_input{extension}"
    )
    output_path = os.path.join(
        WORK_DIR,
        f"{job_id}_audio.mp3"
    )

    try:
        video.save(input_path)
        run_ffmpeg([
            "ffmpeg",
            "-y",
            "-i",
            input_path,
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-b:a",
            "64k",
            output_path
        ])

        response = send_file(
            output_path,
            mimetype="audio/mpeg",
            as_attachment=True,
            download_name="speech.mp3"
        )
        response.headers[
            "X-Reels-Editor-Job-Id"
        ] = job_id
        response.call_on_close(
            lambda: remove_file(output_path)
        )
        return response

    except subprocess.CalledProcessError as error:
        remove_file(output_path)
        details = (
            error.stderr.decode(errors="ignore")
            if error.stderr
            else str(error)
        )
        return jsonify({
            "error": "Audio extraction failed",
            "details": details[-3000:]
        }), 500

    except Exception as error:
        remove_file(output_path)
        return jsonify({
            "error": "Audio extraction failed",
            "details": str(error)
        }), 500

    finally:
        remove_file(input_path)


# =========================================================
# CUT helpers
# =========================================================

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


def map_time_after_cuts(time_value, cuts):

    time_value = float(time_value)

    removed = 0.0

    for start, end in cuts:

        if time_value >= end:

            removed += (
                end - start
            )

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


# =========================================================
# ASS subtitles
# =========================================================

def ass_time(seconds):

    seconds = max(
        0.0,
        float(seconds)
    )

    hours = int(
        seconds // 3600
    )

    minutes = int(
        (seconds % 3600) // 60
    )

    secs = seconds % 60

    return (
        f"{hours}:"
        f"{minutes:02d}:"
        f"{secs:05.2f}"
    )


def escape_ass_text(text):

    text = str(text)

    # ASS управляющие символы
    text = text.replace(
        "\\",
        r"\\"
    )

    text = text.replace(
        "{",
        r"\{"
    )

    text = text.replace(
        "}",
        r"\}"
    )

    text = text.replace(
        "\n",
        r"\N"
    )

    return text


def add_highlight(text, highlight):

    text = str(text).strip()
    highlight = str(highlight).strip()

    safe_text = escape_ass_text(
        text
    )

    if not highlight:
        return safe_text

    # Ищем highlight внутри исходного текста
    pattern = re.compile(
        re.escape(highlight),
        re.IGNORECASE
    )

    match = pattern.search(
        text
    )

    if not match:
        return safe_text

    before = escape_ass_text(
        text[:match.start()]
    )

    selected = escape_ass_text(
        text[
            match.start():
            match.end()
        ]
    )

    after = escape_ass_text(
        text[match.end():]
    )

    # ASS:
    # жёлтый = BBGGRR = 00FFFF
    # белый = FFFFFF

    return (
        before
        + r"{\c&H0000FFFF&}"
        + selected
        + r"{\c&H00FFFFFF&}"
        + after
    )


def create_ass_file(
    path,
    width,
    height,
    subtitles
):

    # примерно как fontsize=h*0.022
    font_size = max(
        18,
        int(height * 0.022)
    )

    # Субтитры располагаем примерно
    # в районе 72% высоты кадра
    bottom_margin = int(
        height * 0.24
    )

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
ScaledBorderAndShadow: yes
WrapStyle: 2

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Default,DejaVu Sans,{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,3,1,0,2,35,35,{bottom_margin},1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""

    lines = [
        header
    ]

    for item in subtitles:

        start = ass_time(
            item["start"]
        )

        end = ass_time(
            item["end"]
        )

        formatted_text = add_highlight(
            item["text"],
            item.get(
                "highlight",
                ""
            )
        )

        line = (
            f"Dialogue: 0,"
            f"{start},"
            f"{end},"
            f"Default,,0,0,0,,"
            f"{formatted_text}\n"
        )

        lines.append(
            line
        )

    with open(
        path,
        "w",
        encoding="utf-8"
    ) as file:

        file.writelines(
            lines
        )


def fit_dimensions(
    width,
    height,
    max_width=OUTPUT_MAX_WIDTH,
    max_height=OUTPUT_MAX_HEIGHT
):
    """Fit a video inside the configured render box using even sizes."""

    if width <= 0 or height <= 0:
        raise ValueError("Invalid video dimensions")

    scale = min(
        1.0,
        max_width / width,
        max_height / height
    )

    fitted_width = max(
        2,
        int(width * scale) // 2 * 2
    )
    fitted_height = max(
        2,
        int(height * scale) // 2 * 2
    )

    return fitted_width, fitted_height


def build_zoom_filters(
    zooms,
    width,
    height
):
    """Render every timed zoom with one scale/crop pair."""

    if not zooms:
        return []

    factor = "1"

    for item in reversed(zooms):
        factor = (
            f"if(between(t,{item['start']},{item['end']}),"
            f"{item['scale']},{factor})"
        )

    return [
        (
            "scale="
            f"w='trunc({width}*({factor})/2)*2':"
            f"h='trunc({height}*({factor})/2)*2':"
            "eval=frame"
        ),
        (
            "crop="
            f"w={width}:h={height}:"
            "x='(iw-ow)/2':y='(ih-oh)/2'"
        ),
        "setsar=1"
    ]


def render_video_single_pass(
    input_path,
    output_path,
    ass_path,
    cuts,
    text_actions,
    zoom_actions
):
    """Apply cuts, zooms and subtitles in one FFmpeg encode."""

    duration = get_duration(input_path)
    source_width, source_height = get_video_size(
        input_path
    )
    output_width, output_height = fit_dimensions(
        source_width,
        source_height
    )

    bounded_cuts = []

    for start, end in cuts:
        start = max(0.0, min(float(start), duration))
        end = max(0.0, min(float(end), duration))

        if end > start:
            bounded_cuts.append((start, end))

    bounded_cuts = merge_cuts(bounded_cuts)
    keep_segments = []
    current = 0.0

    for start, end in bounded_cuts:
        if start > current:
            keep_segments.append((current, start))
        current = max(current, end)

    if current < duration:
        keep_segments.append((current, duration))

    if not keep_segments:
        raise ValueError(
            "CUT actions remove the entire video"
        )

    final_duration = sum(
        end - start
        for start, end in keep_segments
    )

    mapped_zooms = []

    for item in zoom_actions:
        start = map_time_after_cuts(
            item["start"],
            bounded_cuts
        )
        end = map_time_after_cuts(
            item["end"],
            bounded_cuts
        )
        start = max(0.0, min(start, final_duration))
        end = max(0.0, min(end, final_duration))

        if end > start:
            mapped_zooms.append({
                "start": round(start, 3),
                "end": round(end, 3),
                "scale": item["scale"]
            })

    mapped_texts = []

    for item in text_actions:
        start = map_time_after_cuts(
            item["start"],
            bounded_cuts
        )
        end = map_time_after_cuts(
            item["end"],
            bounded_cuts
        )
        start = max(0.0, min(start, final_duration))
        end = max(0.0, min(end, final_duration))

        if end > start:
            mapped_texts.append({
                "start": start,
                "end": end,
                "text": item["text"],
                "highlight": item.get("highlight", "")
            })

    if mapped_texts:
        create_ass_file(
            ass_path,
            output_width,
            output_height,
            mapped_texts
        )

    filters = []

    if bounded_cuts:
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
            + f"concat=n={len(keep_segments)}:v=1:a=1"
            + "[basev][outa]"
        )
    else:
        filters.extend([
            "[0:v]null[basev]",
            "[0:a]anull[outa]"
        ])

    video_filters = [
        (
            f"scale={output_width}:{output_height}:"
            "flags=fast_bilinear"
        )
    ]
    video_filters.extend(
        build_zoom_filters(
            mapped_zooms,
            output_width,
            output_height
        )
    )

    if mapped_texts:
        video_filters.append(
            f"subtitles={ass_path}"
        )

    filters.append(
        "[basev]"
        + ",".join(video_filters)
        + "[outv]"
    )

    run_ffmpeg([
        "ffmpeg",
        "-y",
        "-i",
        input_path,
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[outv]",
        "-map",
        "[outa]",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "28",
        "-pix_fmt",
        "yuv420p",
        "-threads",
        "0",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-movflags",
        "+faststart",
        output_path
    ])


# =========================================================
# EDIT
# =========================================================

@app.route("/edit", methods=["POST"])
def edit_video():

    auth_error = check_api_key()

    if auth_error:
        return auth_error

    if "video" not in request.files:

        return jsonify({
            "error":
            "Video file is required"
        }), 400


    video = request.files[
        "video"
    ]


    if not video.filename:

        return jsonify({
            "error":
            "Video filename is required"
        }), 400


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

        if len(actions) > 500:
            raise ValueError(
                "actions limit is 500"
            )

        for index, action in enumerate(actions):
            if not isinstance(action, dict):
                raise ValueError(
                    f"actions[{index}] must be object"
                )

            action_type = str(
                action.get(
                    "action",
                    ""
                )
            ).upper().strip()

            if action_type not in SUPPORTED_ACTIONS:
                raise ValueError(
                    f"Unsupported action at index {index}: "
                    f"{action_type or 'empty'}"
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


    safe_extension = get_safe_video_extension(
        video.filename
    )

    if not safe_extension:

        return jsonify({
            "error": "Unsupported video format",
            "supported_extensions": [
                ".mp4",
                ".mov",
                ".m4v",
                ".webm"
            ]
        }), 400


    input_path = os.path.join(
        WORK_DIR,
        f"{job_id}_input{safe_extension}"
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

    ass_path = os.path.join(
        WORK_DIR,
        f"{job_id}_subtitles.ass"
    )


    video.save(
        input_path
    )


    cuts = []
    text_actions = []
    zoom_actions = []


    try:

        # =================================================
        # READ ACTIONS
        # =================================================

        for action in actions:

            action_type = str(
                action.get(
                    "action",
                    ""
                )
            ).upper().strip()


            # ----------------------
            # CUT
            # ----------------------

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


            # ----------------------
            # TEXT / SUBTITLE
            # ----------------------

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
                    text
                    and
                    end > start
                ):

                    text_actions.append({
                        "start":
                            start,

                        "end":
                            end,

                        "text":
                            text,

                        "highlight":
                            highlight
                    })


            # ----------------------
            # ZOOM
            # ----------------------

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

        # Render the complete edit in one encode. The former three-pass
        # pipeline (cut -> zoom -> subtitles) exceeded Render's upstream
        # request window for longer videos.
        render_video_single_pass(
            input_path,
            output_path,
            ass_path,
            cuts,
            text_actions,
            zoom_actions
        )

        output_name = secure_filename(
            request.form.get(
                "output_name",
                "edited_reel.mp4"
            )
        )

        if not output_name:
            output_name = "edited_reel.mp4"
        elif not output_name.lower().endswith(".mp4"):
            output_name += ".mp4"

        response = send_file(
            output_path,
            mimetype="video/mp4",
            as_attachment=True,
            download_name=output_name
        )
        response.headers[
            "X-Reels-Editor-Job-Id"
        ] = job_id
        response.call_on_close(
            lambda: remove_file(output_path)
        )
        return response


        # =================================================
        # STAGE 1 — CUT
        # =================================================

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


        # =================================================
        # STAGE 2 — ZOOM
        # =================================================

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

                start = item[
                    "start"
                ]

                end = item[
                    "end"
                ]

                scale = item[
                    "scale"
                ]


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


        # =================================================
        # STAGE 3 — SUBTITLES
        # =================================================

        mapped_texts = []


        final_duration = get_duration(
            zoom_path
        )


        for item in text_actions:

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
                    final_duration
                )
            )

            end = max(
                0.0,
                min(
                    end,
                    final_duration
                )
            )


            if end <= start:
                continue


            mapped_texts.append({
                "start":
                    start,

                "end":
                    end,

                "text":
                    item["text"],

                "highlight":
                    item.get(
                        "highlight",
                        ""
                    )
            })


        if mapped_texts:

            width, height = get_video_size(
                zoom_path
            )


            create_ass_file(
                ass_path,
                width,
                height,
                mapped_texts
            )


            subtitle_filter = (
                f"subtitles={ass_path}"
            )


            text_command = [
                "ffmpeg",
                "-y",

                "-i",
                zoom_path,

                "-vf",
                subtitle_filter,

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


        # =================================================
        # RETURN
        # =================================================

        output_name = secure_filename(
            request.form.get(
                "output_name",
                "edited_reel.mp4"
            )
        )

        if not output_name:
            output_name = "edited_reel.mp4"

        elif not output_name.lower().endswith(
            ".mp4"
        ):
            output_name += ".mp4"

        response = send_file(
            output_path,

            mimetype=
                "video/mp4",

            as_attachment=
                True,

            download_name=
                output_name
        )

        response.headers[
            "X-Reels-Editor-Job-Id"
        ] = job_id

        response.call_on_close(
            lambda: remove_file(
                output_path
            )
        )

        return response


    except subprocess.CalledProcessError as e:

        remove_file(output_path)

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

        remove_file(output_path)

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
            ass_path
        ]


        for path in cleanup_paths:
            remove_file(path)


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
