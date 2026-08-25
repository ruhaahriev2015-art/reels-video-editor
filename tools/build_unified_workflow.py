#!/usr/bin/env python3
"""Build the unified scenario + video editing n8n workflow."""

import argparse
import copy
import json
from pathlib import Path


EDITOR_URL_DEFAULT = "https://reels-video-editor.onrender.com"


def node_by_type(nodes, node_type):
    return next(node for node in nodes if node["type"] == node_type)


def node_by_name(nodes, name):
    return next(node for node in nodes if node["name"] == name)


def code_node(node_id, name, position, code):
    return {
        "parameters": {"jsCode": code.strip()},
        "id": node_id,
        "name": name,
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": position,
    }


def if_string_node(
    node_id,
    name,
    position,
    left_value,
    right_value,
):
    return {
        "parameters": {
            "conditions": {
                "options": {
                    "caseSensitive": True,
                    "leftValue": "",
                    "typeValidation": "strict",
                    "version": 3,
                },
                "conditions": [
                    {
                        "id": f"{node_id}-condition",
                        "leftValue": left_value,
                        "rightValue": right_value,
                        "operator": {
                            "type": "string",
                            "operation": "equals",
                        },
                    }
                ],
                "combinator": "and",
            },
            "options": {},
        },
        "id": node_id,
        "name": name,
        "type": "n8n-nodes-base.if",
        "typeVersion": 2.3,
        "position": position,
    }


def telegram_text_node(
    node_id,
    name,
    position,
    chat_id,
    text,
    credentials,
):
    return {
        "parameters": {
            "chatId": chat_id,
            "text": text,
            "additionalFields": {
                "appendAttribution": False,
            },
        },
        "id": node_id,
        "name": name,
        "type": "n8n-nodes-base.telegram",
        "typeVersion": 1.2,
        "position": position,
        "credentials": copy.deepcopy(credentials),
    }


def connect(connections, source, target, output=0, input_index=0):
    source_connection = connections.setdefault(
        source,
        {"main": []},
    )
    outputs = source_connection["main"]
    while len(outputs) <= output:
        outputs.append([])
    outputs[output].append(
        {
            "node": target,
            "type": "main",
            "index": input_index,
        }
    )


PARSE_VIDEO_CODE = r"""
const source = $input.first();
const message = source.json.message ?? {};
const document = message.document ?? null;
const video = message.video ?? null;

const documentIsVideo = Boolean(
  document &&
  String(document.mime_type ?? '').toLowerCase().startsWith('video/')
);

const media = video ?? (documentIsVideo ? document : null);
const caption = String(message.caption ?? message.text ?? '').trim();
const idMatch = caption.match(/(?:reels?\s*)?#?(\d+)/i) ?? caption.match(/\d+/);
const rowId = idMatch?.[1] ?? idMatch?.[0] ?? '';
const fileSize = Number(media?.file_size ?? 0);
const maxSize = 20 * 1024 * 1024;

let errorMessage = '';

if (!media) {
  errorMessage = '⚠️ Отправьте исходное видео как видео или файл. В подписи укажите ID строки, например: 43';
} else if (!rowId) {
  errorMessage = '⚠️ Не найден ID сценария. Отправьте видео с подписью: 43 или reels 43';
} else if (fileSize > maxSize) {
  const sizeMb = (fileSize / 1024 / 1024).toFixed(1);
  errorMessage = `⚠️ Видео весит ${sizeMb} МБ. Telegram-бот может скачать не больше 20 МБ. Сожмите видео и отправьте снова.`;
}

return [{
  json: {
    ...source.json,
    valid_text: errorMessage ? 'no' : 'yes',
    error_message: errorMessage,
    chat_id: message.chat?.id ?? '',
    row_id: String(rowId),
    file_id: media?.file_id ?? '',
    file_size: fileSize,
    source_file_name: media?.file_name ?? `source_${rowId || 'video'}.mp4`
  }
}];
"""


