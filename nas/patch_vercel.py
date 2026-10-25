#!/usr/bin/env python3
"""Add a "Vercel AI Gateway" chat-completion + image source to SillyTavern 1.19.0.

Usage: patch_vercel.py <pristine_root> <out_root>
Reads files from pristine_root, writes patched copies (same relative paths) to out_root.
Every anchor must match exactly once, otherwise the script aborts.
"""
import hashlib
import pathlib
import re
import sys

SRC = pathlib.Path(sys.argv[1])
OUT = pathlib.Path(sys.argv[2])
files = {}


def load(rel):
    if rel not in files:
        files[rel] = (SRC / rel).read_text()
    return files[rel]


def sub(rel, old, new, count=1):
    text = load(rel)
    n = text.count(old)
    if n != count:
        sys.exit(f'{rel}: anchor found {n}x (expected {count}):\n{old[:200]}')
    files[rel] = text.replace(old, new)


def after(rel, anchor, insert, count=1):
    sub(rel, anchor, anchor + insert, count)


def before(rel, anchor, insert, count=1):
    sub(rel, anchor, insert + anchor, count)


# ---------------- backend ----------------
after('src/constants.js', "    WORKERS_AI: 'workers_ai',\n", "    VERCEL: 'vercel',\n")
after('src/endpoints/secrets.js', "    COMETAPI: 'api_key_cometapi',\n", "    VERCEL: 'api_key_vercel',\n")

cc = 'src/endpoints/backends/chat-completions.js'
after(cc, "const API_GROQ = 'https://api.groq.com/openai/v1';\n", "const API_VERCEL = 'https://ai-gateway.vercel.sh/v1';\n")
# /status and /generate both have a COMETAPI branch; put a VERCEL branch in front of each.
before(cc, "        } else if (request.body.chat_completion_source === CHAT_COMPLETION_SOURCES.COMETAPI) {\n            apiUrl = API_COMETAPI;\n            apiKey = readSecret(request.user.directories, SECRET_KEYS.COMETAPI, request.body.secret_id);\n            headers = {};\n            throw",
       "        } else if (request.body.chat_completion_source === CHAT_COMPLETION_SOURCES.VERCEL) {\n            apiUrl = API_VERCEL;\n            apiKey = readSecret(request.user.directories, SECRET_KEYS.VERCEL, request.body.secret_id);\n            headers = {};\n")
before(cc, "        } else if (request.body.chat_completion_source === CHAT_COMPLETION_SOURCES.COMETAPI) {\n            apiUrl = API_COMETAPI;\n            apiKey = readSecret(request.user.directories, SECRET_KEYS.COMETAPI, request.body.secret_id);\n            headers = {};\n            bodyParams = {",
       "        } else if (request.body.chat_completion_source === CHAT_COMPLETION_SOURCES.VERCEL) {\n            apiUrl = API_VERCEL;\n            apiKey = readSecret(request.user.directories, SECRET_KEYS.VERCEL, request.body.secret_id);\n            headers = {};\n            bodyParams = {};\n")
# Gateway lists image/video/embedding models too; keep chat models only.
before(cc, "            if (request.body.chat_completion_source === CHAT_COMPLETION_SOURCES.CHUTES && Array.isArray(data?.data)) {\n",
       "            if (request.body.chat_completion_source === CHAT_COMPLETION_SOURCES.VERCEL && Array.isArray(data?.data)) {\n                data.data = data.data.filter(model => model?.type === 'language');\n            }\n\n")

