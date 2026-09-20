"""AI provider calls, API-key validation, and the failover engine that
rotates across providers/keys until one succeeds.
"""
import json, urllib.request, urllib.error
from core.constants import AI_PROVIDERS, HIDDEN_PROVIDERS
from core.utils import img_to_b64, model_label


class ProviderError(RuntimeError):
    """Raised by _post/the call_* functions with a stable
    `classification` string (see the list in the project spec: Problem
    B / "Error classification") so call_with_failover and the frontend
    can tell, e.g., an actually-invalid key apart from a rate limit or
    an unavailable free model, instead of collapsing every failure
    into one generic "failed" message."""
    def __init__(self, classification, message):
        self.classification = classification
        super().__init__(message)


# v0.9.7.5: a plain urllib.request call sends "Python-urllib/3.x" as
# its User-Agent by default, and no Accept header at all. Cerebras and
# Groq both sit behind Cloudflare, and Cloudflare's Browser Integrity
# Check treats that missing/non-browser signature as bot traffic --
# returning its own HTTP 403 block page ("Error 1010: The owner of
# this website has banned your access based on your browser's
# signature") *before the request ever reaches the provider's own
# auth check*. That's the root cause of "adding a valid Cerebras/Groq
# key reports error 1010": it was never a key problem, it was Python's
# default request fingerprint getting stopped at Cloudflare's edge. A
# normal browser-style User-Agent is the smallest fix that lets these
# requests actually reach the provider.
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _std_headers(extra=None):
    """Shared base headers (User-Agent + Accept) merged under whatever
    provider-specific headers a caller supplies -- used by every
    outbound request in this module (both _post's provider calls and
    validate_key's lightweight checks) so the Cloudflare-signature fix
    above applies everywhere at once instead of needing to be repeated
    per call site."""
    h = {"User-Agent": _UA, "Accept": "application/json"}
    if extra:
        h.update(extra)
    return h


def _is_cloudflare_1010(text):
    """Cloudflare's Browser Integrity Check block page is HTML, not a
    JSON body from the provider's own API, and its presence is NOT
    proof an API key is invalid -- it means the request never reached
    the provider at all. Detected by the literal 'error code: 1010'
    marker that page always includes, combined with a Cloudflare
    mention so this can't accidentally match some unrelated '1010'
    string a provider's own JSON error body might one day contain."""
    if not text:
        return False
    low = text.lower()
    return "1010" in low and "cloudflare" in low


def _classify_http_error(code, msg, raw_body=""):
    low = (msg or "").lower()
    if code in (401,):
        return "INVALID_API_KEY"
    if code == 403:
        # v0.9.7.5: 403 used to collapse unconditionally into
        # AUTHENTICATION_ERROR. A Cloudflare edge block (see
        # _is_cloudflare_1010 above) is a different, more specific
        # situation -- the request was rejected before the provider's
        # own key check ever ran -- so it gets its own classification
        # rather than being reported the same way as "the provider
        # itself says this key/account isn't authorized."
        if _is_cloudflare_1010(raw_body or msg):
            return "ACCESS_RESTRICTED"
        return "AUTHENTICATION_ERROR"
    if code == 402:
        return "NO_CREDITS"
    if code == 404:
        # OpenRouter's actual wording for a free-tier variant that's
        # temporarily exhausted/pulled: "This model is unavailable for
        # free. The paid version is available now." -- a model/routing
        # problem, not a bad model id and definitely not a bad key.
        if "unavailable for free" in low or "paid version" in low:
            return "MODEL_UNAVAILABLE"
        return "MODEL_NOT_FOUND"
    if code == 429:
        return "RATE_LIMITED"
    if code == 400:
        if "vision" in low or "image" in low and "not support" in low:
            return "VISION_NOT_SUPPORTED"
        return "BAD_REQUEST"
    if 500 <= code < 600:
        return "SERVER_ERROR"
    return "UNKNOWN_ERROR"