PREPARE_VIDEO_CODE = r"""
function normalize(object = {}) {
  return Object.fromEntries(
    Object.entries(object).map(([key, value]) => [String(key).trim(), value])
  );
}

const row = normalize($input.first().json);
const meta = $('Разобрать видео').first().json;
const downloaded = $('Скачать видео').first();

const id = String(row.ID ?? '');
const status = String(row.Статус ?? '').trim();
const decision = String(row.Решение ?? '').trim();
const found = Boolean(id) && id === String(meta.row_id);
const ready = found && status === 'Готово' && decision === 'Принять';

let errorMessage = '';
if (!found) {
  errorMessage = `⚠️ Сценарий с ID ${meta.row_id} не найден в Google Sheets.`;
} else if (!ready) {
  errorMessage = `⚠️ Сценарий ID ${meta.row_id} пока нельзя монтировать. Статус: ${status || '—'}. Решение: ${decision || '—'}.`;
}

return [{
  json: {
    ...row,
    ...meta,
    ready_text: ready ? 'yes' : 'no',
    error_message: errorMessage,
    ID: id || meta.row_id
  },
  binary: downloaded.binary
}];
"""


REATTACH_FOR_TRANSCRIPTION_CODE = r"""
const prepared = $('Подготовить сценарий и видео').first();

return [{
  json: {
    ...prepared.json,
    analysis: $input.first().json
  },
  binary: prepared.binary
}];
"""


COLLECT_EDIT_DATA_CODE = r"""
const prepared = $('Для транскрипции').first().json;
const current = $input.first().json;

let transcript =
  current.text ??
  current.transcription ??
  current.output_text ??
  current.output ??
  '';

if (typeof transcript !== 'string') {
  transcript = JSON.stringify(transcript);
}

return [{
  json: {
    ...prepared,
    transcript: String(transcript).trim()
  }
}];
"""


PARSE_ACTIONS_CODE = r"""
const model = $input.first().json;
const source = $('Собрать данные монтажа').first().json;
const downloaded = $('Скачать видео').first();
const analysis = source.analysis ?? {};
const duration = Number(analysis.duration ?? 0);

let raw =
  model.output?.[0]?.content?.[0]?.text ??
  model.output_text ??
  model.text ??
  '';

raw = String(raw)
  .replace(/```json/gi, '')
  .replace(/```/g, '')
  .trim();

let parsed = {};
try {
  parsed = JSON.parse(raw);
} catch (error) {
  parsed = {};
}

const requested = Array.isArray(parsed)
  ? parsed
  : (Array.isArray(parsed.actions) ? parsed.actions : []);

function number(value, fallback = 0) {
  const result = Number(value);
  return Number.isFinite(result) ? result : fallback;
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

const actions = [];

for (const cut of (analysis.suggested_cuts ?? [])) {
  const start = clamp(number(cut.start), 0, duration);
  const end = clamp(number(cut.end), 0, duration);
  if (end > start) {
    actions.push({ action: 'CUT', start, end });
  }
}

for (const action of requested) {
  const type = String(action.action ?? '').toUpperCase().trim();
  if (!['ZOOM', 'TEXT', 'SUBTITLE'].includes(type)) continue;

  const start = clamp(number(action.start), 0, duration);
  const end = clamp(number(action.end), 0, duration);
  if (end <= start) continue;

  if (type === 'ZOOM') {
    actions.push({
      action: 'ZOOM',
      start,
      end: Math.min(end, start + 3),
      scale: clamp(number(action.scale, 1.1), 1.01, 1.25)
    });
    continue;
  }

  const text = String(action.text ?? '').trim().slice(0, 120);
  if (!text) continue;

  actions.push({
    action: 'SUBTITLE',
    start,
    end: Math.min(end, start + 6),
    text,
    highlight: String(action.highlight ?? '').trim().slice(0, 40)
  });
}

function speechOffsetToTime(offset, segments) {
  let remaining = Math.max(0, offset);
  for (const segment of segments) {
    const start = number(segment.start);
    const end = number(segment.end);
    const length = Math.max(0, end - start);
    if (remaining <= length) return start + remaining;
    remaining -= length;
  }
  return duration;
}

const hasSubtitles = actions.some(action => action.action === 'SUBTITLE');

if (!hasSubtitles && source.transcript) {
  const words = String(source.transcript).split(/\s+/).filter(Boolean);
  const chunks = [];
  for (let index = 0; index < words.length; index += 6) {
    chunks.push(words.slice(index, index + 6));
  }

  const segments = (analysis.speech_segments ?? []).length
    ? analysis.speech_segments
    : [{ start: 0, end: duration }];
  const speechDuration = segments.reduce(
    (sum, segment) => sum + Math.max(0, number(segment.end) - number(segment.start)),
    0
  );

  chunks.forEach((chunk, index) => {
    const startOffset = speechDuration * index / Math.max(1, chunks.length);
    const endOffset = speechDuration * (index + 1) / Math.max(1, chunks.length);
    const start = speechOffsetToTime(startOffset, segments);
    const end = speechOffsetToTime(endOffset, segments);
    if (end > start) {
      actions.push({
        action: 'SUBTITLE',
        start,
        end,
        text: chunk.join(' '),
        highlight: chunk.length > 1 ? chunk[1] : chunk[0]
      });
    }
  });
}

if (!actions.some(action => action.action === 'ZOOM') && duration > 1) {
  for (let start = 0.5; start < duration; start += 7) {
    actions.push({
      action: 'ZOOM',
      start,
      end: Math.min(duration, start + 1.1),
      scale: 1.1
    });
  }
}

return [{
  json: {
    ...source,
    actions,
    actions_json: JSON.stringify(actions),
    montage_notes: parsed.notes ?? ''
  },
  binary: downloaded.binary
}];
"""