sd = 'src/endpoints/stable-diffusion.js'
before(sd, "const xai = express.Router();\n", r"""const vercel = express.Router();

// The page asks for this list on every load and the gateway takes ~1.4 s to answer, so keep
// it for 10 minutes. The list is public (no key), so one copy serves all users. If a refresh
// fails, the last good list is served.
const VERCEL_MODELS_TTL = 10 * 60 * 1000;
/** @type {{time: number, models: {value: string, text: string}[]} | null} */
let vercelModelsCache = null;

vercel.post('/models', async (request, response) => {
    if (vercelModelsCache && Date.now() - vercelModelsCache.time < VERCEL_MODELS_TTL) {
        return response.send(vercelModelsCache.models);
    }

    try {
        const result = await fetch('https://ai-gateway.vercel.sh/v1/models');

        if (!result.ok) {
            console.warn('Vercel AI Gateway returned an error.', result.statusText);
            return vercelModelsCache ? response.send(vercelModelsCache.models) : response.sendStatus(500);
        }

        /** @type {any} */
        const data = await result.json();
        const models = Array.isArray(data?.data)
            ? data.data.filter(m => m?.type === 'image').map(m => ({ value: m.id, text: m.name ? `${m.name} (${m.id})` : m.id }))
            : [];

        if (models.length) {
            vercelModelsCache = { time: Date.now(), models };
        }
        return response.send(models);
    } catch (error) {
        console.error('Error fetching Vercel AI Gateway models', error);
        return vercelModelsCache ? response.send(vercelModelsCache.models) : response.sendStatus(500);
    }
});

// Image generation runs as a background job: /generate starts it and returns an id,
// the browser polls /result. A single request held open for the whole generation gets
// cut off by the Cloudflare tunnel path at 60 s.
/** @type {Map<string, {owner: string, created: number, status: 'pending'|'done'|'error', image?: string, format?: string, error?: string}>} */
const vercelJobs = new Map();
const VERCEL_JOB_TTL = 15 * 60 * 1000;

async function runVercelJob(job, key, requestBody) {
    try {
        const result = await fetch('https://ai-gateway.vercel.sh/v1/images/generations', {
            method: 'POST',
            body: JSON.stringify(requestBody),
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${key}`,
            },
        });

        if (!result.ok) {
            const text = await result.text();
            console.warn('Vercel AI Gateway returned an error.', text);
            job.error = text || result.statusText;
            job.status = 'error';
            return;
        }

        /** @type {any} */
        const data = await result.json();
        const encodedImage = String(data?.data?.[0]?.b64_json || '');
        if (!encodedImage) {
            console.warn('Vercel AI Gateway returned invalid data.');
            job.error = 'Vercel AI Gateway returned invalid data.';
            job.status = 'error';
            return;
        }

        const dataUrlMatch = encodedImage.match(/^data:(.+);base64,(.+)$/);
        const mimeType = dataUrlMatch?.[1] || 'image/png';
        job.format = mime.extension(mimeType) || 'png';
        job.image = dataUrlMatch?.[2] || encodedImage;
        job.status = 'done';
        console.debug('Vercel AI Gateway job done:', requestBody.model, `${Math.round((Date.now() - job.created) / 1000)}s`);
    } catch (error) {
        console.error('Error communicating with Vercel AI Gateway', error);
        job.error = String(error?.message || error);
        job.status = 'error';
    }
}

vercel.post('/generate', async (request, response) => {
    try {
        const key = readSecret(request.user.directories, SECRET_KEYS.VERCEL);

        if (!key) {
            console.warn('Vercel AI Gateway key not found.');
            return response.sendStatus(400);
        }

        const requestBody = {
            prompt: request.body.prompt,
            model: request.body.model,
            size: request.body.size,
            n: 1,
            response_format: 'b64_json',
        };

        for (const [id, job] of vercelJobs) {
            if (Date.now() - job.created > VERCEL_JOB_TTL) {
                vercelJobs.delete(id);
            }
        }

        const id = randomUUID();
        const job = { owner: request.user.profile.handle, created: Date.now(), status: /** @type {const} */ ('pending') };
        vercelJobs.set(id, job);

        console.debug('Vercel AI Gateway request:', id, requestBody);
        runVercelJob(job, key, requestBody);

        return response.send({ id });
    } catch (error) {
        console.error('Error communicating with Vercel AI Gateway', error);
        return response.sendStatus(500);
    }
});

vercel.post('/result', async (request, response) => {
    const job = vercelJobs.get(String(request.body.id));

    if (!job || job.owner !== request.user.profile.handle) {
        return response.status(404).send('Vercel AI Gateway job not found (expired, or SillyTavern restarted).');
    }

    if (job.status === 'error') {
        return response.status(500).send(job.error);
    }

    if (job.status === 'pending') {
        return response.send({ status: 'pending' });
    }

    // Kept until the TTL, so a result download that gets cut off can be fetched again.
    return response.send({ status: 'done', image: job.image, format: job.format });
});

""")
after(sd, "import path from 'node:path';\n", "import { randomUUID } from 'node:crypto';\n")
after(sd, "router.use('/xai', xai);\n", "router.use('/vercel', vercel);\n")

