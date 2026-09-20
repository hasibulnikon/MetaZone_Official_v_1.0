"""Static configuration: app version, AI provider catalog, platform
keyword/title/description rules, supported file extensions. No logic
here — just data other modules read.
"""

APP_VERSION = "v0.9.8"

AI_PROVIDERS = {
    "OpenRouter": {
        # v0.9.7.5: full re-audit against OpenRouter's live free-model
        # catalog. Every model previously listed here (Qwen2.5-VL 72B/
        # 32B, Gemini 2.0 Flash exp, Llama 4 Maverick/Scout, Mistral
        # Small 3.1) has since been pulled from OpenRouter's free tier
        # -- each now returns exactly the bug report's symptom: HTTP
        # 404 "This model is unavailable for free. The paid version is
        # available now -- use this slug instead: ..." (MODEL_UNAVAILABLE,
        # not a key problem; see engine/ai_providers._classify_http_error).
        # Replaced with OpenRouter's current free, natively multimodal
        # models, each individually confirmed (via its own OpenRouter
        # listing, not assumed from the model name) to be free, image-
        # capable, and served over the same OpenAI-compatible chat-
        # completions endpoint this app's _oa_style_content()/
        # call_openrouter() already sends -- no request-shape changes
        # needed. Ordered fastest/most-reliable-uptime first: since
        # call_with_failover (v0.9.7.5) now retries the next model in
        # this list on the same key when one comes back
        # MODEL_UNAVAILABLE, the earlier entries are tried first.
        "models": [
            ("Inkling Small (Vision)", "thinkingmachines/inkling-small:free"),
            ("Ling 3.0 Flash VL",      "inclusionai/ling-3.0-flash-vl:free"),
            ("Inkling (Vision)",       "thinkingmachines/inkling:free"),
            ("Nemotron 3 Nano Omni",   "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"),
        ],
        "key_url": "https://openrouter.ai/keys",
        "key_hint": "Get free key → openrouter.ai",
        "validate": "openrouter",
        "vision": True,
    },
    "Gemini": {
        "models": [
            ("Gemini 3.6 Flash",         "gemini-3.6-flash"),
            ("Gemini 3.5 Flash",         "gemini-3.5-flash"),
            ("Gemini 3.5 Flash-Lite",    "gemini-3.5-flash-lite"),
            ("Gemini 3.1 Flash-Lite",    "gemini-3.1-flash-lite"),
            ("Gemini 3 Flash (Preview)", "gemini-3-flash-preview"),
            ("Gemini 2.5 Flash",     "gemini-2.5-flash"),
            ("Gemini 1.5 Flash",     "gemini-1.5-flash"),
            ("Gemini 1.5 Pro",       "gemini-1.5-pro"),
        ],
        "key_url": "https://aistudio.google.com/app/apikey",
        "key_hint": "Get free key → aistudio.google.com",
        "validate": "gemini",
        "vision": True,
    },
    "Mistral": {
        "models": [
            ("Pixtral 12B",  "pixtral-12b-2409"),
            ("Pixtral Large","pixtral-large-2411"),
        ],
        "key_url": "https://console.mistral.ai/api-keys/",
        "key_hint": "Get key → console.mistral.ai",
        "validate": "mistral",
        "vision": True,
    },
    "Groq": {
        "models": [
            # Groq deprecated both Llama 4 Scout (Jun 2026) and Maverick
            # (Feb 2026) in favor of text-only gpt-oss models. Qwen 3.6 27B
            # is currently Groq's vision-capable model — note it's a
            # preview model on Groq's side, so this may need updating again
            # if Groq's lineup changes (check console.groq.com/docs/vision).
            ("Qwen 3.6 27B (Vision)", "qwen/qwen3.6-27b"),
        ],
        "key_url": "https://console.groq.com/keys",
        "key_hint": "Get free key → console.groq.com",
        "validate": "groq",
        "vision": True,
    },
    "Cerebras": {
        # v0.9.7.2: new provider. Cerebras' public free tier is
        # text-only -- no multimodal/vision models are served on
        # api.cerebras.ai as of this patch (verified against Cerebras'
        # own model catalog: llama3.1-8b / llama-3.3-70b are the free,
        # text-only models). "vision": False below is load-bearing --
        # it's what keeps image-to-metadata generation from ever being
        # routed to Cerebras (see engine/ai_providers.get_active_keys'
        # require_vision filter). Cerebras is still useful as a text-
        # generation fallback (Prompt-to-Prompt's text mode, etc).
        "models": [
            ("Llama 3.3 70B", "llama-3.3-70b"),
            ("Llama 3.1 8B", "llama3.1-8b"),
        ],
        "key_url": "https://cloud.cerebras.ai/",
        "key_hint": "Get free key → cloud.cerebras.ai",
        "validate": "cerebras",
        "vision": False,
    },
    "OpenAI": {
        "models": [
            ("GPT-4o",      "gpt-4o"),
            ("GPT-4o Mini", "gpt-4o-mini"),
            ("GPT-4.1 Nano","gpt-4.1-nano"),
        ],
        "key_url": "https://platform.openai.com/api-keys",
        "key_hint": "Get key → platform.openai.com",
        "validate": "openai",
        "vision": True,
    },
    "Claude": {
        "models": [
            ("Claude Haiku 4.5",  "claude-haiku-4-5-20251001"),
            ("Claude Sonnet 5",   "claude-sonnet-5"),
        ],
        "key_url": "https://console.anthropic.com/settings/keys",
        "key_hint": "Get key → console.anthropic.com",
        "validate": "claude",
        "vision": True,
    },
    "Grok": {
        "models": [
            ("Grok 4",       "grok-4"),
            ("Grok 4 Fast",  "grok-4-fast"),
        ],
        "key_url": "https://console.x.ai",
        "key_hint": "Get key → console.x.ai (this is xAI's Grok — different from Groq above)",
        "validate": "grok",
        "vision": True,
    },
}