EDIT_PLAN_PROMPT = """=Ты профессиональный монтажёр разговорных Reels врача-флеболога.

Создай только команды ZOOM и SUBTITLE для исходного видео. CUT не создавай: безопасные удаления тишины добавляются автоматически.

ID сценария: {{$json.ID}}
Длительность: {{$json.analysis.duration}} секунд
Речевые интервалы исходного видео:
{{ JSON.stringify($json.analysis.speech_segments) }}

Транскрипция фактической речи:
{{$json.transcript}}

Проверенный сценарий:
HOOK: {{$json.Hook}}
КОНФЛИКТ: {{$json.Конфликт}}
СЦЕНАРИЙ: {{$json.Сценарий}}
CTA: {{$json.CTA}}

Правила:
1. Тайминги указывай относительно ИСХОДНОГО видео.
2. SUBTITLE должен повторять фактическую речь из транскрипции, ничего медицинского не выдумывай.
3. Одна субтитровая фраза — примерно 3–7 слов, продолжительность 1–4 секунды.
4. highlight — одно важное слово, которое уже есть в text.
5. ZOOM ставь примерно раз в 5–8 секунд, длительность 0.8–1.5 секунды, scale от 1.07 до 1.15.
6. Не создавай команды за пределами длительности видео.
7. Не добавляй музыку, диагнозы, обещания лечения или новый текст.

Верни только валидный JSON без Markdown:
{
  "actions": [
    {"action":"ZOOM","start":0.5,"end":1.5,"scale":1.1},
    {"action":"SUBTITLE","start":0.2,"end":2.5,"text":"точная фраза из речи","highlight":"фраза"}
  ],
  "notes":"краткое примечание"
}
"""