# ---------------- frontend: API connections ----------------
sec = 'public/scripts/secrets.js'
after(sec, "    COMETAPI: 'api_key_cometapi',\n", "    VERCEL: 'api_key_vercel',\n")
after(sec, "    [SECRET_KEYS.COMETAPI]: 'CometAPI',\n", "    [SECRET_KEYS.VERCEL]: 'Vercel AI Gateway',\n")
after(sec, "    [SECRET_KEYS.COMETAPI]: '#api_key_cometapi',\n", "    [SECRET_KEYS.VERCEL]: '#api_key_vercel',\n")

oai = 'public/scripts/openai.js'
after(oai, "    COMETAPI: 'cometapi',\n", "    VERCEL: 'vercel',\n")
after(oai, "    cometapi_model: ['#model_cometapi_select', 'cometapi_model', false, true],\n",
      "    vercel_model: ['#model_vercel_select', 'vercel_model', false, true],\n")
after(oai, "    cometapi_model: 'gpt-4o',\n", "    vercel_model: 'openai/gpt-5-mini',\n")
after(oai, "        case chat_completion_sources.COMETAPI:\n            return settings.cometapi_model;\n",
      "        case chat_completion_sources.VERCEL:\n            return settings.vercel_model;\n")
before(oai, "    if (oai_settings.chat_completion_source === chat_completion_sources.SILICONFLOW) {\n        $('#model_siliconflow_select').empty();\n",
       """    if (oai_settings.chat_completion_source === chat_completion_sources.VERCEL) {
        $('#model_vercel_select').empty();
        model_list.forEach((model) => {
            $('#model_vercel_select').append(
                $('<option>', {
                    value: model.id,
                    text: model.id,
                }));
        });

        const selectedModel = model_list.find(model => model.id === oai_settings.vercel_model);
        if (model_list.length > 0 && (!selectedModel || !oai_settings.vercel_model)) {
            oai_settings.vercel_model = model_list[0].id;
        }

        $('#model_vercel_select').val(oai_settings.vercel_model).trigger('change');
    }

""")
# seed support
sub(oai, "        chat_completion_sources.GROQ,\n        chat_completion_sources.ELECTRONHUB,\n        chat_completion_sources.NANOGPT,\n",
    "        chat_completion_sources.GROQ,\n        chat_completion_sources.VERCEL,\n        chat_completion_sources.ELECTRONHUB,\n        chat_completion_sources.NANOGPT,\n")
# streaming parser: OpenAI-compatible with reasoning deltas
sub(oai, "chat_completion_sources.WORKERS_AI, chat_completion_sources.FIREWORKS].includes(chat_completion_source)) {",
    "chat_completion_sources.WORKERS_AI, chat_completion_sources.FIREWORKS, chat_completion_sources.VERCEL].includes(chat_completion_source)) {")
before(oai, "    if ($(this).is('#model_siliconflow_select')) {\n",
       """    if ($(this).is('#model_vercel_select')) {
        if (!value || !hasModelsLoaded) {
            console.debug('Null Vercel AI Gateway model selected. Ignoring.');
            return;
        }
        console.log('Vercel AI Gateway model changed to', value);
        oai_settings.vercel_model = value;
    }

""")
before(oai, "    if (oai_settings.chat_completion_source == chat_completion_sources.AI21) {\n",
       """    if (oai_settings.chat_completion_source == chat_completion_sources.VERCEL) {
        $('#openai_max_context').attr('max', unlocked_max);
        oai_settings.openai_max_context = Math.min(Number($('#openai_max_context').attr('max')), oai_settings.openai_max_context);
        $('#openai_max_context').val(oai_settings.openai_max_context).trigger('input');
        oai_settings.temp_openai = Math.min(oai_max_temp, oai_settings.temp_openai);
        $('#temp_openai').attr('max', oai_max_temp).val(oai_settings.temp_openai).trigger('input');
    }

""")
after(oai, "        [chat_completion_sources.COMETAPI]: { key: SECRET_KEYS.COMETAPI, selector: '#api_key_cometapi', proxy: false },\n",
      "        [chat_completion_sources.VERCEL]: { key: SECRET_KEYS.VERCEL, selector: '#api_key_vercel', proxy: false },\n")