def _post(url,body,headers,timeout=30):
    req=urllib.request.Request(url,data=json.dumps(body).encode(),
                               headers=_std_headers(headers),method="POST")
    try:
        with urllib.request.urlopen(req,timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            raw=e.read().decode(errors='replace')
        except Exception:
            raw=""
        try:
            parsed_msg=json.loads(raw).get("error",{}).get("message") if raw else None
        except Exception:
            parsed_msg=None
        # Classification looks at the FULL raw body (the Cloudflare
        # 1010 marker can sit well past the first 300 characters of
        # its block page), but the message shown/logged stays short.
        msg = parsed_msg or (raw[:300] if raw else str(e))
        raise ProviderError(_classify_http_error(e.code, msg, raw), f"HTTP {e.code}: {msg}")
    except urllib.error.URLError as e:
        raise ProviderError("NETWORK_ERROR", f"Network error: {str(e.reason)}")

def _normalize_paths(path):
    """Every provider call ultimately accepts EITHER a single image path
    (the original, still-default behavior — Meta Generator, Smart
    Workflow, single-image Prompt Generator, and Prompt-to-Prompt's
    original Image mode all still pass a plain string, completely
    unaffected by this) OR a list of paths, for Prompt-to-Prompt's
    multi-image Image mode (up to 15 reference images analyzed together
    in one call). This just normalizes either shape to a list once, so
    every provider function below can loop over it uniformly."""
    if not path: return []
    if isinstance(path,(list,tuple)): return list(path)
    return [path]

def call_gemini(key,model,path,prompt,max_tokens=2200):
    parts=[{"text":prompt}]
    for p in reversed(_normalize_paths(path)):
        b64,mime=img_to_b64(p)
        parts.insert(0,{"inline_data":{"mime_type":mime,"data":b64}})
    r=_post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
        {"contents":[{"parts":parts}],
         "generationConfig":{"temperature":0.3,"maxOutputTokens":max_tokens}},
        {"Content-Type":"application/json"})
    try:
        text = r["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        raise ProviderError("INVALID_RESPONSE_FORMAT", f"Gemini parse error: {str(r)[:200]}")
    if not text or not text.strip():
        raise ProviderError("EMPTY_RESPONSE", "Gemini returned an empty response")
    return text

def _oa_style_content(path,prompt):
    """Content list shared by every OpenAI-chat-format provider
    (OpenRouter/OpenAI/Groq/Grok/Mistral) — image part(s) omitted
    entirely for a text-only call (Prompt-to-Prompt's text mode), so
    those providers never see an image field they didn't ask for.
    Supports one image or several — see _normalize_paths."""
    content=[{"type":"text","text":prompt}]
    for p in reversed(_normalize_paths(path)):
        b64,mime=img_to_b64(p)
        content.insert(0,{"type":"image_url","image_url":{"url":f"data:{mime};base64,{b64}"}})
    return content

def _extract_oa_content(r,provider_name):
    """Shared response parsing for every OpenAI-chat-format provider
    (OpenRouter/OpenAI/Groq/Grok/Mistral/Cerebras). Distinguishes three
    different failure shapes that were previously all collapsed into
    one generic "parse error":
      - the provider returned something that isn't shaped like a chat
        completion at all (INVALID_RESPONSE_FORMAT) -- this is what
        happens when, e.g., an error body slips past _post's own
        HTTPError handling in a non-standard shape;
      - the provider returned a well-formed response with a genuinely
        empty message (EMPTY_RESPONSE) -- seen from some free/preview
        models under load, distinct from a malformed response;
      - a normal, non-empty completion, returned as-is.
    """
    try:
        choice = r["choices"][0]
        content = choice["message"]["content"]
    except Exception:
        raise ProviderError("INVALID_RESPONSE_FORMAT", f"{provider_name} parse error: {str(r)[:200]}")
    if content is None or not str(content).strip():
        raise ProviderError("EMPTY_RESPONSE", f"{provider_name} returned an empty response")
    return content

def _claude_style_content(path,prompt):
    content=[{"type":"text","text":prompt}]
    for p in reversed(_normalize_paths(path)):
        b64,mime=img_to_b64(p)
        content.insert(0,{"type":"image","source":{"type":"base64","media_type":mime,"data":b64}})
    return content

def call_openrouter(key,model,path,prompt,max_tokens=2200):
    r=_post("https://openrouter.ai/api/v1/chat/completions",
        {"model":model,"max_tokens":max_tokens,"messages":[
            {"role":"user","content":_oa_style_content(path,prompt)}]},
        {"Content-Type":"application/json","Authorization":f"Bearer {key}",
         "HTTP-Referer":"https://metazone.app","X-Title":"Meta Zone"})
    return _extract_oa_content(r, "OpenRouter")

def call_claude(key,model,path,prompt,max_tokens=2200):
    r=_post("https://api.anthropic.com/v1/messages",
        {"model":model,"max_tokens":max_tokens,"messages":[
            {"role":"user","content":_claude_style_content(path,prompt)}]},
        {"Content-Type":"application/json","x-api-key":key,"anthropic-version":"2023-06-01"})
    try:
        text = r["content"][0]["text"]
    except Exception:
        raise ProviderError("INVALID_RESPONSE_FORMAT", f"Claude parse error: {str(r)[:200]}")
    if not text or not text.strip():
        raise ProviderError("EMPTY_RESPONSE", "Claude returned an empty response")
    return text

def call_openai(key,model,path,prompt,max_tokens=2200):
    r=_post("https://api.openai.com/v1/chat/completions",
        {"model":model,"max_tokens":max_tokens,"messages":[
            {"role":"user","content":_oa_style_content(path,prompt)}]},
        {"Content-Type":"application/json","Authorization":f"Bearer {key}"})
    return _extract_oa_content(r, "OpenAI")

def call_groq(key,model,path,prompt,max_tokens=2200):
    r=_post("https://api.groq.com/openai/v1/chat/completions",
        {"model":model,"max_tokens":max_tokens,"messages":[
            {"role":"user","content":_oa_style_content(path,prompt)}]},
        {"Content-Type":"application/json","Authorization":f"Bearer {key}"})
    return _extract_oa_content(r, "Groq")

def call_grok(key,model,path,prompt,max_tokens=2200):
    """xAI's Grok — not to be confused with Groq (LPU inference cloud)
    above. Different company, different endpoint, different key format
    (xAI keys look like 'xai-...'; Groq keys look like 'gsk_...')."""
    r=_post("https://api.x.ai/v1/chat/completions",
        {"model":model,"max_tokens":max_tokens,"messages":[
            {"role":"user","content":_oa_style_content(path,prompt)}]},
        {"Content-Type":"application/json","Authorization":f"Bearer {key}"})
    return _extract_oa_content(r, "Grok")

def call_mistral(key,model,path,prompt,max_tokens=2200):
    r=_post("https://api.mistral.ai/v1/chat/completions",
        {"model":model,"max_tokens":max_tokens,"messages":[
            {"role":"user","content":_oa_style_content(path,prompt)}]},
        {"Content-Type":"application/json","Authorization":f"Bearer {key}"})
    return _extract_oa_content(r, "Mistral")

def call_cerebras(key,model,path,prompt,max_tokens=2200):
    """Cerebras (api.cerebras.ai) -- OpenAI-compatible /v1/chat/completions,
    same Authorization: Bearer header shape as Groq/OpenAI/Mistral. Text
    only: Cerebras' free tier serves no vision-capable models, so `path`
    is expected to be empty/unused here in practice -- get_active_keys()
    only offers Cerebras for text-only calls (see require_vision below),
    but _oa_style_content is still used unmodified so a stray image
    path doesn't silently vanish; Cerebras itself will reject it with a
    normal 400 if one ever gets through, which _post classifies as
    BAD_REQUEST/VISION_NOT_SUPPORTED rather than mis-reporting it as an
    invalid key."""
    r=_post("https://api.cerebras.ai/v1/chat/completions",
        {"model":model,"max_tokens":max_tokens,"messages":[
            {"role":"user","content":_oa_style_content(path,prompt)}]},
        {"Content-Type":"application/json","Authorization":f"Bearer {key}"})
    return _extract_oa_content(r, "Cerebras")

CALLERS={"Gemini":call_gemini,"OpenRouter":call_openrouter,"Claude":call_claude,
         "OpenAI":call_openai,"Groq":call_groq,"Mistral":call_mistral,"Grok":call_grok,
         "Cerebras":call_cerebras}

# ── API key validation (lightweight, cheap calls) ──────────────────────
def validate_key(provider, key):
    """Returns (ok: bool, message: str)"""
    key = key.strip()
    if not key:
        return False, "Empty key"
    try:
        if provider == "Gemini":
            url = f"https://generativelanguage.googleapis.com/v1beta/models?key={key}"
            req = urllib.request.Request(url, headers=_std_headers(), method="GET")
            with urllib.request.urlopen(req, timeout=12) as r:
                json.loads(r.read())
            return True, "Valid"
        elif provider == "OpenRouter":
            req = urllib.request.Request("https://openrouter.ai/api/v1/auth/key",
                headers=_std_headers({"Authorization": f"Bearer {key}"}), method="GET")
            with urllib.request.urlopen(req, timeout=12) as r:
                json.loads(r.read())
            return True, "Valid"
        elif provider == "Mistral":
            req = urllib.request.Request("https://api.mistral.ai/v1/models",
                headers=_std_headers({"Authorization": f"Bearer {key}"}), method="GET")
            with urllib.request.urlopen(req, timeout=12) as r:
                json.loads(r.read())
            return True, "Valid"
        elif provider == "Groq":
            req = urllib.request.Request("https://api.groq.com/openai/v1/models",
                headers=_std_headers({"Authorization": f"Bearer {key}"}), method="GET")
            with urllib.request.urlopen(req, timeout=12) as r:
                json.loads(r.read())
            return True, "Valid"
        elif provider == "Cerebras":
            # v0.9.7.2: official Cerebras endpoint/header, matching the
            # same GET .../models pattern used for Groq/OpenAI/Mistral
            # above (Cerebras' inference API is OpenAI-compatible,
            # confirmed against inference-docs.cerebras.ai). This is
            # what was entirely missing before -- there was no Cerebras
            # branch at all, so any key "validated" against Cerebras
            # previously fell through to the bottom `return False,
            # "Unknown"` no matter how valid the key was.
            # v0.9.7.5: _std_headers() (real User-Agent/Accept) is what
            # actually fixes error 1010 here -- see the module docstring
            # near _UA above. Without it, this request (and Groq's right
            # above it) never even reached Cerebras/Groq's own auth
            # check; Cloudflare's edge rejected it first.
            req = urllib.request.Request("https://api.cerebras.ai/v1/models",
                headers=_std_headers({"Authorization": f"Bearer {key}"}), method="GET")
            with urllib.request.urlopen(req, timeout=12) as r:
                json.loads(r.read())
            return True, "Valid"
        elif provider == "OpenAI":
            req = urllib.request.Request("https://api.openai.com/v1/models",
                headers=_std_headers({"Authorization": f"Bearer {key}"}), method="GET")
            with urllib.request.urlopen(req, timeout=12) as r:
                json.loads(r.read())
            return True, "Valid"
        elif provider == "Claude":
            body = json.dumps({"model":"claude-haiku-4-5-20251001","max_tokens":1,
                               "messages":[{"role":"user","content":"hi"}]}).encode()
            req = urllib.request.Request("https://api.anthropic.com/v1/messages",
                data=body, headers=_std_headers({"Content-Type":"application/json","x-api-key":key,
                "anthropic-version":"2023-06-01"}), method="POST")
            with urllib.request.urlopen(req, timeout=12) as r:
                json.loads(r.read())
            return True, "Valid"
        elif provider == "Grok":
            req = urllib.request.Request("https://api.x.ai/v1/models",
                headers=_std_headers({"Authorization": f"Bearer {key}"}), method="GET")
            with urllib.request.urlopen(req, timeout=12) as r:
                json.loads(r.read())
            return True, "Valid"
    except urllib.error.HTTPError as e:
        try: full_body=e.read().decode("utf-8","replace")
        except Exception: full_body=""
        body=full_body[:200]
        if e.code == 401:
            return False, f"Invalid key" + (f" — {body}" if body else "")
        elif e.code == 403:
            # v0.9.7.5: a 403 whose body is Cloudflare's own "error code:
            # 1010" block page (see _is_cloudflare_1010) never reached
            # the provider's own key check at all -- the request was
            # rejected at the edge. That's a materially different,
            # clearer situation than "the provider says this key/account
            # isn't authorized" (still possible, still handled below),
            # so it gets its own explicit message rather than the same
            # generic "authentication error" text. Checked against the
            # FULL body (not the 200-char `body` used for display),
            # since the 1010 marker can sit past that cutoff.
            if _is_cloudflare_1010(full_body):
                return False, ("Access restricted (Cloudflare error 1010 — the request was blocked at "
                                "Cloudflare's edge before it reached this provider's own key check. "
                                "This is NOT proof the API key itself is invalid; it's often transient. "
                                "Try Test again, and if it keeps happening this may be a regional/network "
                                "restriction on this connection rather than the key.")
            # v0.9.7.2: 403 is "authentication succeeded but this key/
            # account isn't allowed to do this" (e.g. a Groq/Cerebras/
            # OpenRouter key that's valid but region/plan-restricted,
            # or lacks a permission scope) -- genuinely different from
            # 401's "the key itself is wrong", so it must NOT collapse
            # into the same "Invalid key" message record_key_test()
            # keys its "invalid" status off of (see settings.py).
            return False, f"Authentication error (not necessarily an invalid key)" + (f" — {body}" if body else "")
        elif e.code == 429:
            return True, "Valid (rate-limited)"
        elif e.code == 402:
            return False, f"No credits/quota remaining" + (f" — {body}" if body else "")
        else:
            return False, f"HTTP {e.code}" + (f" — {body}" if body else "")
    except Exception as e:
        return False, f"Error: {str(e)[:40]}"
    return False, "Unknown"

def get_active_keys(prefs, require_vision=False):
    """require_vision=True (image-to-metadata tasks) excludes any
    provider whose AI_PROVIDERS entry doesn't have vision: True --
    e.g. Cerebras, which serves no multimodal models on its free tier.
    require_vision=False (text-only tasks, e.g. Prompt-to-Prompt's text
    mode) offers every active provider, vision-capable or not, since a
    text-only model is perfectly usable there. Providers with no
    "vision" key at all default to True (every provider defined before
    this flag existed was, in fact, vision-capable) so this is
    additive and doesn't change behavior for anyone who hasn't touched
    the new Cerebras provider."""
    seq=[]
    for provider,cfg in AI_PROVIDERS.items():
        if provider in HIDDEN_PROVIDERS: continue
        if require_vision and not cfg.get("vision", True): continue
        keys=prefs.get("ai_keys",{}).get(provider,[])
        model=prefs.get("ai_models",{}).get(provider, cfg["models"][0][1])
        active_keys=[k for k in keys if k.get("active") and k.get("key")]
        for i,k in enumerate(active_keys,1):
            seq.append((provider,k["key"],model,i))
    return seq

def _fallback_models(provider, current_model, prefs, require_vision):
    """v0.9.7.5: alternate enabled models for this provider (current
    model first), used by call_with_failover to retry a
    MODEL_UNAVAILABLE failure -- e.g. OpenRouter's ":free" tier variant
    getting pulled, the exact bug report this shipped to fix -- against
    a different model on the SAME key before giving up on that key/
    provider entirely. Respects set_model_enabled's disabled_models
    (a model the user explicitly hid from the dropdown is never
    silently used here either) and the same require_vision filtering
    get_active_keys already applies, so this can't hand an image task
    to a text-only provider/model either."""
    cfg = AI_PROVIDERS.get(provider, {})
    if require_vision and not cfg.get("vision", True):
        return [current_model] if current_model else []
    disabled = set(prefs.get("disabled_models", {}).get(provider, []))
    all_ids = [mid for (_, mid) in cfg.get("models", [])]
    ordered = [m for m in all_ids if m not in disabled]
    if current_model in ordered:
        ordered.remove(current_model)
        ordered.insert(0, current_model)
    elif current_model:
        ordered.insert(0, current_model)
    return ordered or ([current_model] if current_model else [])


def call_with_failover(path,prompt,prefs,status_cb=None,max_tokens=2200,require_vision=None):
    """Try each active key in order. On failure, immediately move to
    the next key -- EXCEPT a MODEL_UNAVAILABLE failure (the selected
    model itself isn't servable, e.g. OpenRouter's ":free" tier variant
    pulled -- see _classify_http_error), which retries the SAME key
    against the provider's next enabled model (see _fallback_models)
    before moving on. That's what turns "the whole OpenRouter key
    failed" into "that one free model was unavailable, the next one
    worked" instead of burning through every configured key over a
    single stale model id. If every attempt across every key/model
    failed with MODEL_UNAVAILABLE specifically, the final error says so
    plainly ("no available free model") rather than the generic
    "all keys failed" wording, which previously made a pure
    model-availability problem look exactly like a broken API key.

    require_vision: if None (default), infer from `path` -- any image
    task (path is a non-empty string/list) requires a vision-capable
    provider; a text-only call (path is empty/None, e.g. Prompt-to-
    Prompt's text mode) does not. Callers can still pass an explicit
    True/False to override the inference. This is the capability-aware
    selection Problem B/section 5 calls for: an image task can no
    longer silently land on a text-only provider (previously nothing
    stopped Cerebras -- or any future text-only provider -- from being
    handed an image path it can't use, then reporting a confusing
    provider-side 400 that looked like "the app is broken" rather than
    "no eligible vision model is configured")."""
    if require_vision is None:
        require_vision = bool(path)
    seq=get_active_keys(prefs, require_vision=require_vision)
    if not seq:
        if require_vision:
            raise RuntimeError("No active vision-capable API keys. Text-only providers (e.g. Cerebras) can't process images -- open 'Configuration' and enable a vision-capable provider (Gemini, Mistral, OpenRouter, Groq, or OpenAI).")
        raise RuntimeError("No active API keys. Open 'Configuration'.")
    last_err=""
    last_classification="UNKNOWN_ERROR"
    saw_any_attempt=False
    all_model_unavailable=True
    for provider,key,model,key_idx in seq:
        for m in _fallback_models(provider, model, prefs, require_vision):
            saw_any_attempt=True
            try:
                if status_cb:
                    status_cb(f"{provider} · {model_label(provider,m)}…")
                raw=CALLERS[provider](key,m,path,prompt,max_tokens=max_tokens)
                return raw,provider,m,key_idx
            except Exception as e:
                last_classification = getattr(e, "classification", "UNKNOWN_ERROR")
                last_err=f"{provider} [{last_classification}]: {str(e)[:120]}"
                if last_classification == "MODEL_UNAVAILABLE":
                    # Same key, next enabled model for this provider.
                    continue
                all_model_unavailable=False
                # Any other failure (bad key, rate limit, network,
                # etc.) -- stop trying more models on this key and
                # move on to the next key/provider.
                break
    if saw_any_attempt and all_model_unavailable:
        raise RuntimeError(f"No available free model could process this request. Last error: {last_err}")
    raise RuntimeError(f"All keys failed. Last error: {last_err}")