CONTENT_SUFFIXES = {
    "Auto Detect":          "",
    "Vector Image Preview": "a vector illustration",
    "Vector File":          "",
    "Transparent PNG":      "isolated on a transparent background",
    "White Background":     "on a solid white background",
    "Silhouette":           "presented as a silhouette",
    "Videos":               "",
}
# v0.9.7: renamed "Vector" -> "Vector Image Preview" (still just a style
# hint fed to the normal image engine's prompt, for an actual raster
# image that depicts vector-style artwork -- unchanged behavior, new
# label), dropped "Illustration" (not in the requested final list), and
# added "Vector File" / "Videos" -- these two carry no content-phrase
# text because they're not style hints at all. A real .ai/.eps/.svg or
# .mp4/.mov file is *always* routed to the real vector/video engine by
# extension (session.py's process_one), regardless of which of these
# seven labels is selected -- the plain image engine literally can't
# open those formats, so there's nothing this dropdown could sensibly
# override for them. See session.py's VECTOR_EXTS/VIDEO_EXTS dispatch.

# v0.8.6: Image to Prompt Generator's "Prompt Style" dropdown -- feeds
# straight into engine/prompt_generator.py's build_prompt_prompt(styles=...)
# as styles=[value]. "Auto" (empty string) falls through to that
# function's own "realistic photography" default. This is a new,
# separate list from CONTENT_SUFFIXES above (which describes what the
# *source image itself* is, for metadata generation) -- this one
# describes what style the *generated prompt* should ask for.
PROMPT_GEN_STYLES = {
    "Auto (Realistic Photography)": "",
    "Realistic Photography":        "realistic photography",
    "Cinematic":                    "cinematic",
    "Digital Art":                  "digital art",
    "Illustration":                 "illustration",
    "Anime":                        "anime style",
    "3D Render":                    "3d render",
    "Product Photography":          "product photography",
    "Minimalist":                   "minimalist",
}