after(oai, "    } else if (oai_settings.chat_completion_source == chat_completion_sources.GROQ) {\n        $('#model_groq_select').trigger('change');\n",
      "    } else if (oai_settings.chat_completion_source == chat_completion_sources.VERCEL) {\n        $('#model_vercel_select').trigger('change');\n")
after(oai, "    $('#model_groq_select').on('change', onModelChange);\n", "    $('#model_vercel_select').on('change', onModelChange);\n")

after('public/scripts/reasoning.js', "                case chat_completion_sources.FIREWORKS:\n", "                case chat_completion_sources.VERCEL:\n")
after('public/scripts/tool-calling.js', "            chat_completion_sources.COMETAPI,\n", "            chat_completion_sources.VERCEL,\n")
after('public/scripts/RossAscends-mods.js',
      "                    || (secret_state[SECRET_KEYS.COMETAPI] && oai_settings.chat_completion_source == chat_completion_sources.COMETAPI)\n",
      "                    || (secret_state[SECRET_KEYS.VERCEL] && oai_settings.chat_completion_source == chat_completion_sources.VERCEL)\n")
after('public/scripts/slash-commands.js',
      "        { id: 'model_cometapi_select', api: 'openai', type: chat_completion_sources.COMETAPI },\n",
      "        { id: 'model_vercel_select', api: 'openai', type: chat_completion_sources.VERCEL },\n")

html = 'public/index.html'
after(html, '                                <option value="groq">Groq</option>\n',
      '                                <option value="vercel">Vercel AI Gateway</option>\n')
before(html, '                        <div id="groq_form" data-source="groq">\n', """                        <div id="vercel_form" data-source="vercel">
                            <h4><a href="https://vercel.com/docs/ai-gateway" target="_blank" rel="noopener noreferrer">Vercel AI Gateway API Key</a></h4>
                            <div class="flex-container">
                                <input id="api_key_vercel" name="api_key_vercel" class="text_pole flex1" value="" type="text" autocomplete="off">
                                <div title="Manage API keys" data-i18n="[title]Manage API keys" class="menu_button fa-solid fa-key fa-fw manage-api-keys" data-key="api_key_vercel"></div>
                            </div>
                            <div data-for="api_key_vercel" class="neutral_warning" data-i18n="For privacy reasons, your API key will be hidden after you click 'Connect'.">
                                For privacy reasons, your API key will be hidden after you click 'Connect'.
                            </div>
                            <h4>Vercel AI Gateway Model</h4>
                            <select id="model_vercel_select">
                                <option value="openai/gpt-5-mini">openai/gpt-5-mini</option>
                            </select>
                        </div>
""")
# Same sampler/feature visibility as Groq (plain OpenAI-compatible).
text = load(html)
pat = re.compile(r'data-source="([^"]*)"')
def add_vercel(m):
    items = m.group(1).split(',')
    # Only shared multi-source lists; single-source ones are the per-provider forms.
    if len(items) > 1 and 'groq' in items and 'vercel' not in items:
        items.append('vercel')
    return f'data-source="{",".join(items)}"'
new_text, n = pat.subn(add_vercel, text)
files[html] = new_text

# ---------------- frontend: image generation ----------------
sdi = 'public/scripts/extensions/stable-diffusion/index.js'
after(sdi, "    xai: 'xai',\n", "    vercel: 'vercel',\n")
# samplers / schedulers / VAEs: 'N/A' like xAI
for kind in ('samplers', 'schedulers', 'vaes'):
    sub(sdi, f"        case sources.xai:\n            {kind} = ['N/A'];\n",
        f"        case sources.xai:\n        case sources.vercel:\n            {kind} = ['N/A'];\n")
after(sdi, "        case sources.xai:\n            models = await loadXAIModels();\n            break;\n",
      "        case sources.vercel:\n            models = await loadVercelModels();\n            break;\n")