def build_workflow(source_path, output_path, editor_url):
    workflow = json.loads(source_path.read_text(encoding="utf-8"))
    workflow["name"] = "Единый Reels AI — сценарий + монтаж"
    workflow["active"] = False
    workflow.pop("id", None)
    workflow.pop("versionId", None)

    nodes = workflow["nodes"]
    connections = workflow["connections"]

    telegram_credentials = copy.deepcopy(
        node_by_type(nodes, "n8n-nodes-base.telegram")["credentials"]["telegramApi"]
    )
    telegram_credentials = {"telegramApi": telegram_credentials}
    openai_credentials = copy.deepcopy(
        node_by_type(nodes, "@n8n/n8n-nodes-langchain.openAi")["credentials"]
    )
    sheets_source = node_by_name(nodes, "Get row(s) in sheet")
    sheets_credentials = copy.deepcopy(sheets_source["credentials"])

    existing_telegram = node_by_name(nodes, "Send a text message")
    existing_telegram.setdefault("parameters", {}).setdefault(
        "additionalFields",
        {},
    )["appendAttribution"] = False

    new_nodes = [
        if_string_node(
            "10000000-0000-4000-8000-000000000001",
            "IF Готов к монтажу",
            [2016, 320],
            "={{ $json.Статус === 'Готово' && $json.Решение === 'Принять' ? 'yes' : 'no' }}",
            "yes",
        ),
        telegram_text_node(
            "10000000-0000-4000-8000-000000000002",
            "Запросить исходное видео",
            [2240, 320],
            "388686197",
            "={{ '🎞 СЦЕНАРИЙ ГОТОВ К МОНТАЖУ\\n\\nID: ' + ($json.ID || $json.row_number) + '\\nТема: ' + ($json.Тема || '—') + '\\n\\nОтправьте исходное видео до 20 МБ в этот чат. В подписи укажите: ' + ($json.ID || $json.row_number) }}",
            telegram_credentials,
        ),
        {
            "parameters": {
                "updates": ["message"],
                "additionalFields": {
                    "chatIds": "388686197",
                },
            },
            "id": "10000000-0000-4000-8000-000000000003",
            "name": "Telegram — исходное видео",
            "type": "n8n-nodes-base.telegramTrigger",
            "typeVersion": 1.5,
            "position": [-432, 704],
            "webhookId": "10000000-0000-4000-8000-000000000103",
            "credentials": copy.deepcopy(telegram_credentials),
        },
        code_node(
            "10000000-0000-4000-8000-000000000004",
            "Разобрать видео",
            [-208, 704],
            PARSE_VIDEO_CODE,
        ),
        if_string_node(
            "10000000-0000-4000-8000-000000000005",
            "IF Входное видео корректно",
            [16, 704],
            "={{ $json.valid_text }}",
            "yes",
        ),
        telegram_text_node(
            "10000000-0000-4000-8000-000000000006",
            "Подсказка по загрузке",
            [240, 832],
            "={{ $json.chat_id || 388686197 }}",
            "={{ $json.error_message }}",
            telegram_credentials,
        ),
        {
            "parameters": {
                "resource": "file",
                "operation": "get",
                "fileId": "={{ $json.file_id }}",
                "binaryProperty": "data",
                "download": True,
                "additionalFields": {
                    "mimeType": "video/mp4",
                },
            },
            "id": "10000000-0000-4000-8000-000000000007",
            "name": "Скачать видео",
            "type": "n8n-nodes-base.telegram",
            "typeVersion": 1.2,
            "position": [240, 624],
            "credentials": copy.deepcopy(telegram_credentials),
        },
        {
            "parameters": {
                "documentId": copy.deepcopy(
                    sheets_source["parameters"]["documentId"]
                ),
                "sheetName": copy.deepcopy(
                    sheets_source["parameters"]["sheetName"]
                ),
                "filtersUI": {
                    "values": [
                        {
                            "lookupColumn": "    ID",
                            "lookupValue": "={{ $('Разобрать видео').first().json.row_id }}",
                        }
                    ]
                },
                "options": {"returnFirstMatch": True},
            },
            "id": "10000000-0000-4000-8000-000000000008",
            "name": "Найти готовый сценарий",
            "type": "n8n-nodes-base.googleSheets",
            "typeVersion": 4.7,
            "position": [464, 624],
            "alwaysOutputData": True,
            "credentials": sheets_credentials,
        },
        code_node(
            "10000000-0000-4000-8000-000000000009",
            "Подготовить сценарий и видео",
            [688, 624],
            PREPARE_VIDEO_CODE,
        ),
        if_string_node(
            "10000000-0000-4000-8000-000000000010",
            "IF Сценарий можно монтировать",
            [912, 624],
            "={{ $json.ready_text }}",
            "yes",
        ),
        telegram_text_node(
            "10000000-0000-4000-8000-000000000011",
            "Ошибка готовности сценария",
            [1136, 800],
            "={{ $json.chat_id || 388686197 }}",
            "={{ $json.error_message }}",
            telegram_credentials,
        ),
        {
            "parameters": {
                "method": "POST",
                "url": f"{editor_url.rstrip('/')}/analyze",
                "sendBody": True,
                "contentType": "multipart-form-data",
                "bodyParameters": {
                    "parameters": [
                        {
                            "parameterType": "formBinaryData",
                            "name": "video",
                            "inputDataFieldName": "data",
                        }
                    ]
                },
                "options": {"timeout": 300000},
            },
            "id": "10000000-0000-4000-8000-000000000012",
            "name": "Анализ видео",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.4,
            "position": [1136, 544],
            "retryOnFail": True,
            "maxTries": 2,
            "waitBetweenTries": 5000,
        },
        code_node(
            "10000000-0000-4000-8000-000000000013",
            "Для транскрипции",
            [1360, 544],
            REATTACH_FOR_TRANSCRIPTION_CODE,
        ),
        {
            "parameters": {
                "method": "POST",
                "url": f"{editor_url.rstrip('/')}/extract-audio",
                "sendBody": True,
                "contentType": "multipart-form-data",
                "bodyParameters": {
                    "parameters": [
                        {
                            "parameterType": "formBinaryData",
                            "name": "video",
                            "inputDataFieldName": "data",
                        }
                    ]
                },
                "options": {
                    "timeout": 300000,
                    "response": {
                        "response": {
                            "responseFormat": "file",
                            "outputPropertyName": "data",
                        }
                    },
                },
            },
            "id": "10000000-0000-4000-8000-000000000021",
            "name": "Извлечь аудио",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.4,
            "position": [1584, 544],
            "retryOnFail": True,
            "maxTries": 2,
            "waitBetweenTries": 3000,
        },
        {
            "parameters": {
                "resource": "audio",
                "operation": "transcribe",
                "inputDataFieldName": "data",
                "options": {"language": "ru"},
            },
            "id": "10000000-0000-4000-8000-000000000014",
            "name": "Транскрибировать речь",
            "type": "@n8n/n8n-nodes-langchain.openAi",
            "typeVersion": 2.3,
            "position": [1808, 544],
            "retryOnFail": True,
            "maxTries": 2,
            "waitBetweenTries": 3000,
            "credentials": openai_credentials,
        },
        code_node(
            "10000000-0000-4000-8000-000000000015",
            "Собрать данные монтажа",
            [2032, 544],
            COLLECT_EDIT_DATA_CODE,
        ),
        {
            "parameters": {
                "modelId": {
                    "__rl": True,
                    "value": "gpt-4o-mini",
                    "mode": "list",
                    "cachedResultName": "GPT-4O-MINI",
                },
                "responses": {
                    "values": [{"content": EDIT_PLAN_PROMPT}]
                },
                "builtInTools": {},
                "options": {},
            },
            "id": "10000000-0000-4000-8000-000000000016",
            "name": "Создать монтажные команды",
            "type": "@n8n/n8n-nodes-langchain.openAi",
            "typeVersion": 2.3,
            "position": [2256, 544],
            "retryOnFail": True,
            "maxTries": 2,
            "waitBetweenTries": 3000,
            "credentials": openai_credentials,
        },
        code_node(
            "10000000-0000-4000-8000-000000000017",
            "Парсер монтажных команд",
            [2480, 544],
            PARSE_ACTIONS_CODE,
        ),
        {
            "parameters": {
                "method": "POST",
                "url": f"{editor_url.rstrip('/')}/edit",
                "sendBody": True,
                "contentType": "multipart-form-data",
                "bodyParameters": {
                    "parameters": [
                        {
                            "parameterType": "formBinaryData",
                            "name": "video",
                            "inputDataFieldName": "data",
                        },
                        {
                            "parameterType": "formData",
                            "name": "actions",
                            "value": "={{ $json.actions_json }}",
                        },
                        {
                            "parameterType": "formData",
                            "name": "output_name",
                            "value": "={{ 'reel_' + $json.row_id + '.mp4' }}",
                        },
                    ]
                },
                "options": {
                    "timeout": 300000,
                    "response": {
                        "response": {
                            "responseFormat": "file",
                            "outputPropertyName": "data",
                        }
                    },
                },
            },
            "id": "10000000-0000-4000-8000-000000000018",
            "name": "Смонтировать Reels",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.4,
            "position": [2704, 544],
            "retryOnFail": True,
            "maxTries": 2,
            "waitBetweenTries": 5000,
        },
        {
            "parameters": {
                "operation": "sendVideo",
                "chatId": "={{ $('Разобрать видео').first().json.chat_id }}",
                "binaryData": True,
                "binaryPropertyName": "data",
                "additionalFields": {
                    "caption": "={{ '✅ ГОТОВЫЙ REELS\\n\\nID: ' + $('Разобрать видео').first().json.row_id + '\\nСценарий и монтаж объединены.' }}",
                    "appendAttribution": False,
                },
            },
            "id": "10000000-0000-4000-8000-000000000019",
            "name": "Отправить готовый Reels",
            "type": "n8n-nodes-base.telegram",
            "typeVersion": 1.2,
            "position": [2928, 544],
            "credentials": copy.deepcopy(telegram_credentials),
        },
        {
            "parameters": {
                "content": "## Единый Reels AI\\n\\n1. Сценарий создаётся и проверяется существующей веткой.\\n2. Когда статус `Готово`, бот просит исходное видео.\\n3. Отправьте видео до 20 МБ с подписью `43` или `reels 43`.\\n4. Ветка ниже найдёт сценарий, проанализирует и смонтирует видео.\\n\\nПеред активацией отключите другой Telegram Trigger, использующий этого же бота.",
                "height": 360,
                "width": 440,
                "color": 5,
            },
            "id": "10000000-0000-4000-8000-000000000020",
            "name": "Инструкция единого агента",
            "type": "n8n-nodes-base.stickyNote",
            "typeVersion": 1,
            "position": [-912, 544],
        },
    ]

    nodes.extend(new_nodes)

    connect(connections, "Code in JavaScript6", "IF Готов к монтажу")
    connect(connections, "IF Готов к монтажу", "Запросить исходное видео", output=0)
    connect(connections, "Telegram — исходное видео", "Разобрать видео")
    connect(connections, "Разобрать видео", "IF Входное видео корректно")
    connect(connections, "IF Входное видео корректно", "Скачать видео", output=0)
    connect(connections, "IF Входное видео корректно", "Подсказка по загрузке", output=1)
    connect(connections, "Скачать видео", "Найти готовый сценарий")
    connect(connections, "Найти готовый сценарий", "Подготовить сценарий и видео")
    connect(connections, "Подготовить сценарий и видео", "IF Сценарий можно монтировать")
    connect(connections, "IF Сценарий можно монтировать", "Анализ видео", output=0)
    connect(connections, "IF Сценарий можно монтировать", "Ошибка готовности сценария", output=1)
    connect(connections, "Анализ видео", "Для транскрипции")
    connect(connections, "Для транскрипции", "Извлечь аудио")
    connect(connections, "Извлечь аудио", "Транскрибировать речь")
    connect(connections, "Транскрибировать речь", "Собрать данные монтажа")
    connect(connections, "Собрать данные монтажа", "Создать монтажные команды")
    connect(connections, "Создать монтажные команды", "Парсер монтажных команд")
    connect(connections, "Парсер монтажных команд", "Смонтировать Reels")
    connect(connections, "Смонтировать Reels", "Отправить готовый Reels")

    output_path.write_text(
        json.dumps(
            workflow,
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--editor-url",
        default=EDITOR_URL_DEFAULT,
    )
    args = parser.parse_args()
    build_workflow(
        args.source,
        args.output,
        args.editor_url,
    )


if __name__ == "__main__":
    main()