IMAGE_EXTS  = {'.jpg','.jpeg','.png','.gif','.webp','.tiff','.tif'}
VECTOR_EXTS = {'.svg','.eps','.ai'}
VIDEO_EXTS  = {'.mp4','.mov'}
ALL_SUPPORTED_EXTS = IMAGE_EXTS | VECTOR_EXTS | VIDEO_EXTS

AI_PROVIDERS_ORDERED=["Gemini","Mistral","Groq","Cerebras","OpenAI","Claude","Grok","OpenRouter"]

# Hidden from the Configuration window's API Keys tabs and skipped during generation
# failover — NOT deleted from AI_PROVIDERS/CALLERS/AI_PROVIDERS_ORDERED, so
# re-enabling them later (or if their issues get sorted out) is just
# removing an entry here, nothing structural. Claude and Grok are hidden
# because neither has a free API tier and this app is free-providers-only.
#
# v0.9.7.2: Groq REMOVED from this set. It was previously hidden here
# with no corresponding explanation (only Claude's lack of a free tier
# was ever documented as a reason) -- that mismatch is the actual root
# cause of "a freshly created Groq key gets reported as invalid": Groq
# never had a tab in the Configuration window to enter a key into in
# the first place, and was silently excluded from get_active_keys()'s
# failover sequence even for anyone who found another way to save one.
# Groq's own validate_key()/call_groq() endpoint, auth header, and
# model ID were already correct (console.groq.com's official
# OpenAI-compatible /openai/v1 API) -- see engine/ai_providers.py.
HIDDEN_PROVIDERS={"Grok","Claude"}
VISIBLE_PROVIDERS=[p for p in AI_PROVIDERS_ORDERED if p not in HIDDEN_PROVIDERS]

# v0.8.4: key names now match what app.js's applyPlatformDefaults()
# actually reads (title_chars/desc_chars/kw_count) -- previously this
# dict used "kw"/"title"/"desc" while app.js looked up
# rule.title_chars/rule.desc_chars/rule.kw_count, so every platform
# switch silently applied nothing (a real, previously-undetected bug,
# not a style change). has_desc marks platforms with no real
# description field at all (Adobe Stock has Title + Keywords only --
# confirmed against Adobe's own contributor docs, Sep 2026), so the
# UI can disable Description instead of just capping its length at 0.
# Numeric limits are working figures from public contributor
# docs/guides as of Sep 2026, not pulled from an official rate-limit
# API -- treat as "best current understanding", not a guarantee, and
# recheck if a platform changes its submission rules.
PLATFORM_RULES = {
    "General":      {"kw_count":49,"title_chars":300,"desc_chars":500,"has_desc":True},
    "Adobe Stock":  {"kw_count":49,"title_chars":200,"desc_chars":0,  "has_desc":False},
    "Shutterstock": {"kw_count":50,"title_chars":200,"desc_chars":200,"has_desc":True},
    "Getty Images": {"kw_count":50,"title_chars":200,"desc_chars":500,"has_desc":True},
    "Freepik":      {"kw_count":30,"title_chars":150,"desc_chars":200,"has_desc":True},
    "Pond5":        {"kw_count":50,"title_chars":200,"desc_chars":500,"has_desc":True},
    "iStock":       {"kw_count":50,"title_chars":200,"desc_chars":200,"has_desc":True},
    "Vecteezy":     {"kw_count":50,"title_chars":200,"desc_chars":200,"has_desc":True},
}

# Theme picker presets (see ui/theme.py for how one color becomes the full
# background ladder / accent hover+dim pair).
THEME_BG_PRESETS = {
    "Pitch Black":   "#000000",
    "Natural Black": "#0a0a0a",
    "Grayish Black": "#1c1c1c",
}
THEME_ACCENT_PRESETS = {
    "Green":  "#00c853",
    "Red":    "#eb6562",
    "Purple": "#b53bd5",
    "Pink":   "#e8447f",
    "Violet": "#875cff",
    "Orange": "#fb8c00",
    "Blue":   "#5293ff",
    "Teal":   "#00bfa5",
}