before(sdi, "async function loadXAIModels() {\n", """async function loadVercelModels() {
    $('#sd_vercel_key').toggleClass('success', !!secret_state[SECRET_KEYS.VERCEL]);

    const result = await fetch('/api/sd/vercel/models', {
        method: 'POST',
        headers: getRequestHeaders(),
    });

    if (result.ok) {
        return await result.json();
    }

    return [];
}

""")
after(sdi, "            case sources.xai:\n                result = await generateXAIImage(prefixedPrompt, negativePrompt, signal);\n                break;\n",
      "            case sources.vercel:\n                result = await generateVercelImage(prefixedPrompt, signal);\n                break;\n")
before(sdi, "async function generateXAIImage(prompt, _negativePrompt, signal) {\n", """/**
 * Generates an image using the Vercel AI Gateway (OpenAI-compatible images endpoint).
 * @param {string} prompt - The main instruction used to guide the image generation.
 * @param {AbortSignal} signal - An AbortSignal object that can be used to cancel the request.
 * @returns {Promise<{format: string, data: string}>} - A promise that resolves when the image generation and processing are complete.
 */
async function generateVercelImage(prompt, signal) {
    let width = extension_settings.sd.width;
    let height = extension_settings.sd.height;

    // OpenAI image models only accept a few fixed sizes.
    if (/^openai\\//.test(extension_settings.sd.model)) {
        const aspectRatio = width / height;
        width = aspectRatio > 1 ? 1536 : 1024;
        height = aspectRatio < 1 ? 1536 : 1024;
    }

    const start = await fetch('/api/sd/vercel/generate', {
        method: 'POST',
        headers: getRequestHeaders(),
        signal: signal,
        body: JSON.stringify({
            prompt: prompt,
            model: extension_settings.sd.model,
            size: `${width}x${height}`,
        }),
    });

    if (!start.ok) {
        const text = await start.text();
        throw new Error(text);
    }

    // The server runs the generation as a job; poll with short requests, because
    // one request held open for the whole generation gets cut off by the tunnel at 60 s.
    const { id } = await start.json();
    const startedAt = Date.now();
    let failures = 0;

    while (Date.now() - startedAt < 10 * 60 * 1000) {
        await delay(3000);
        signal?.throwIfAborted();

        let result;
        try {
            result = await fetch('/api/sd/vercel/result', {
                method: 'POST',
                headers: getRequestHeaders(),
                signal: signal,
                body: JSON.stringify({ id }),
            });
        } catch (error) {
            if (signal?.aborted || ++failures > 5) {
                throw error;
            }
            continue;
        }

        if (!result.ok) {
            const text = await result.text();
            throw new Error(text || result.statusText);
        }

        const data = await result.json();
        if (data.status === 'done') {
            return { format: data.format, data: data.image };
        }
        failures = 0;
    }

    throw new Error('Vercel AI Gateway image generation timed out after 10 minutes.');
}

""")
after(sdi, "        case sources.xai:\n            return secret_state[SECRET_KEYS.XAI];\n",
      "        case sources.vercel:\n            return secret_state[SECRET_KEYS.VERCEL];\n")

after('public/scripts/extensions/stable-diffusion/settings.html',
      '                <option value="togetherai">TogetherAI</option>\n',
      '                <option value="vercel">Vercel AI Gateway</option>\n')

# ---------------- frontend: stop-reason toast ----------------
# Gemini cuts replies on MAX_TOKENS (thinking counts toward the limit) or with its own content
# filter (PROHIBITED_CONTENT even with every safety setting OFF), and stock ST shows nothing for
# either: a continue just adds a few words or nothing. Show a toast with the reason instead.
# Non-streaming Gemini replies drop finishReason on the server, so pass it through as finish_reason.
sub(cc, "            const reply = { choices: [{ 'message': { 'content': responseText } }], responseContent };\n",
    "            const reply = { choices: [{ 'message': { 'content': responseText }, 'finish_reason': candidates[0].finishReason }], responseContent };\n")
before(oai, "/**\n * Send a chat completion request to backend\n", r"""// LOCAL PATCH: tell the user why a reply stopped early (see patches/patch_vercel.py).
const STOP_REASONS_NORMAL = ['STOP', 'FINISH_REASON_UNSPECIFIED', 'stop', 'tool_calls', 'function_call', 'end_turn', 'eos'];
const STOP_REASONS_LENGTH = ['MAX_TOKENS', 'length'];
const STOP_REASONS_FILTER = ['SAFETY', 'PROHIBITED_CONTENT', 'BLOCKLIST', 'SPII', 'IMAGE_SAFETY', 'content_filter'];

/**
 * Remembers the finish reason and prompt block reason from a Gemini or OpenAI-style response chunk.
 * @param {object} data Parsed response or stream chunk
 * @param {object} state Object to store `stopReason` and `blockReason` in
 */
function trackStopReason(data, state) {
    const reason = data?.candidates?.[0]?.finishReason ?? data?.choices?.[0]?.finish_reason;
    if (reason) state.stopReason = reason;
    const blocked = data?.promptFeedback?.blockReason;
    if (blocked) state.blockReason = blocked;
}

/**
 * Shows a toast when a reply was cut short by the length limit or a content filter.
 * @param {{stopReason?: string, blockReason?: string}} state Reasons collected by trackStopReason
 */
function notifyStopReason({ stopReason, blockReason }) {
    const options = { timeOut: 15000, extendedTimeOut: 5000 };
    if (blockReason) {
        toastr.warning(`提示词被服务商拦截（${blockReason}），没有生成内容。可以修改上一条消息后重试，或换用其他模型。`, '回复被中断', options);
        return;
    }
    if (!stopReason || STOP_REASONS_NORMAL.includes(stopReason)) return;
    if (STOP_REASONS_LENGTH.includes(stopReason)) {
        toastr.warning(`回复达到“最大回复长度”（${oai_settings.openai_max_tokens} token，思考过程也计入），后面的内容被截断。可以点“继续”，或调大“最大回复长度”。`, '回复被截断', options);
    } else if (STOP_REASONS_FILTER.includes(stopReason)) {
        toastr.warning(`回复被服务商的内容过滤中断（${stopReason}）。这类过滤无法通过安全设置关闭，可以重试、修改上一条消息，或换用其他模型。`, '回复被中断', options);
    } else if (stopReason === 'RECITATION') {
        toastr.warning('回复因疑似复述受版权保护的内容被中断（RECITATION）。可以重试或换个说法。', '回复被中断', options);
    } else if (/^[A-Z_]+$/.test(stopReason)) {
        // Other Gemini reasons (OTHER, MALFORMED_FUNCTION_CALL, ...); OpenAI-style values vary too much to report.
        toastr.warning(`回复异常结束（${stopReason}）。`, '回复被中断', options);
    }
}

""")
sub(oai, """                if (done) return;
                const rawData = value.data;
                if (rawData === '[DONE]') return;
                tryParseStreamingError(response, rawData);
                const parsed = JSON.parse(rawData);
""", """                if (done) {
                    type !== 'quiet' && notifyStopReason(state);
                    return;
                }
                const rawData = value.data;
                if (rawData === '[DONE]') {
                    type !== 'quiet' && notifyStopReason(state);
                    return;
                }
                tryParseStreamingError(response, rawData);
                const parsed = JSON.parse(rawData);
                trackStopReason(parsed, state);
""")
before(oai, "        if (type !== 'quiet') {\n            const logprobs = parseChatCompletionLogprobs(data);\n", """        if (type !== 'quiet') {
            const stopState = {};
            trackStopReason(data, stopState);
            notifyStopReason(stopState);
        }

""")

# ---------------- frontend: service worker static cache ----------------
# Through the CF tunnel every request costs ~1 s and ST sends max-age=0, so a page load
# (~250 static files) took ~95 s. The worker answers static files from the browser cache.
# VERSION is a hash of everything generated here, so regenerating for a new ST release (or
# changing any patch) yields a new worker that drops the old caches.
SW_TEMPLATE = r"""// LOCAL PATCH: static-file cache, generated by patches/patch_vercel.py. Do not edit here.
// Core ST files: cache first (they only change with VERSION). Third-party extensions and user
// media: answered from cache, refreshed in the background. API, pages, user.css: network only.
const VERSION = '__VERSION__';
const CORE = `st-core-${VERSION}`;
const SWR = `st-swr-${VERSION}`;
const CORE_FILES = ['/lib.js', '/script.js', '/style.css', '/manifest.json', '/favicon.ico', '/st.ico'];
const CORE_PREFIXES = ['/lib/', '/scripts/', '/css/', '/webfonts/', '/img/', '/sounds/', '/locales/'];
const SWR_PREFIXES = ['/scripts/extensions/third-party/', '/backgrounds/', '/characters/', '/User%20Avatars/', '/thumbnail', '/user/images/'];
const NETWORK_ONLY = ['/css/user.css'];

function classify(url) {
    const p = url.pathname;
    if (NETWORK_ONLY.includes(p)) return null;
    if (SWR_PREFIXES.some(x => p.startsWith(x))) return 'swr';
    if (CORE_FILES.includes(p) || CORE_PREFIXES.some(x => p.startsWith(x))) return 'core';
    return null;
}

function cacheable(res) {
    return res.status === 200 && res.type === 'basic';
}

async function cacheFirst(request) {
    const cache = await caches.open(CORE);
    const hit = await cache.match(request);
    if (hit) return hit;
    const res = await fetch(request);
    if (cacheable(res)) await cache.put(request, res.clone());
    return res;
}

async function staleWhileRevalidate(event) {
    const cache = await caches.open(SWR);
    const hit = await cache.match(event.request);
    const update = fetch(event.request).then(async res => {
        if (cacheable(res)) await cache.put(event.request, res.clone());
        return res;
    });
    if (hit) {
        event.waitUntil(update.catch(() => {}));
        return hit;
    }
    return update;
}

async function dropCaches(keep = []) {
    const keys = await caches.keys();
    await Promise.all(keys.filter(k => k.startsWith('st-') && !keep.includes(k)).map(k => caches.delete(k)));
}

// index.html carries <meta name="st-sw" content="VERSION">. No marker: the patch was removed,
// so remove the worker too. Other version: fetch the new worker.
async function checkPage(res) {
    if (!res.ok || res.redirected || !(res.headers.get('content-type') || '').includes('text/html')) return;
    const m = (await res.text()).match(/<meta name="st-sw" content="([^"]*)">/);
    if (!m) {
        await dropCaches();
        await self.registration.unregister();
    } else if (m[1] !== VERSION) {
        await self.registration.update();
    }
}

async function navigate(event) {
    const res = await fetch(event.request);
    event.waitUntil(checkPage(res.clone()).catch(e => console.warn('[SW] page check failed', e)));
    return res;
}

self.addEventListener('install', () => self.skipWaiting());

self.addEventListener('activate', event => {
    event.waitUntil(dropCaches([CORE, SWR]).then(() => self.clients.claim()));
});

self.addEventListener('fetch', event => {
    const request = event.request;
    if (request.method !== 'GET' || request.headers.has('range')) return;
    const url = new URL(request.url);
    if (url.origin !== self.location.origin) return;
    if (request.mode === 'navigate') {
        if (url.pathname === '/') event.respondWith(navigate(event));
        return;
    }
    const kind = classify(url);
    if (kind === 'core') event.respondWith(cacheFirst(request));
    else if (kind === 'swr') event.respondWith(staleWhileRevalidate(event));
});
"""
sw_version = hashlib.sha256(
    ''.join(f'{rel}\0{files[rel]}\0' for rel in sorted(files)).encode() + SW_TEMPLATE.encode()
).hexdigest()[:12]
files['public/sw.js'] = SW_TEMPLATE.replace('__VERSION__', sw_version)
after(html, '    <meta name="theme-color" content="#333">\n', f"""    <meta name="st-sw" content="{sw_version}">
    <script>
        // LOCAL PATCH: register the static-file cache (public/sw.js). Needs HTTPS or localhost.
        if ('serviceWorker' in navigator) {{
            let hadController = !!navigator.serviceWorker.controller;
            navigator.serviceWorker.addEventListener('controllerchange', () => {{
                if (hadController && window.toastr) toastr.info('SillyTavern 已更新，刷新页面后生效');
                hadController = true;
            }});
            window.addEventListener('load', () => {{
                navigator.serviceWorker.register('sw.js', {{ scope: '/' }}).catch(e => console.warn('[SW] register failed', e));
            }});
        }}
    </script>
""")

for rel, text in files.items():
    dest = OUT / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text)
    print('patched', rel)
print(f'service worker version: {sw_version}')
print(f'index.html data-source lists extended: {sum(1 for m in pat.finditer(files[html]) if "vercel" in m.group(1).split(","))}')
