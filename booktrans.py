#!/usr/bin/env python3
"""
booktrans - structure-preserving EPUB literary translation (English -> Hindi)

Pipeline:
  Pass 0  Build a "Book Bible" (register, characters, formality matrix, glossary)
  Pass 1  Translate paragraph-batches, preserving inline HTML markup
  Pass 2  Editor pass (fidelity + flow + glossary adherence)
  Pass 3  Reassemble EPUB with Devanagari typography

Design notes:
  - Operates on each block's INNER HTML, not plain text, so <i>/<em>/<span>
    emphasis survives translation. Hindi word order differs, so the model is
    told to MOVE tags to the semantically correct word.
  - Everything checkpoints to disk. A crash at chapter 14 must not cost you
    a day of free-tier quota.
  - Strictly sequential. Free-tier RPM caps throughput anyway, and parallel
    requests just trigger 429s.

Usage:
  export GEMINI_API_KEY=...
  python booktrans.py book.epub -o book.hi.epub
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import random
import re
import shutil
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from urllib.parse import unquote
from typing import Any, Iterator

from bs4 import BeautifulSoup, NavigableString, Tag

try:
    from google import genai
    from google.genai import types as genai_types
except ImportError:
    genai = None


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

@dataclasses.dataclass
class Config:
    # Ordered fallback chain. Free-tier daily quota is per model, so when one
    # is exhausted the next is tried. Cheapest/highest-quota first: burning the
    # scarce high-RPD models on easy batches wastes them.
    models: tuple[str, ...] = (
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-3-flash-preview",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
    )   # free quota is counted PER MODEL, so a wide chain multiplies your
        # daily budget. Run --check for what this key actually has.

    # Words per request. Keep well under the 250k TPM free-tier ceiling:
    # bible + summary + context + source + output must all fit in one minute.
    batch_words: int = 900

    # 8192 is enough for a translation batch but NOT for a bible with a full
    # terminology list plus an archaism scan; that silently truncates the JSON.
    max_output_tokens: int = 16384
    bible_max_output_tokens: int = 32768
    # Review each chapter before translating it: pick up new characters and
    # address-form shifts, scoped to take effect from that chapter forward.
    review_each_chapter: bool = True

    # None = do not send thinking_config at all. Several Gemini 3.x models
    # reject a zero budget with an opaque 400, so this is opt-in via
    # --thinking. Truncation is handled by batch splitting instead.
    translate_thinking: int | None = None

    # When every model is exhausted, sleep until quota resets and continue
    # instead of exiting. Needed for full books, which span several days.
    wait_for_quota: bool = False
    # Cap on how long --wait will sleep. A job with a hard time limit can
    # afford to wait out a reset that is close, but not one hours away.
    max_wait: float = 10 ** 9

    max_retries: int = 6
    base_backoff: float = 4.0
    min_request_gap: float = 6.5   # ~9 RPM, under the 10 RPM free-tier cap

    source_lang: str = "English"
    target_lang: str = "Hindi"

    run_editor_pass: bool = True

    # "fiction"    -> voice, characters, formality matrix, preserve sentence rhythm
    # "nonfiction" -> terminology lock, argument thread, sentences may be split
    profile: str = "fiction"

    # None lets the model choose the register per book; a preset forces it.
    register: str | None = None

    # Blocks in these CSS classes are left untouched (indexes, page refs).
    skip_classes: tuple[str, ...] = ("index", "indexmaindf", "indexsub",
                                     "indexmain", "toc", "pagenum")

    # Spine documents whose filename matches this are skipped entirely.
    # Deliberately narrow: Calibre often names chapter files index_split_NNN,
    # so matching "index" here would skip the whole book. Index CONTENT is
    # excluded by skip_classes instead, which keys off CSS classes.
    skip_docs: str = r"(^|[^a-z])(copyright|colophon)([^a-z]|$)"

    # Blocks whose inner text is translated.
    block_tags: tuple[str, ...] = ("p", "h1", "h2", "h3", "h4", "h5", "h6",
                                   "blockquote", "li", "td", "th")

    # Used only when the tags above find nothing. NEVER promote these into
    # block_tags: block_index is an index into find_all(block_tags), so a
    # wider default remaps every key in every existing checkpoint and lands
    # translations on the wrong paragraphs.
    fallback_block_tags: tuple[str, ...] = (
        "p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "li", "td",
        "th", "div", "dd", "dt", "article", "section")


# --------------------------------------------------------------------------
# Rate-limited Gemini client
# --------------------------------------------------------------------------

def brief_error(exc: Exception) -> str:
    """One readable line. Google sometimes returns an HTML error page."""
    txt = re.sub(r"\s+", " ", str(exc))
    if "<!DOCTYPE html" in txt or "<html" in txt:
        code = re.search(r"\b(4\d\d|5\d\d)\b", txt)
        return (f"HTTP {code.group(1) if code else '???'} "
                f"(HTML error page, not an API response)")
    return txt[:200]


class ChainExhausted(RuntimeError):
    """Every model failed. `cause` says whether that is about capacity or content.

    capacity - quota spent, rate limited, overloaded. Waiting fixes it.
    content  - every model returned nothing usable for THIS request. Waiting
               changes nothing; the request has to get smaller.
    """

    def __init__(self, message: str, cause: str):
        super().__init__(message)
        self.cause = cause


def classify_error(exc: Exception) -> str:
    """Work out what actually went wrong. Each class needs a different response."""
    msg = str(exc).lower()
    if any(k in msg for k in ("api key not valid", "api_key_invalid",
                              "unauthenticated", "permission_denied",
                              "invalid authentication", "403", "forbidden",
                              "api has not been used", "is disabled")):
        return "auth"
    if any(k in msg for k in ("not found", "404", "is not supported",
                              "unsupported model", "does not exist")):
        return "missing"
    if any(k in msg for k in ("429", "resource_exhausted", "quota",
                              "rate limit", "exceeded your current quota")):
        return "quota"
    if any(k in msg for k in ("safety", "blocked", "prohibited_content",
                              "recitation")):
        return "blocked"
    if "token output limit" in msg:
        return "truncated"
    if "empty response" in msg:
        return "empty"
    if "invalid_argument" in msg or "invalid argument" in msg or "400" in msg:
        return "invalid"
    if any(k in msg for k in ("503", "unavailable", "overloaded",
                              "high demand", "try again later")):
        return "overloaded"
    return "transient"


RETRY_RE = re.compile(r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)\s*s", re.I)
QUOTA_VAL_RE = re.compile(r"quotaValue['\"]?\s*:\s*['\"]?(\d+)", re.I)
PER_DAY_RE = re.compile(r"PerDay|per_day|RequestsPerDay", re.I)


def quota_details(exc: Exception) -> tuple[bool, float | None, int | None]:
    """Pull (is_daily, retry_after_seconds, limit) out of a 429 payload.

    Google tells us precisely what was hit; guessing at cooldowns when the
    answer is in the error is wasted time.
    """
    msg = str(exc)
    daily = bool(PER_DAY_RE.search(msg))
    m = RETRY_RE.search(msg)
    retry = float(m.group(1)) if m else None
    q = QUOTA_VAL_RE.search(msg)
    limit = int(q.group(1)) if q else None
    return daily, retry, limit


NON_TEXT_RE = re.compile(
    r"(image|tts|audio|video|embedding|robotics|lyria|banana|veo|imagen|"
    r"computer-use|antigravity|deep-research|customtools|omni)", re.I)


def suggest_chain(models: list[str]) -> list[str]:
    """Keep plain text-generation models, newest first.

    Excludes image/TTS/video/robotics variants (wrong modality) and the
    `-latest` aliases, which point at a concrete model and therefore share
    its quota rather than adding a fresh allowance.
    """
    keep = [m for m in models
            if m.startswith("gemini-")
            and not NON_TEXT_RE.search(m)
            and not m.endswith("-latest")]

    def rank(name: str) -> tuple[float, int]:
        v = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
        ver = float(v.group(1)) if v else 0.0
        # flash-lite has the largest allowance, pro the smallest
        tier = 0 if "flash-lite" in name else (1 if "flash" in name else 2)
        return (-ver, tier)

    return sorted(keep, key=rank)


def next_quota_reset() -> float:
    """Unix time of the next free-tier daily reset (midnight America/Los_Angeles).

    zoneinfo needs the tzdata package on Windows, which is often absent, so
    fall back to a fixed UTC-8. That errs an hour late during daylight saving,
    which is the safe direction: waking early just means another 429.
    """
    import datetime as dt
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Los_Angeles")
    except Exception:
        tz = dt.timezone(dt.timedelta(hours=-8))
    now = dt.datetime.now(tz)
    nxt = (now + dt.timedelta(days=1)).replace(
        hour=0, minute=1, second=0, microsecond=0)
    return nxt.timestamp()


def fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, m = divmod(seconds // 60, 60)
    if h >= 24:
        return f"{h // 24}d {h % 24}h"
    return f"{h}h {m:02d}m" if h else f"{m}m {seconds % 60:02d}s"


class Gemini:
    """Sequential, rate-limited client that cascades across a model chain.

    Free-tier daily quota is counted per model, so when one is exhausted the
    right move is to switch models, not to sleep. Sleeping only helps for
    per-minute limits and transient server errors.
    """

    def __init__(self, cfg: Config, api_key: str | None = None):
        if genai is None:
            raise RuntimeError("python -m pip install google-genai beautifulsoup4")
        key = api_key or os.environ.get("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("Set GEMINI_API_KEY")
        if key.strip() in ("...", "your_key", "YOUR_KEY"):
            raise RuntimeError(
                "GEMINI_API_KEY is a placeholder, not a real key. "
                "Get one at https://aistudio.google.com/apikey")
        self.client = genai.Client(api_key=key)
        self.key_id = hashlib.sha1(key.encode()).hexdigest()[:12]
        self.cfg = cfg
        self._last_call = 0.0
        self.calls = 0
        self.retries = 0
        self.warnings: list[str] = []
        self.model_calls: dict[str, int] = {}
        self._cooldown: dict[str, float] = {}   # model -> unix time it frees up
        self._strikes: dict[str, int] = {}
        self.limits: dict[str, int] = {}          # model -> observed daily quota
        self._reason: dict[str, str] = {}         # model -> why it is benched
        self._dead: set[str] = set()            # model does not exist for this key
        self._no_thinking_cfg = False           # SDK/model rejects ThinkingConfig

    # -- availability ------------------------------------------------------

    def _ready(self, model: str) -> bool:
        return model not in self._dead and time.time() >= self._cooldown.get(model, 0)

    def _benched(self, model: str, kind: str, exc: Exception | None = None) -> None:
        if kind == "missing":
            self._dead.add(model)
            print(f"    - {model}: not available to this key, dropping it")
            return

        daily, retry, limit = quota_details(exc) if exc else (False, None, None)
        if limit:
            self.limits[model] = limit
        if daily:
            # A per-day quota does not come back in minutes. Retrying wastes
            # wall clock; move on and let the next model carry the work.
            self._cooldown[model] = next_quota_reset()
            self._reason[model] = "daily quota"
            cap = f" (limit {limit}/day)" if limit else ""
            eta = fmt_duration(self._cooldown[model] - time.time())
            print(f"    - {model}: daily quota spent{cap}, resets in {eta}")
            return

        n = self._strikes[model] = self._strikes.get(model, 0) + 1
        wait = retry + 2 if retry else min(60 * (2 ** (n - 1)), 3600)
        self._cooldown[model] = time.time() + wait
        self._reason[model] = "rate limit"
        src = "server says" if retry else "backoff"
        print(f"    - {model}: rate limited, benched {wait:.0f}s ({src})")

    def available(self) -> list[str]:
        return [m for m in self.cfg.models if self._ready(m)]

    def probe(self) -> list[str]:
        """Ask the API which models this key can actually use."""
        names = []
        for m in self.client.models.list():
            n = getattr(m, "name", "").replace("models/", "")
            actions = getattr(m, "supported_actions", None) or []
            if not actions or "generateContent" in actions:
                names.append(n)
        return names

    # -- calling -----------------------------------------------------------

    def _sleep_until(self, when: float) -> None:
        """Sleep with a visible countdown so a multi-hour wait is legible."""
        total = when - time.time()
        if total <= 0:
            return
        live = [m for m in self._cooldown if m not in self._dead]
        reasons = sorted({self._reason.get(m, "unavailable") for m in live})
        why = " and ".join(reasons) or "unavailable"
        print(f"\n  Every model is benched ({why}). "
              f"Waiting {fmt_duration(total)}.")
        print("  Progress is checkpointed; Ctrl-C is safe and the run resumes "
              "from here.")
        while True:
            left = when - time.time()
            if left <= 0:
                break
            print(f"\r  resuming in {fmt_duration(left)}   ", end="", flush=True)
            time.sleep(min(30, left))
        print("\r  wait over, continuing".ljust(50))
        # Only free models whose bench has actually expired. Clearing daily
        # cooldowns early just re-probes dead models and spends calls.
        now = time.time()
        for m in [k for k, v in self._cooldown.items() if v <= now]:
            self._cooldown.pop(m, None)
            self._strikes.pop(m, None)

    def _throttle(self) -> None:
        gap = time.time() - self._last_call
        if gap < self.cfg.min_request_gap:
            time.sleep(self.cfg.min_request_gap - gap)
        self._last_call = time.time()

    def ask(self, system: str, prompt: str, temperature: float = 0.35,
            json_mode: bool = False, max_tokens: int | None = None,
            thinking: int | None = None) -> str:
        kwargs: dict[str, Any] = {
            "system_instruction": system,
            "temperature": temperature,
            "max_output_tokens": max_tokens or self.cfg.max_output_tokens,
        }
        if json_mode:
            kwargs["response_mime_type"] = "application/json"
        # Reasoning tokens count against max_output_tokens. Translation is a
        # mechanical transformation, so thinking mostly consumes budget that
        # the Hindi output then runs out of. The bible, which is genuine
        # analysis, passes thinking=None and keeps the model's default.
        if thinking is not None and not self._no_thinking_cfg:
            try:
                kwargs["thinking_config"] = genai_types.ThinkingConfig(
                    thinking_budget=thinking)
            except Exception:
                self._no_thinking_cfg = True

        last_err: Exception | None = None
        # Bounded: a wait that keeps finding no quota must give up and
        # let the caller checkpoint, not spin.
        for _pass in range(4 if self.cfg.wait_for_quota else 2):
            for model in self.cfg.models:
                if not self._ready(model):
                    continue
                for attempt in range(self.cfg.max_retries):
                    self._throttle()
                    try:
                        resp = self.client.models.generate_content(
                            model=model, contents=prompt,
                            config=genai_types.GenerateContentConfig(**kwargs))
                        reason = block = ""
                        try:
                            reason = str(resp.candidates[0].finish_reason or "")
                        except Exception:
                            pass
                        try:
                            block = str(resp.prompt_feedback.block_reason or "")
                        except Exception:
                            pass
                        if "MAX_TOKENS" in reason.upper():
                            raise RuntimeError(
                                f"response hit the {kwargs['max_output_tokens']} "
                                f"token output limit and was cut off")
                        if not resp.text:
                            raise RuntimeError(
                                f"empty response (finish_reason="
                                f"{reason or 'unknown'}"
                                + (f", block_reason={block}" if block else "")
                                + ")")
                        self.calls += 1
                        self.model_calls[model] = self.model_calls.get(model, 0) + 1
                        return resp.text
                    except Exception as exc:      # noqa: BLE001
                        last_err = exc
                        kind = classify_error(exc)
                        # Some models reject thinking_budget=0 outright and
                        # the message does not say so. If we sent one, drop it
                        # and retry before concluding anything else.
                        if (kind == "invalid" and "thinking_config" in kwargs
                                and not self._no_thinking_cfg):
                            self._no_thinking_cfg = True
                            kwargs.pop("thinking_config", None)
                            print(f"    - {model}: rejected thinking_config, "
                                  f"retrying without it")
                            continue
                        if kind == "auth":
                            raise RuntimeError(
                                f"API key rejected by {model}: "
                                f"{brief_error(exc)}\n"
                                "  This is a key/project problem, not a quota "
                                "one, so retrying cannot help. Check:\n"
                                "  1. The key exists and belongs to the "
                                "intended project:\n"
                                "     https://aistudio.google.com/apikey\n"
                                "  2. The Generative Language API is enabled "
                                "for that project. A key made in the Cloud\n"
                                "     Console does not enable it; a key made "
                                "in AI Studio does.\n"
                                "  3. The key has no HTTP-referrer or IP "
                                "restriction (CI has no fixed IP).\n"
                                "  4. The secret has no stray whitespace or "
                                "newline.\n"
                                "  Verify with: python booktrans.py --check"
                            ) from exc
                        if kind in ("quota", "missing"):
                            self._benched(model, kind, exc)
                            break                 # next model, do not sleep
                        if kind == "invalid":
                            # Deterministic. Retrying wastes quota; surface it.
                            raise RuntimeError(
                                f"{model} rejected the request as invalid, and "
                                f"retrying cannot help.\n  {str(exc)[:300]}"
                            ) from exc
                        if kind == "truncated":
                            # Deterministic: the same request will overflow
                            # again. Surface it so the caller sends less.
                            raise
                        if kind == "empty":
                            # Same request, same empty result. One retry in
                            # case it really was a blip, then try elsewhere.
                            if attempt >= 1:
                                self._reason[model] = "empty response"
                                print(f"    - {model}: empty response, "
                                      f"trying next model")
                                break
                            self.retries += 1
                            time.sleep(2)
                            continue
                        if kind == "overloaded":
                            # Another model is idle; switching beats waiting.
                            # Escalate, or a chain down to one busy model
                            # spins: wake, retry, bench, repeat.
                            n = self._strikes[model] = self._strikes.get(model, 0) + 1
                            wait = min(45 * (2 ** (n - 1)), 900)
                            self._cooldown[model] = time.time() + wait
                            self._reason[model] = "overloaded"
                            print(f"    - {model}: overloaded, trying next model "
                                  f"(back off {fmt_duration(wait)})")
                            break
                        if kind == "blocked":
                            self._reason[model] = "content blocked"
                            self.warnings.append(f"{model}: content blocked - {exc}")
                            break
                        self.retries += 1
                        wait = self.cfg.base_backoff * (2 ** attempt)
                        wait += random.uniform(0, 2)
                        print(f"    ! {model}: {brief_error(exc)[:120]}")
                        print(f"      retry in {wait:.0f}s "
                              f"({attempt + 1}/{self.cfg.max_retries})", flush=True)
                        time.sleep(wait)

            waits = [t for m, t in self._cooldown.items() if m not in self._dead]
            soonest = min(waits) - time.time() if waits else 0
            limit_s = self.cfg.max_wait if self.cfg.wait_for_quota else 300
            if soonest > 0 and (limit_s is None or soonest <= limit_s):
                self._sleep_until(time.time() + soonest + 2)
                continue
            if self.cfg.wait_for_quota and soonest > 0:
                print(f"\n  Next reset is {fmt_duration(soonest)} away, beyond "
                      f"the {fmt_duration(self.cfg.max_wait)} wait limit.")
            break

        live = [m for m in self.cfg.models if m not in self._dead]
        now = time.time()
        state = []
        for m in self.cfg.models:
            if m in self._dead:
                state.append(f"    {m}: dropped (unavailable)")
            elif m in self._cooldown:
                state.append(f"    {m}: {self._reason.get(m, 'benched')}, "
                             f"free in {fmt_duration(self._cooldown[m] - now)}")
            else:
                state.append(f"    {m}: not benched - failed without a "
                             f"recognised reason")
        benched = [m for m in self.cfg.models
                   if m in self._cooldown and self._cooldown[m] > now]
        cause = "capacity" if benched else "content"
        raise ChainExhausted(
            f"No model could complete the request ({cause}).\n"
            f"  chain    : {len(live)} live, {len(self._dead)} dropped\n"
            + "\n".join(state) + "\n"
            f"  next reset: {fmt_duration(next_quota_reset() - now)}\n"
            f"  wait mode : {'on' if self.cfg.wait_for_quota else 'off'}"
            f" (max {fmt_duration(self.cfg.max_wait)})\n"
            f"  last err : {last_err}\n"
            f"Progress is checkpointed - rerun the same command to resume.",
            cause,
        )


# --------------------------------------------------------------------------
# EPUB reading
# --------------------------------------------------------------------------

NS = {
    "container": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
}


class Epub:
    """Reads an EPUB, exposes spine documents in reading order, rewrites in place."""

    def __init__(self, path: Path, workdir: Path):
        self.src = path
        self.root = workdir / "epub"
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)
        with zipfile.ZipFile(path) as zf:
            zf.extractall(self.root)
        self.opf_path = self._find_opf()
        self.opf_dir = self.opf_path.parent
        self.spine = self._read_spine()

    def _find_opf(self) -> Path:
        container = self.root / "META-INF" / "container.xml"
        tree = ET.parse(container)
        rootfile = tree.getroot().find(".//{*}rootfile")
        if rootfile is None or not rootfile.get("full-path"):
            raise RuntimeError("container.xml has no rootfile; is this a valid EPUB?")
        return self.root / rootfile.get("full-path")

    def _read_spine(self) -> list[Path]:
        root = ET.parse(self.opf_path).getroot()
        manifest = {
            item.get("id"): item.get("href")
            for item in root.findall(".//{*}item")
            if item.get("id") and item.get("href")
        }
        out: list[Path] = []
        for ref in root.findall(".//{*}itemref"):
            href = manifest.get(ref.get("idref", ""))
            if not href:
                continue
            p = (self.opf_dir / unquote(href)).resolve()
            if p.exists() and p.suffix.lower() in (".html", ".xhtml", ".htm"):
                out.append(p)
        return out

    def repack(self, dest: Path) -> None:
        """Write EPUB with mimetype stored first and uncompressed (spec requirement)."""
        if dest.exists():
            dest.unlink()
        with zipfile.ZipFile(dest, "w") as zf:
            mt = self.root / "mimetype"
            if mt.exists():
                zf.write(mt, "mimetype", compress_type=zipfile.ZIP_STORED)
            for f in sorted(self.root.rglob("*")):
                if f.is_file() and f.name != "mimetype":
                    zf.write(f, f.relative_to(self.root),
                             compress_type=zipfile.ZIP_DEFLATED)


# --------------------------------------------------------------------------
# Block extraction
# --------------------------------------------------------------------------

@dataclasses.dataclass
class Block:
    """One translatable unit: a paragraph or heading, as inner HTML."""
    doc_index: int
    block_index: int
    tag: str
    css_class: str
    html: str           # inner HTML (markup preserved)
    text: str           # plain text, for word counting only

    @property
    def key(self) -> str:
        return f"{self.doc_index:04d}:{self.block_index:04d}"


def is_leaf_block(tag: Tag, cfg: Config) -> bool:
    """A block we translate: right tag name, no nested block descendants."""
    if tag.name not in cfg.block_tags:
        return False
    return not tag.find(cfg.block_tags)


NUMERIC_RE = re.compile(r"^[\s\d.,;:£$€₹%\-–—()/]+$")


INLINE_TAGS = {"i", "em", "b", "strong", "span", "a", "sup", "sub", "small",
               "u", "code", "cite", "q", "abbr", "br", "img", "font", "s"}

PARA_SPLIT_RE = re.compile(r"\n[ \t\r\f\v]*\n+")


def needs_normalising(soup: BeautifulSoup) -> int:
    """Words of prose sitting outside any block element.

    Some conversions drop paragraph markup entirely and leave the text as bare
    nodes under <body>, separated by blank lines. There is then nothing for a
    block-based extractor to find, and the book looks empty.
    """
    body = soup.find("body")
    if not body:
        return 0
    loose = 0
    for node in body.children:
        if isinstance(node, NavigableString):
            loose += len(str(node).split())
    return loose


def normalise_document(soup: BeautifulSoup) -> int:
    """Wrap unparented text in real <p> elements. Returns paragraphs created.

    Walks <body>'s direct children in order, splitting bare text on blank
    lines and keeping interleaved inline tags attached to the paragraph they
    belong to, so emphasis is not orphaned.
    """
    body = soup.find("body")
    if not body:
        return 0

    children = list(body.contents)
    rebuilt: list[Any] = []
    current: list[Any] = []
    made = 0

    def flush() -> None:
        nonlocal made
        if not current:
            return
        if any((isinstance(n, NavigableString) and str(n).strip())
               or getattr(n, "name", None) in ("img", "br")
               or (hasattr(n, "get_text") and n.get_text(strip=True))
               for n in current):
            p = soup.new_tag("p")
            for n in current:
                p.append(n)
            rebuilt.append(p)
            made += 1
        current.clear()

    for node in children:
        name = getattr(node, "name", None)
        if isinstance(node, NavigableString):
            parts = PARA_SPLIT_RE.split(str(node))
            for i, part in enumerate(parts):
                if i:
                    flush()
                if part.strip():
                    current.append(NavigableString(part.strip()))
        elif name in INLINE_TAGS:
            current.append(node.extract())
        else:
            flush()                       # a real block ends the paragraph
            rebuilt.append(node.extract())
    flush()

    body.clear()
    for node in rebuilt:
        body.append(node)
    return made


def load_document(path: Path) -> tuple[BeautifulSoup, int]:
    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"),
                         "html.parser")
    made = 0
    if needs_normalising(soup) > 30:
        made = normalise_document(soup)
    return soup, made


def extract_blocks(soup: BeautifulSoup, doc_index: int, cfg: Config) -> list[Block]:
    blocks: list[Block] = []
    skip = set(cfg.skip_classes)
    for i, tag in enumerate(soup.find_all(cfg.block_tags)):
        if not is_leaf_block(tag, cfg):
            continue
        classes = set(tag.get("class", []))
        if classes & skip:
            continue                      # index entries, page refs, nav
        inner = tag.decode_contents()
        text = tag.get_text(" ", strip=True)
        if not text or len([c for c in text if c.isalpha()]) < 2:
            continue                      # empty / anchor-only / numeral-only
        if NUMERIC_RE.match(text):
            continue                      # numeric table cells pass through
        if tag.name in ("td", "th") and len(text.split()) < 2:
            continue                      # single-token cells: units, symbols
        blocks.append(Block(
            doc_index=doc_index,
            block_index=i,
            tag=tag.name,
            css_class=" ".join(tag.get("class", [])),
            html=inner,
            text=text,
        ))
    return blocks


def diagnose_extraction(soups: dict[int, Any], cfg: Config) -> str:
    """Explain why no blocks were found, rather than leaving it a mystery."""
    from collections import Counter
    tags: Counter = Counter()
    texty: Counter = Counter()
    nonleaf = empty = classfiltered = 0
    skip = set(cfg.skip_classes)
    for soup in soups.values():
        for t in soup.find_all(True):
            tags[t.name] += 1
            if t.name in cfg.block_tags:
                if t.find(cfg.block_tags):
                    nonleaf += 1
                elif set(t.get("class", [])) & skip:
                    classfiltered += 1
                elif not t.get_text(strip=True):
                    empty += 1
            elif t.get_text(strip=True) and not t.find(True):
                texty[t.name] += 1

    lines = ["", "  Why nothing was extracted:"]
    lines.append(f"    tags present   : "
                 + ", ".join(f"{n}x{c}" for n, c in tags.most_common(10)))
    lines.append(f"    block tags seen: "
                 + (", ".join(f"{n}x{tags[n]}" for n in cfg.block_tags if tags[n])
                    or "NONE - this file uses different markup"))
    lines.append(f"    rejected       : {nonleaf} non-leaf, {empty} empty, "
                 f"{classfiltered} filtered by CSS class")
    if texty:
        lines.append(f"    text found in  : "
                     + ", ".join(f"<{n}> x{c}" for n, c in texty.most_common(6))
                     + "  <- add these to block_tags")
    body_text = sum(len(s.get_text(strip=True)) for s in soups.values())
    lines.append(f"    total characters in documents: {body_text:,}")
    if body_text and not any(tags[n] for n in cfg.block_tags):
        lines.append("    The text exists but is not inside any recognised "
                     "block element.")
    return "\n".join(lines)


def batch_blocks(blocks: list[Block], max_words: int) -> Iterator[list[Block]]:
    """Group consecutive blocks into request-sized batches."""
    cur: list[Block] = []
    words = 0
    for b in blocks:
        w = len(b.text.split())
        if cur and words + w > max_words:
            yield cur
            cur, words = [], 0
        cur.append(b)
        words += w
    if cur:
        yield cur


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------


# Register presets. The single biggest lever on whether the result reads like
# a book someone would pick up, or like a school translation exercise.
REGISTER_PRESETS = {
    "modern": """BINDING REGISTER - this overrides any judgement you would \
otherwise make.

Write the everyday standard Hindi actually spoken and read across north India \
today: the Hindustani of newspapers, television and ordinary conversation in \
Delhi and the Hindi belt. The reader is an adult who reads Hindi fluently and \
has no Sanskrit training.

Where a common everyday word and a Sanskritised tatsam word both exist, use \
the everyday one:
  इस्तेमाल not प्रयोग, ज़रूरत not आवश्यकता, कोशिश not प्रयास,
  मुश्किल not कठिन, मदद not सहायता, उम्मीद not आशा,
  वजह not कारण, शुरू not आरंभ, ख़त्म not समाप्त,
  हिस्सा not भाग, ख़बर not समाचार, सवाल not प्रश्न,
  जवाब not उत्तर, ज़िंदगी not जीवन, दुनिया not संसार.

Avoid the formal written connectives - अतः, किंतु, परंतु, तथा, एवं, यद्यपि, \
तत्पश्चात - and use the spoken ones: लेकिन, मगर, और, तो, इसलिए, फिर भी, \
उसके बाद.

Urdu-derived vocabulary is ordinary Hindi and should be used freely wherever \
it is what people actually say. Do not reach for a Sanskrit equivalent to \
sound more literary.

This does NOT mean flattening the prose. Keep the author's rhythm, irony and \
force. Narration may be elegant; it must not be archaic or bookish. Educated \
characters can speak with precision, rustic ones with earthiness - the \
register sets the floor and ceiling of the vocabulary, not the range of \
voices within it.""",

    "literary": """BINDING REGISTER - this overrides any judgement you would \
otherwise make.

Write literary Hindi in the tradition of published Hindi fiction: a \
Sanskritised but readable register, closer to the written standard than to \
speech. Prefer तत्सम vocabulary where it is current in literary prose, and \
allow formal connectives. Keep it readable rather than archaic.""",
}


BIBLE_SYSTEM = """You are a senior literary translator preparing to translate a \
novel from {src} into {tgt}. You produce a translation bible: the binding \
decisions that keep a long translation internally consistent.

Return ONLY valid JSON. No markdown fences, no commentary."""

BIBLE_PROMPT = """Below is a representative sample of a novel.

Produce a translation bible as JSON with exactly these keys:

"register": How the {tgt} prose should sound. Choose a point on the \
Sanskritised-शुद्ध-हिंदी to colloquial-Hindustani spectrum and justify it in one \
sentence. This choice is binding for the whole book.

"narrative_voice": Person, tense, and the texture of the narration \
(wry, clinical, breathless, elegiac...). Name the {tgt} verb-ending style \
that matches it.

"characters": array of objects with keys "name" (source), "name_{tgt}" \
(fixed transliteration in the TARGET SCRIPT ONLY - no Latin letters, no \
bracketed original spelling - decided ONCE and never varied), "role", and \
"notes" on how they speak.

"formality_matrix": array of objects with "speakers" (e.g. "Theo -> Alicia"), \
"form" (one of आप / तुम / तू), "why", and — where it applies — "changes" \
(an array of objects with "when" describing the point of change in the \
narrative, "form", and "why"). Cover every pair that speaks, in BOTH \
directions, since asymmetry is often the whole point of a relationship. \
Every character you list above must appear here.

This is the single most important section: {tgt} forces a formality choice \
that {src} does not make, and drift here silently rewrites relationships.

Relationships also MOVE. A character who begins as an intimate may become a \
social superior, terror may become affection, a child may grow up. Where the \
address form should shift, you must record it in "changes" rather than \
picking one value for the whole book — a single form applied throughout will \
either destroy the opening or destroy the ending. State the narrative event \
that triggers each shift, not a chapter number.

"invented_names": array of objects for names the author coined - people, \
peoples, species, places, objects, titles. Each has:
  "name"            the source spelling
  "meaning_visible" true only if a reader of the ORIGINAL perceives ordinary \
                    words in it. Treebeard yes; Frodo no. Judge by what a \
                    present-day reader perceives, NOT by etymology: a name \
                    that means something in an older form of the language but \
                    reads as a bare sound today is meaning_visible false.
  "{tgt}"           the {tgt} form. Transliterate the name itself, ALWAYS, \
                    including when the meaning is visible. Names stay names.
  "gloss"           TWO OR THREE WORDS of {tgt} giving the image the source \
                    name carries, or null when meaning_visible is false. This \
                    is shown once, at first mention, in brackets after the \
                    name - never again. Write it in the same everyday register \
                    as the rest of the translation: what a person would \
                    actually say, not literary phrasing. Never a definition, \
                    never a sentence.
  "alias_of"        when this names something already listed under another \
                    name, the other name; otherwise null. A reader who meets \
                    the same character under two names has no way to connect \
                    them unless this is recorded.
  "plural"          for peoples and species, the {tgt} plural and oblique \
                    form, decided once so it does not drift.

Category words inside a coined compound are translated while the coined part \
is transliterated: an invented tree named X becomes "X" plus the {tgt} word \
for tree, not a wholly translated phrase.

"glossary": array of objects with "term" (recurring motif word, institution, \
object, or repeated phrase), "hindi" (its fixed rendering), and "why". \
Include anything whose repetition carries meaning.

"pitfalls": array of strings — specific traps in translating THIS text.

SAMPLE:
---
{sample}
---"""


TRANSLATE_SYSTEM = """You are a literary translator rendering a novel from {src} \
into {tgt}. You are translating for an adult reader of literary fiction whose \
first language is {tgt}. Your output must read as though the novel had been \
written in {tgt}, not as though it had been converted into it.

ABSOLUTE RULES

1. OUTPUT FORMAT. The input is a numbered list of blocks, each marked
   [[N]] on its own line. Return exactly the same numbers, in the same
   order, each marked [[N]] on its own line, followed by the translation.
   Never merge, split, drop, reorder, or add blocks. If a block is a
   single word, translate that single word as its own block.

2. INLINE MARKUP. Blocks may contain HTML tags (<span>, <i>, <em>, <a>,
   <b>). Every tag in the input must appear in the output, with all its
   attributes byte-identical. Tags mark emphasis, so wrap the {tgt} word
   that carries the emphasis — which is usually NOT in the same position,
   because {tgt} word order differs. Move the tag; never strip it, never
   leave it stranded on a particle.

3. THE BIBLE IS BINDING. Formality (आप/तुम/तू), name spellings, and
   glossary renderings are fixed decisions. Follow them even where another
   choice would read better in isolation. Consistency across 300 pages
   outranks local elegance.

4. TRANSLATE MEANING, NOT WORDS. Idioms become {tgt} idioms. Rhythm,
   irony, understatement, and menace must survive. Where a sentence's
   effect depends on withholding information, withhold it in {tgt} too —
   do not clarify, expand, or explain. Never add a word the author did
   not imply.

5. NO COMMENTARY. No notes, no alternatives, no apologies. Only the
   numbered translation."""

TRANSLATE_PROMPT = """TRANSLATION BIBLE:
{bible}

STORY SO FAR (for continuity; do not translate):
{summary}

IMMEDIATELY PRECEDING TEXT (for tone and pronoun continuity; do not translate):
{src_lang} : {prev_src}
{tgt_lang} : {prev_tgt}

POSITION IN THE BOOK: {position}
Where the bible records that a relationship or address form CHANGES, apply the
form appropriate to this point in the narrative, not the opening default.

Now translate these {n} blocks into {tgt_lang}.
{structure_note}

BLOCKS:
{blocks}"""


EDITOR_SYSTEM = """You are a {tgt} editor revising a translated novel. You see \
the {src} source and a {tgt} draft. You are not re-translating; you are making \
the draft read like {tgt} literary prose while staying faithful.

Fix, in priority order:
  1. Meaning errors, omissions, additions.
  2. Formality (आप/तुम/तू) or name spellings that contradict the bible.
  3. Sentences that read as translated {src} — calques, English word order,
     participle stacks {tgt} would not use.
  4. Rhythm. Long sentences that should breathe; short ones that should land.

Preserve every HTML tag exactly. Preserve block numbering exactly.
Return ONLY the numbered revised blocks. If a block needs no change,
return it unchanged. No commentary."""

EDITOR_PROMPT = """BIBLE (binding):
{bible}

{src_lang} SOURCE:
{source}

{tgt_lang} DRAFT:
{draft}

Return the revised {tgt_lang} blocks, same numbering."""


SUMMARY_SYSTEM = """You maintain a running plot summary for a translator working \
chapter by chapter. Be terse and factual. Track: what happened, who knows what, \
unresolved threads, and shifts in any relationship. Under 200 words."""


# --- nonfiction / expository ------------------------------------------------

NF_BIBLE_SYSTEM = """You are a senior translator preparing to render a work of \
expository non-fiction from {src} into {tgt}. You produce a translation bible: \
the binding decisions that keep a long argument internally coherent.

Return ONLY valid JSON. No markdown fences, no commentary."""

NF_BIBLE_PROMPT = """Below is a representative sample of a work of non-fiction.

Produce a translation bible as JSON with exactly these keys:

"register": How the {tgt} prose should sound. This is expository argument, not \
narrative — choose a level of formality and say whether to use Sanskritised \
academic {tgt} or plainer standard {tgt}. Justify in one sentence. Binding.

"terminology": array of objects with "term" ({src} technical term), "hindi" \
(its fixed rendering), and "why". THIS IS THE MOST IMPORTANT SECTION. In an \
argumentative work the technical vocabulary IS the argument: if a key term is \
rendered three different ways across three chapters, the reasoning becomes \
untraceable. Prefer established {tgt} vocabulary in this subject area over \
invented coinages or bare transliteration. Include every term the author uses \
in a precise or specialised sense.

"archaism_scan": An object with "period" (your best estimate of when the text \
was written) and "checked" (array of at least 12 ordinary-looking {src} words \
that ACTUALLY APPEAR in the sample and that you deliberately examined for \
sense-drift). List what you examined even when the word turned out to be \
unchanged. This section is the audit trail for the next one.

"archaisms": array of objects with "term", "period_meaning", "modern_trap", \
and "hindi" (the rendering that carries the PERIOD meaning). THIS IS THE \
HIGHEST-VALUE SECTION AND THE EASIEST TO GET WRONG BY OMISSION.

Older texts use ordinary words in senses that have since shifted. Such a word \
does not look like a technical term — it looks like a word you already know. \
Translated by its MODERN meaning it produces {tgt} that is fluent, confident, \
and wrong, with nothing visibly broken to signal the error. This is the worst \
failure mode available to you.

Work through your "checked" list one word at a time and ask: did this word \
mean something different when the author wrote it? Pay particular attention to \
words naming occupations, social ranks, materials, mental states, and \
institutions — these drift most. Also check any word whose ordinary modern \
sense would make the sentence slightly odd but not impossible; that mild \
oddness is usually the symptom of a shifted sense.

An empty "archaisms" array is a strong empirical claim: that not one word in \
this text has changed meaning. For a text written before 1900 that claim is \
almost always false, and returning an empty array is far more costly than \
including a term that turns out to be unremarkable. If in doubt, include it.

Do NOT put a shifted-sense word only in "terminology" or only in "pitfalls". \
It must appear HERE, because only this section binds the translator to the \
period meaning.

"sentence_policy": Guidance on handling the author's sentence construction. \
If the source uses long periodic sentences with stacked subordinate clauses, \
say explicitly that these SHOULD be split in {tgt}, and on what principle — \
{tgt} cannot carry that depth of subordination and attempting it produces \
unreadable prose. State what must be preserved when splitting: the logical \
connectives, and the order in which claims arrive.

"units_and_currency": How to handle period currency, weights, and measures. \
Default to keeping the original unit and NOT converting.

"pitfalls": array of strings — specific traps in translating THIS text.

SAMPLE:
---
{sample}
---"""

NF_TRANSLATE_SYSTEM = """You are translating a work of expository non-fiction \
from {src} into {tgt}, for an educated adult reader of {tgt} who is not a \
specialist. Your output must be an argument a {tgt} reader can follow and \
reason about — not a word-level conversion of {src} sentences.

ABSOLUTE RULES

1. OUTPUT FORMAT. The input is a numbered list of blocks, each marked
   [[N]] on its own line. Return the same numbers, in the same order, each
   marked [[N]] on its own line, followed by the translation. Never merge,
   split, drop, reorder, or add BLOCKS. (You may split sentences WITHIN a
   block — see rule 4.)

2. INLINE MARKUP. Blocks may contain HTML tags (<em>, <i>, <span>, <a>,
   <sup>, <b>). Every tag in the input must appear in the output with its
   attributes byte-identical. <sup> tags are footnote markers: carry them
   across untouched and never translate their contents. Emphasis tags must
   wrap the {tgt} word that carries the emphasis, which is usually NOT in
   the same position because {tgt} word order differs.

3. TERMINOLOGY IS BINDING. Every term in the bible's "terminology" list
   uses its fixed rendering, every time, even where variety would read
   better. Every term in "archaisms" is translated by its PERIOD meaning,
   never its modern one. This outranks style.

4. SPLIT LONG SENTENCES. The source may build sentences of great length
   through stacked subordinate clauses. {tgt} cannot sustain that
   subordination depth. Break them into shorter {tgt} sentences. When you
   do, you MUST preserve: (a) every logical connective — because,
   therefore, although, unless, whereas — made explicit in {tgt}; (b) the
   ORDER in which claims arrive, since the author's order is the
   reasoning; (c) exactly what is asserted versus what is conceded or
   supposed. Never let a split turn a qualified claim into a flat one.

5. ARGUE, DON'T DECORATE. Preserve hedges, quantifiers, and scope
   ("some", "in general", "rarely", "almost always") exactly — they carry
   the author's precision. Never strengthen a hedged claim, never add an
   example, never insert an explanation the author did not give, never
   omit a step in the reasoning because it seems obvious.

6. NO COMMENTARY. Only the numbered translation."""

NF_SUMMARY_SYSTEM = """You maintain a running ARGUMENT THREAD for a translator \
working through a work of non-fiction section by section. Track: what has been \
established so far, what is currently being argued, which technical terms have \
been introduced and defined, and what the author has promised to address later. \
Be terse and factual. Under 200 words."""

NF_SUMMARY_PROMPT = """Existing argument thread:
{prev}

New section (translated):
{chapter}

Return the updated argument thread. Under 200 words. No preamble."""

SUMMARY_PROMPT = """Existing summary:
{prev}

New chapter (translated):
{chapter}

Return the updated summary. Under 200 words. No preamble."""


# --------------------------------------------------------------------------
# Numbered-block protocol
# --------------------------------------------------------------------------

BLOCK_RE = re.compile(r"^\s*\[\[(\d+)\]\]\s*$", re.MULTILINE)


def render_blocks(blocks: list[Block], prepared: list[str] | None = None) -> str:
    src = prepared if prepared is not None else [b.html for b in blocks]
    return "\n".join(f"[[{i + 1}]]\n{h}" for i, h in enumerate(src))


def parse_blocks(response: str, expected: int) -> list[str] | None:
    """Parse [[N]]-delimited output. Returns None if the count doesn't match."""
    marks = list(BLOCK_RE.finditer(response))
    if len(marks) != expected:
        return None
    out: list[str] = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(response)
        out.append(response[m.end():end].strip())
    if any(n != str(i + 1) for i, n in enumerate(m.group(1) for m in marks)):
        return None
    return out


def set_inner_html(tag: Tag, html: str) -> None:
    """Replace a tag's children with parsed HTML.

    NOTE: Tag.append() detaches the node from its current parent, which mutates
    the .contents list it came from. Iterating .contents directly therefore
    skips every second node and silently drops inline markup. Snapshot first.
    """
    nodes = list(BeautifulSoup(html, "html.parser").contents)
    tag.clear()
    for node in nodes:
        tag.append(node.extract() if node.parent else node)


TAG_RE = re.compile(r"<[^>]+>")

# Tags that carry meaning and MUST survive translation.
SEMANTIC_TAGS = {"em", "i", "strong", "b", "sup", "sub", "cite", "q",
                 "abbr", "code", "mark", "dfn", "br", "img"}

# Tags that exist only for typography in the source script. Case-based
# devices — drop caps, small caps, spaced capitals — have NO Devanagari
# equivalent, because the script has no letter case. Demanding the model
# preserve them asks for something that cannot exist, so we unwrap them
# before translation instead of treating their loss as an error.
COSMETIC_TAGS = {"small", "big", "font", "u"}
COSMETIC_CLASS_RE = re.compile(r"(sc|smallcap|dropcap|caps|initial)", re.I)


def is_anchor_only(el: Tag) -> bool:
    """A zero-width structural marker: page target, nav hook, index point."""
    if el.name not in ("a", "span", "div"):
        return False
    if el.get_text(strip=True):
        return False
    if el.find(["img", "br"]):
        return False
    return not el.get("href")      # real links are semantic; bare ids are not


def is_cosmetic(el: Tag) -> bool:
    """Typographic-only markup with no meaning in the target script."""
    if el.name in COSMETIC_TAGS:
        return True
    if el.name == "span":
        return bool(COSMETIC_CLASS_RE.search(" ".join(el.get("class", []))))
    return False


def prepare_html(inner: str) -> tuple[str, list[tuple[float, str]]]:
    """Strip what the model must not see.

    Returns cleaned inner HTML plus extracted zero-width anchors, each with
    the fractional position in the block's text where it occurred, so it can
    be put back after translation.
    """
    soup = BeautifulSoup(inner, "html.parser")
    total = max(len(soup.get_text()), 1)

    anchors: list[tuple[float, str]] = []
    for el in list(soup.find_all(True)):
        if not is_anchor_only(el):
            continue
        offset = 0
        for node in soup.descendants:
            if node is el:
                break
            if isinstance(node, NavigableString):
                offset += len(node)
        anchors.append((offset / total, str(el)))
        el.extract()

    for el in list(soup.find_all(True)):
        if is_cosmetic(el):
            el.unwrap()            # keep the letters, drop the case styling

    return soup.decode(), anchors


def reinsert_anchors(html: str, anchors: list[tuple[float, str]]) -> str:
    """Put zero-width anchors back at the equivalent position in the output."""
    if not anchors:
        return html
    try:
        soup = BeautifulSoup(html, "html.parser")
        total = max(len(soup.get_text()), 1)
        for frac, tag_html in sorted(anchors, reverse=True):
            target = int(frac * total)
            anchor = BeautifulSoup(tag_html, "html.parser").contents[0]
            seen = 0
            for node in list(soup.descendants):
                if not isinstance(node, NavigableString):
                    continue
                s = str(node)
                if seen + len(s) < target:
                    seen += len(s)
                    continue
                off = max(0, min(len(s), target - seen))
                sp = s.rfind(" ", 0, off)          # snap to a word boundary
                if sp != -1:
                    off = sp + 1
                rest = NavigableString(s[off:])
                node.replace_with(rest)
                rest.insert_before(NavigableString(s[:off]))
                rest.insert_before(anchor)
                break
            else:
                soup.insert(0, anchor)             # empty output: park at start
        return soup.decode()
    except Exception:                              # never lose text to this
        return "".join(t for _, t in anchors) + html


def tag_signature(html: str, semantic_only: bool = False) -> list[str]:
    tags = TAG_RE.findall(html)
    if not semantic_only:
        return sorted(tags)
    keep = []
    for t in tags:
        name = re.match(r"</?\s*([a-zA-Z0-9]+)", t)
        if name and name.group(1).lower() in SEMANTIC_TAGS:
            keep.append(t)
    return sorted(keep)


DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")


def output_problem(src_html: str, out_html: str) -> str | None:
    """Why an output is unusable, or None if it looks like a translation.

    An empty string passes every structural check - the block count matches
    and the tag signature (nothing) matches - and then reassembly silently
    leaves the source text in place. So it has to be caught here.
    """
    out_txt = re.sub(TAG_RE, "", out_html).strip()
    src_txt = re.sub(TAG_RE, "", src_html).strip()
    if not out_txt:
        return "empty"
    if not src_txt:
        return None

    # Headings, numerals and short labels are often identical in translation
    # ("II", "PART ONE", a bare name). Treating identity as failure there
    # costs a full retry-and-split cascade on output that was correct.
    letters = re.findall(r"[A-Za-z]", src_txt)
    if len(src_txt) < 20 or len(letters) < 6:
        return None
    if re.fullmatch(r"[IVXLCDM\d\s.,:;()\[\]\-\u2013\u2014]+", src_txt):
        return None
    if len(src_txt.split()) <= 3 and not re.search(r"[a-z]", src_txt):
        return None

    norm = lambda t: re.sub(r"\W+", " ", t.lower()).strip()
    if norm(out_txt) == norm(src_txt):
        return "echoed the source"
    if len(re.findall(r"[A-Za-z]", src_txt)) >= 3 and not DEVANAGARI_RE.search(out_txt):
        return "no target script"
    return None


def check_tags(src: str, out: str) -> bool:
    """True if every MEANING-BEARING tag survived.

    Cosmetic markup is deliberately excluded: it is unwrapped before the
    model ever sees it, and enforcing it would fail on markup that has no
    counterpart in the target script.
    """
    return tag_signature(src, True) == tag_signature(out, True)


def structure_hint(blocks: list[Block]) -> str:
    """Tell the model what these blocks structurally are."""
    hints = []
    for i, b in enumerate(blocks):
        cls = b.css_class
        if b.tag.startswith("h") or "cn" in cls.split():
            hints.append(f"Block {i+1} is a chapter heading.")
        elif "stni" in cls.split():
            hints.append(f"Block {i+1} follows a scene break.")
    return " ".join(hints) if hints else "All blocks are body paragraphs."


# --------------------------------------------------------------------------
# Checkpointing
# --------------------------------------------------------------------------

class Store:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = {"blocks": {}, "bible": None, "summary": ""}
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(self.path)

    def write_bible(self, raw: str) -> None:
        """Write the bible as its own pretty-printed file.

        The checkpoint holds the entire translated text and gets large; the
        bible is a few KB and is the file worth reading or sharing when
        diagnosing a run.
        """
        out = self.path.parent / "bible.json"
        try:
            parsed = json.loads(re.sub(r"^```(?:json)?|```$", "", raw.strip(),
                                       flags=re.M))
            out.write_text(json.dumps(parsed, ensure_ascii=False, indent=2),
                           encoding="utf-8")
        except Exception:
            out.write_text(raw, encoding="utf-8")   # unparseable: keep as-is

    def write_chapter_records(self, records: list[dict[str, Any]]) -> None:
        """Kept separate from bible.json, which stays the hand-editable base.

        Merging them in would change the bible hash and discard every
        translated block mid-run.
        """
        (self.path.parent / "bible_chapters.json").write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")

    def write_report(self, report: dict[str, Any]) -> None:
        (self.path.parent / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    def done(self, key: str) -> str | None:
        return self.data["blocks"].get(key)

    def put(self, key: str, value: str) -> None:
        # An empty value is worse than a missing one: reassembly skips it and
        # silently leaves the source text, while the block looks "done".
        if value and value.strip():
            self.data["blocks"][key] = value


# --------------------------------------------------------------------------
# Devanagari typography
# --------------------------------------------------------------------------

HINDI_CSS = """
/* injected by booktrans */
body, p, div, span, h1, h2, h3, h4, h5, h6, li, td, th, blockquote {
  font-family: "Noto Serif Devanagari", "Nirmala UI", "Mangal", serif;
  line-height: 1.75;          /* Devanagari needs headroom for matras */
  text-align: justify;
  hyphens: none;              /* never hyphenate Devanagari */
  word-break: normal;
}
p { orphans: 2; widows: 2; }
"""


def inject_css(epub: Epub) -> None:
    css_files = list(epub.root.rglob("*.css"))
    if css_files:
        for c in css_files:
            with c.open("a", encoding="utf-8") as fh:
                fh.write("\n" + HINDI_CSS)
    else:
        (epub.opf_dir / "booktrans.css").write_text(HINDI_CSS, encoding="utf-8")


def set_language(epub: Epub, lang: str = "hi") -> None:
    txt = epub.opf_path.read_text(encoding="utf-8")
    txt = re.sub(r"<dc:language>[^<]*</dc:language>",
                 f"<dc:language>{lang}</dc:language>", txt)
    epub.opf_path.write_text(txt, encoding="utf-8")


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------

HEDGE_RE = re.compile(
    r"(might be|may be|could also|could be considered|assuming|for now|"
    r"if a precise|is lacking|not certain|uncertain|unclear|perhaps|possibly|"
    r"unsure|no exact|approximate|a direct transliteration)", re.I)


def parse_bible(raw: str) -> dict[str, Any] | None:
    text = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M)
    try:
        return json.loads(text)
    except Exception:
        pass
    # Truncated output: close what is open and keep whatever parsed cleanly.
    for cut in range(len(text), max(len(text) - 20000, 0), -1):
        if text[cut - 1] not in "}]\"0123456789truefalsnl":
            continue
        candidate = text[:cut].rstrip().rstrip(",")
        depth_c = candidate.count("{") - candidate.count("}")
        depth_s = candidate.count("[") - candidate.count("]")
        if depth_c < 0 or depth_s < 0:
            continue
        try:
            return json.loads(candidate + "]" * depth_s + "}" * depth_c)
        except Exception:
            continue
    return None


def validate_bible(raw: str, cfg: Config) -> list[str]:
    """Check the bible before spending calls translating against it.

    Every problem found here would otherwise surface as fluent, plausible,
    wrong output hundreds of blocks later.
    """
    warn: list[str] = []
    b = parse_bible(raw)
    if b is None:
        return ["bible could not be parsed at all; translation will proceed "
                "without it. Rerun with --rebuild-bible"]
    try:
        json.loads(re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M))
    except Exception:
        warn.append("bible JSON was TRUNCATED (output token limit) and has been "
                    "salvaged; sections near the end may be missing. Rerun with "
                    "--rebuild-bible now that the limit is higher")

    required = (["register", "terminology", "archaisms", "sentence_policy"]
                if cfg.profile == "nonfiction"
                else ["register", "characters", "formality_matrix", "glossary"])
    for k in required:
        if k not in b:
            warn.append(f"bible is missing '{k}'")

    if cfg.profile == "nonfiction":
        arch = b.get("archaisms") or []
        scan = (b.get("archaism_scan") or {}).get("checked") or []
        if not arch:
            warn.append(
                "archaisms is EMPTY - the field that binds period meanings. "
                f"{len(scan)} word(s) were scanned. If this text predates 1900 "
                "this is almost certainly a miss; rerun with --rebuild-bible "
                "or add entries to bible.json by hand")
        elif len(scan) < 8:
            warn.append(f"archaism scan covered only {len(scan)} word(s); "
                        "coverage may be shallow")

    if cfg.profile == "fiction":
        chars = b.get("characters") or []
        matrix = b.get("formality_matrix") or []
        TITLES = {"mr", "mrs", "miss", "ms", "dr", "the", "a", "an", "sir"}
        def tokens(name: str) -> set[str]:
            raw = re.sub(r"[()]", " ", name).lower().split()
            return {w.strip(".,") for w in raw} - TITLES

        matrix_text = " ".join(str(m.get("speakers", "")) for m in matrix).lower()
        missing = set()
        for c in chars:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            toks = tokens(str(c["name"]))
            if toks and not any(t in matrix_text for t in toks):
                missing.add(str(c["name"]))
        if missing:
            warn.append(f"characters with no formality entry: {sorted(missing)} "
                        "- their address form will drift")
        # Both directions of a pair must be stated; asymmetry is often the point.
        pairs = {tuple(x.strip() for x in str(m.get("speakers", "")).split("->"))
                 for m in matrix if "->" in str(m.get("speakers", ""))}
        one_way = [f"{a} -> {b_}" for a, b_ in pairs
                   if (b_, a) not in pairs and a and b_]
        if one_way:
            warn.append(f"one-directional formality pairs: {sorted(one_way)[:4]} "
                        "- the reverse direction is unconstrained")

    terms = b.get("terminology") or b.get("glossary") or []
    if isinstance(terms, list):
        by_term: dict[str, set[str]] = {}
        by_hindi: dict[str, set[str]] = {}
        for e in terms:
            if not isinstance(e, dict):
                continue
            t = str(e.get("term", "")).strip().lower()
            h = str(e.get("hindi", "")).strip()
            if not t or not h:
                continue
            by_term.setdefault(t, set()).add(h)
            by_hindi.setdefault(h, set()).add(t)
            if HEDGE_RE.search(str(e.get("why", ""))):
                warn.append(f"low-confidence rendering: '{t}' -> '{h}' "
                            "(the bible hedges; check this one)")
        for t, hs in by_term.items():
            if len(hs) > 1:
                warn.append(f"CONFLICT: '{t}' has {len(hs)} renderings {sorted(hs)} "
                            "- consistency will break")
        for h, ts in by_hindi.items():
            if len(ts) > 2:
                warn.append(f"'{h}' is reused for {sorted(ts)} - distinctions "
                            "between these terms will be lost")
    return warn
def sample_blocks(blocks: list[Block], budget: int = 28000,
                  windows: int = 14) -> tuple[str, float]:
    """Take a text sample spread across the WHOLE selection.

    Slicing head/middle/tail and then truncating the join does not work: on a
    long book the head slice alone exceeds the budget, so the later material
    is silently discarded and the bible only ever sees the opening. Instead,
    walk `windows` evenly spaced points and take a contiguous run at each,
    giving every window an equal share of the budget.

    Returns the sample and the fraction of the book it spans.
    """
    if not blocks:
        return "", 0.0
    per = max(budget // windows, 500)
    n = len(blocks)
    parts: list[str] = []
    touched: list[int] = []
    for w in range(windows):
        start = min(int(w * n / windows), n - 1)
        used = 0
        i = start
        while i < n and used < per:
            t = blocks[i].text
            parts.append(t)
            used += len(t) + 2
            touched.append(i)
            i += 1
    span = (max(touched) - min(touched) + 1) / n if touched else 0.0
    return "\n\n".join(parts)[:budget], span


SENTENCE_STARTERS = {
    "the", "he", "she", "it", "they", "we", "you", "i", "but", "and", "then",
    "when", "what", "there", "this", "that", "as", "at", "in", "on", "so",
    "for", "if", "no", "not", "now", "yes", "why", "how", "his", "her", "my",
    "our", "their", "was", "were", "had", "have", "one", "all", "after",
    "before", "with", "while", "among", "upon", "such", "which", "who",
    "chapter", "mr", "mrs", "miss", "sir", "lady", "god", "lord", "london",
    "well", "oh", "ah", "yet", "still", "here", "very", "much", "some", "any",
    "england", "english", "french", "christmas", "sunday", "monday",
}


def frequent_names(sample: str, min_count: int = 6) -> list[tuple[str, int]]:
    """Proper nouns that recur often enough to be characters.

    A token that only ever appears at the start of a sentence is probably an
    ordinary word that happens to be capitalised, so require at least one
    mid-sentence occurrence.
    """
    counts: dict[str, int] = {}
    midsentence: set[str] = set()
    for m in re.finditer(r"\b([A-Z][a-z]{2,})\b", sample):
        w = m.group(1)
        if w.lower() in SENTENCE_STARTERS:
            continue
        counts[w] = counts.get(w, 0) + 1
        before = sample[max(0, m.start() - 2):m.start()]
        if before and not re.search(r'[.!?"]\s*$|^\s*$', before):
            midsentence.add(w)
    hits = [(w, c) for w, c in counts.items()
            if c >= min_count and w in midsentence]
    return sorted(hits, key=lambda x: -x[1])


def check_cast(sample: str, raw_bible: str, top: int = 12) -> list[str]:
    """Warn about recurring names the bible never mentions."""
    b = parse_bible(raw_bible)
    if not b:
        return []
    # Only the character list and formality matrix count as "known". A name
    # mentioned in passing inside another character's description is not an
    # entry and binds nothing.
    known = " ".join(
        [str(c.get("name", "")) for c in (b.get("characters") or [])
         if isinstance(c, dict)]
        + [str(m.get("speakers", "")) for m in (b.get("formality_matrix") or [])
           if isinstance(m, dict)]
    ).lower()
    missing = [(w, c) for w, c in frequent_names(sample)[:top]
               if w.lower() not in known]
    if not missing:
        return []
    named = ", ".join(f"{w} ({c}x)" for w, c in missing[:6])
    return [f"recurring names not yet in the bible: {named} - "
            f"entries will be added automatically as they appear"]


REVIEW_SYSTEM = """You keep a translation bible current as a novel is \
translated from {src} into {tgt}, one chapter at a time.

For each new chapter you report two things: characters appearing for the first \
time, and address-form changes that take effect FROM THIS CHAPTER ONWARD.

You never rewrite the past. Earlier chapters are already translated, and a \
change you report now applies only from here forward — which is correct, \
because that is how relationships actually move in a story. Report a change \
only when the narrative has genuinely shifted the relationship, not because \
another form would read slightly better.

Return ONLY valid JSON. No markdown fences, no commentary."""

REVIEW_PROMPT = """CURRENT BIBLE STATE (what is binding as of this chapter):
{digest}

STORY SO FAR:
{summary}

THIS CHAPTER ({position}):
---
{excerpt}
---

Return JSON with exactly three keys:

"new_characters": array of objects with "name", "name_{tgt}" (a fixed \
transliteration in the TARGET SCRIPT ONLY - no Latin letters, no bracketed \
original spelling - decided now, never varied afterwards), "role", and \
"notes" on how they speak. Only people not already listed above. If a name is a place, \
an object, or an alias of an existing character, say so in "role" and give it \
no formality entry. Empty array if none.

"formality_changes": array of objects with "speakers" (e.g. "Pip -> Joe"), \
"form" (आप / तुम / तू), and "why". Include BOTH new pairs that speak for the \
first time here, and existing pairs whose form should change from this chapter \
on. For a change to an existing pair, "why" must name the event in the story \
that causes it. Cover both directions for new speaking pairs, since asymmetry \
is usually the point. Empty array if nothing new or changed.

"new_glossary": array of objects with "term", "hindi", "why" for recurring \
motifs, coinages, or objects introduced here that must be rendered \
consistently. Empty array if none.

"new_names": array of objects for author-coined names appearing here for the \
first time, with keys "name", "meaning_visible", "{tgt}", "gloss" (two or \
three everyday {tgt} words, or null when the meaning is not visible to a \
reader of the original), "alias_of" (another name for the same thing, or \
null) and "plural" (for peoples and species, or null). Transliterate the name \
itself even when its meaning is visible. Empty array if none."""


def bible_digest(base: str, records: list[dict[str, Any]], upto: int) -> str:
    """Compact statement of what is binding right now.

    Sending the whole bible each chapter is wasteful and buries the part that
    matters, so this lists characters and the CURRENT address form for each
    pair, with the base entries already overridden by later decisions.
    """
    b = parse_bible(base) or {}
    names: list[str] = []
    for c in (b.get("characters") or []):
        if isinstance(c, dict) and c.get("name"):
            names.append(f"{c['name']} = {c.get('name_Hindi') or c.get('name_हिंदी', '?')}")
    forms: dict[str, tuple[str, str]] = {}
    for m in (b.get("formality_matrix") or []):
        if isinstance(m, dict) and m.get("speakers"):
            forms[str(m["speakers"]).strip()] = (str(m.get("form", "")), "opening")
    for rec in sorted(records, key=lambda r: r.get("doc", 0)):
        if rec.get("doc", 0) > upto:
            continue
        for c in (rec.get("new_characters") or []):
            if isinstance(c, dict) and c.get("name"):
                names.append(f"{c['name']} = {c.get('name_Hindi', '?')}")
        for m in (rec.get("formality_changes") or []):
            if isinstance(m, dict) and m.get("speakers"):
                forms[str(m["speakers"]).strip()] = (
                    str(m.get("form", "")), f"since doc {rec.get('doc')}")
    lines = [f"register: {str(b.get('register', ''))[:300]}",
             "", "characters: " + ("; ".join(names) if names else "(none yet)"),
             "", "address forms in force:"]
    lines += [f"  {k}: {v[0]}  [{v[1]}]" for k, v in sorted(forms.items())] or ["  (none)"]

    # Shifts the bible predicted but that nobody has applied yet. Without
    # this the base "changes" array is inert text and the opening form runs
    # to the end of the book.
    pending = []
    for m in (b.get("formality_matrix") or []):
        if not isinstance(m, dict):
            continue
        key = str(m.get("speakers", "")).strip()
        if forms.get(key, ("", ""))[1] != "opening":
            continue                      # already overridden by a review
        for ch in (m.get("changes") or []):
            if isinstance(ch, dict) and ch.get("form"):
                pending.append(f"  {key}: expected to become {ch['form']} "
                               f"when {str(ch.get('when', '?'))[:90]}")
    if pending:
        lines += ["", "shifts the bible predicts but that are NOT yet applied.",
                  "If this chapter is where one happens, report it now:"] + pending
    return "\n".join(lines)


def compose_bible(base: str, records: list[dict[str, Any]],
                  upto: int = 10 ** 9) -> str:
    """The bible as it stands at a given document, for the translator.

    Later decisions override earlier ones, but only for documents at or after
    the point the change took effect, so nothing already translated is
    contradicted.
    """
    b = parse_bible(base)
    if b is None:
        return base
    b = json.loads(json.dumps(b))
    matrix = {str(m.get("speakers", "")).strip(): m
              for m in (b.get("formality_matrix") or []) if isinstance(m, dict)}
    for rec in sorted(records, key=lambda r: r.get("doc", 0)):
        if rec.get("doc", 0) > upto:
            continue
        for c in (rec.get("new_characters") or []):
            b.setdefault("characters", []).append(c)
        for g in (rec.get("new_glossary") or []):
            b.setdefault("glossary", []).append(g)
        for nm in (rec.get("new_names") or []):
            b.setdefault("invented_names", []).append(nm)
        for m in (rec.get("formality_changes") or []):
            if not isinstance(m, dict) or not m.get("speakers"):
                continue
            key = str(m["speakers"]).strip()
            entry = dict(m)
            entry["effective_from"] = f"document {rec.get('doc')}"
            matrix[key] = entry
    b["formality_matrix"] = list(matrix.values())
    b.pop("archaism_scan", None)
    return json.dumps(b, ensure_ascii=False, indent=1)


def review_chapter(api: Gemini, cfg: Config, base: str,
                   records: list[dict[str, Any]], doc_blocks: list[Block],
                   di: int, position: str, summary: str,
                   store: Store) -> dict[str, Any] | None:
    """Bring the bible up to date for this chapter before translating it."""
    if any(r.get("doc") == di for r in records):
        return None                                   # already reviewed

    text = "\n\n".join(b.text for b in doc_blocks)
    excerpt = text[:7000]
    if len(text) > 7000:                              # keep some later dialogue
        excerpt += "\n\n[...]\n\n" + text[-2000:]
    try:
        raw = api.ask(
            REVIEW_SYSTEM.format(src=cfg.source_lang, tgt=cfg.target_lang),
            REVIEW_PROMPT.format(digest=bible_digest(base, records, di),
                                 summary=summary or "(start of book)",
                                 position=position, tgt=cfg.target_lang,
                                 excerpt=excerpt),
            temperature=0.3, json_mode=True)
    except Exception as exc:                          # noqa: BLE001
        api.warnings.append(f"doc {di}: chapter review failed - {str(exc)[:100]}")
        print(f"    ~ chapter review unavailable, using bible as-is")
        return None

    rec = parse_bible(raw)
    if not isinstance(rec, dict):
        api.warnings.append(f"doc {di}: chapter review returned unparseable JSON")
        return None
    rec["doc"] = di
    chars = [c for c in (rec.get("new_characters") or []) if isinstance(c, dict)]
    changes = [m for m in (rec.get("formality_changes") or []) if isinstance(m, dict)]
    gloss = [g for g in (rec.get("new_glossary") or []) if isinstance(g, dict)]
    if not (chars or changes or gloss):
        rec = {"doc": di, "new_characters": [], "formality_changes": [],
               "new_glossary": []}
    records.append(rec)
    store.data["bible_records"] = records
    store.save()
    store.write_chapter_records(records)

    for c in chars:
        print(f"    + character: {c.get('name')} = {c.get('name_Hindi', '?')}")
    for m in changes:
        print(f"    + {m.get('speakers')}: {m.get('form')}")
    for g in gloss:
        print(f"    + term: {g.get('term')} = {g.get('hindi')}")
    return rec


DETECT_SYSTEM = """You classify a book as fiction or non-fiction from a sample.

fiction    - novels, short stories, anything with characters, dialogue and a
             narrative. Memoir and narrative history count as fiction here,
             because they need the same handling: character voices and forms
             of address.
nonfiction - argument, exposition, instruction, analysis. Essays, treatises,
             textbooks, reference.

Answer with exactly one word: fiction or nonfiction."""


def detect_profile(api: Gemini, cfg: Config, blocks: list[Block],
                   store: Store) -> str:
    """Work out which profile a book needs, once, and remember it."""
    saved = store.data.get("profile")
    if saved in ("fiction", "nonfiction"):
        print(f"  profile: {saved} (detected earlier)")
        return saved
    sample, _span = sample_blocks(blocks, budget=9000, windows=6)
    try:
        answer = api.ask(DETECT_SYSTEM, f"SAMPLE:\n---\n{sample}\n---",
                         temperature=0.0, max_tokens=16).strip().lower()
    except Exception as exc:                          # noqa: BLE001
        print(f"  ! could not detect the profile ({str(exc)[:60]}), "
              f"using fiction")
        return "fiction"
    guess = "nonfiction" if "nonfiction" in answer or "non-fiction" in answer \
        else "fiction"
    store.data["profile"] = guess
    store.save()
    print(f"  profile: detected as {guess}")
    return guess


def build_bible(api: Gemini, cfg: Config, blocks: list[Block],
                store: Store, rebuild: bool = False,
                keep_blocks: bool = False) -> str:
    bible_path = store.path.parent / "bible.json"

    # A hand-edited bible.json wins over the cached one. This is the control
    # surface: fix a rendering by hand, rerun, and the correction is binding.
    edited = None
    if bible_path.exists() and not rebuild:
        edited = bible_path.read_text(encoding="utf-8")

    if edited is not None and edited.strip():
        if edited.strip() != (store.data.get("bible") or "").strip():
            print("  bible: using your edited bible.json")
            store.data["bible"] = edited
            store.save()
        else:
            print("  bible: cached")
        raw = edited
    elif store.data.get("bible") and not rebuild:
        raw = store.data["bible"]
        store.write_bible(raw)
        print("  bible: cached")
    else:
        sample, span = sample_blocks(blocks)
        print(f"  bible: sampling {len(sample):,} chars spanning "
              f"{span:.0%} of the selection")

        nf = cfg.profile == "nonfiction"
        system = (NF_BIBLE_SYSTEM if nf else BIBLE_SYSTEM).format(
            src=cfg.source_lang, tgt=cfg.target_lang)
        prompt = (NF_BIBLE_PROMPT if nf else BIBLE_PROMPT).format(
            src=cfg.source_lang, tgt=cfg.target_lang, sample=sample)
        if cfg.register in REGISTER_PRESETS:
            prompt = (REGISTER_PRESETS[cfg.register] + "\n\nRecord this in "
                      "the \"register\" field, in your own words, and follow "
                      "it throughout.\n\n" + prompt)
        raw = api.ask(system, prompt, temperature=0.3, json_mode=True,
                      max_tokens=cfg.bible_max_output_tokens)
        store.data["bible"] = raw
        store.save()
        store.write_bible(raw)
        print(f"  bible: built -> {bible_path}")

    # Blocks translated under a different bible are not consistent with the
    # current one. Warning is not enough: a mixed output is exactly the
    # failure the bible exists to prevent, and it is invisible in the EPUB.
    h = bible_hash(raw)
    prev = store.data.get("bible_hash")
    stale = len(store.data["blocks"])
    if prev and prev != h and stale:
        if keep_blocks:
            print(f"  ! {stale} block(s) were translated under a DIFFERENT bible "
                  f"and are being kept (--keep-blocks)")
            print(f"    the output will mix two sets of terminology")
            api.warnings.append(
                f"MIXED OUTPUT: {stale} blocks retained from bible {prev} "
                f"while the rest use {h}")
        else:
            print(f"  bible changed: discarding {stale} block(s) translated "
                  f"under the old one so terminology stays consistent")
            print(f"    (use --keep-blocks to retain them instead)")
            store.data["blocks"] = {}
            store.data["summary"] = ""
            api.warnings.append(
                f"{stale} stale block(s) discarded after bible change")
    store.data["bible_hash"] = h
    store.save()

    problems = validate_bible(raw, cfg)
    if blocks:
        problems += check_cast(sample_blocks(blocks)[0], raw)
    if problems:
        print(f"\n  {len(problems)} issue(s) found in the bible:")
        for p in problems:
            print(f"    ! {p}")
        print(f"    edit {bible_path} and rerun to apply fixes\n")
        api.warnings.extend(f"bible: {p}" for p in problems)
    else:
        print("  bible: validated, no issues")
    return raw



SENTENCE_END = re.compile(r"(?<=[.!?\u0964])\s+")


def translate_oversized(api: Gemini, cfg: Config, block: "Block", clean: str,
                        system: str, ctx: dict[str, Any]) -> str:
    """Translate a single block too large for one response.

    A batch of one cannot be split further, so the block itself must be. Try
    a bigger output budget first; if that still truncates, translate the text
    in sentence-aligned pieces and rejoin. Blocks carrying inline markup are
    not chopped, because the tags cannot be reliably reassembled.
    """
    def one(text: str, max_tokens: int) -> str | None:
        prompt = TRANSLATE_PROMPT.format(blocks=f"[[1]]\n{text}", n=1, **ctx)
        try:
            raw = api.ask(system, prompt, temperature=0.2,
                          thinking=cfg.translate_thinking, max_tokens=max_tokens)
        except RuntimeError:
            return None
        parsed = parse_blocks(raw, 1)
        return parsed[0] if parsed and parsed[0].strip() else None

    big = one(clean, cfg.bible_max_output_tokens)
    if big and not output_problem(clean, big):
        print(f"    ~ oversized block translated with a larger budget")
        return big

    if tag_signature(clean, True):
        api.warnings.append(f"{block.key}: oversized and carries markup, "
                            f"source retained")
        print(f"    ! oversized block with markup, keeping source")
        return clean

    sentences = [x for x in SENTENCE_END.split(re.sub(TAG_RE, "", clean)) if x.strip()]
    if len(sentences) < 2:
        api.warnings.append(f"{block.key}: oversized and unsplittable, "
                            f"source retained")
        return clean

    def render(sents: list[str], depth: int = 0) -> list[str]:
        """Translate a run of sentences, halving it until the model copes."""
        text = " ".join(sents)
        got = one(text, cfg.max_output_tokens)
        if got and not output_problem(text, got):
            return [re.sub(TAG_RE, "", got)]
        if len(sents) == 1 or depth >= 6:
            api.warnings.append(f"{block.key}: piece untranslated, source kept")
            return [text]
        mid = len(sents) // 2
        return render(sents[:mid], depth + 1) + render(sents[mid:], depth + 1)

    print(f"    ~ oversized block: splitting {len(sentences)} sentences")
    out = render(sentences)
    api.warnings.append(f"{block.key}: oversized, translated in "
                        f"{len(out)} pieces")
    return " ".join(out)



def names_map(bible_text: str) -> dict[str, dict[str, Any]]:
    """Coined names the translator should gloss, keyed by source spelling."""
    b = parse_bible(bible_text) or {}
    out: dict[str, dict[str, Any]] = {}
    for entry in (b.get("invented_names") or []):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()
        if not name:
            continue
        out[name] = entry
    return out


NAME_TOKEN = re.compile(r"\b[A-Z][A-Za-z'\u2019-]{1,}\b")


def pending_glosses(batch: list[Block], names: dict[str, dict[str, Any]],
                    introduced: set[str]) -> list[dict[str, Any]]:
    """Names appearing in this batch that have not been glossed yet.

    A gloss belongs at first mention and nowhere else: Treebeard occurs 135
    times in one volume, and glossing each one would be unreadable.
    """
    if not names:
        return []
    text = " ".join(b.text for b in batch)
    seen = {t for t in NAME_TOKEN.findall(text)}
    out = []
    for name, entry in names.items():
        if name in introduced:
            continue
        head = name.split()[0]
        if name not in text and head not in seen:
            continue
        if not (entry.get("gloss") or entry.get("alias_of")):
            continue
        out.append(entry)
    return out


def gloss_instruction(entries: list[dict[str, Any]], tgt: str) -> str:
    if not entries:
        return ""
    lines = []
    for e in entries:
        hindi = e.get(tgt) or e.get("hindi") or e.get("Hindi") or ""
        if e.get("gloss"):
            lines.append(f"  {e['name']} -> {hindi} ({e['gloss']})")
        elif e.get("alias_of"):
            lines.append(f"  {e['name']} -> {hindi} "
                         f"(= {e['alias_of']}, already met)")
    if not lines:
        return ""
    return (
        "\n\nFIRST MENTION IN THIS BATCH. Each name below appears here for "
        "the first time in the book. Write it exactly as shown, with the "
        "bracket, ONCE - at its first occurrence in these blocks. Every later "
        "occurrence, here or in any later batch, is the name alone with no "
        "bracket.\n" + "\n".join(lines)
    )


def translate_batch(api: Gemini, cfg: Config, batch: list[Block], bible: str,
                    summary: str, prev_src: str, prev_tgt: str,
                    position: str = "",
                    names: dict[str, Any] | None = None,
                    introduced: set[str] | None = None) -> list[str]:
    # Strip zero-width anchors and case-based typography before the model
    # sees anything. Page anchors have no word to attach to, and drop caps
    # have no Devanagari counterpart; sending either only causes retries.
    prepped: list[str] = []
    anchors: list[list[tuple[float, str]]] = []
    for b in batch:
        clean, anch = prepare_html(b.html)
        prepped.append(clean)
        anchors.append(anch)

    def finish(parts: list[str]) -> list[str]:
        return [reinsert_anchors(p, a) for p, a in zip(parts, anchors)]

    ctx = dict(
        bible=bible,
        summary=summary or "(start of book)",
        src_lang=cfg.source_lang,
        tgt_lang=cfg.target_lang,
        prev_src=prev_src or "(none)",
        prev_tgt=prev_tgt or "(none)",
        position=position or "(unknown)",
        structure_note=structure_hint(batch),
    )
    prompt = TRANSLATE_PROMPT.format(
        n=len(batch), blocks=render_blocks(batch, prepped), **ctx)
    system = (NF_TRANSLATE_SYSTEM if cfg.profile == "nonfiction"
              else TRANSLATE_SYSTEM).format(src=cfg.source_lang, tgt=cfg.target_lang)
    if cfg.register in REGISTER_PRESETS:
        system += "\n\n" + REGISTER_PRESETS[cfg.register]

    fresh = pending_glosses(batch, names or {}, introduced or set())
    if fresh:
        prompt += gloss_instruction(fresh, cfg.target_lang)

    for attempt in range(3):
        try:
            raw = api.ask(system, prompt,
                          temperature=0.4 if attempt == 0 else 0.15,
                          thinking=cfg.translate_thinking)
        except RuntimeError as exc:
            # Chain exhaustion comes in two flavours and they need opposite
            # responses. Capacity: stop, checkpoint, resume after a reset.
            # Content: every model refused THIS request, so waiting is
            # useless and the request must get smaller.
            if isinstance(exc, ChainExhausted):
                if exc.cause != "content":
                    raise
                if len(batch) > 1:
                    mid = len(batch) // 2
                    print(f"    ~ no model produced output for these "
                          f"{len(batch)} blocks, splitting into "
                          f"{mid} + {len(batch) - mid}")
                    left = translate_batch(api, cfg, batch[:mid], bible, summary,
                                           prev_src, prev_tgt, position)
                    right = translate_batch(api, cfg, batch[mid:], bible, summary,
                                            batch[mid - 1].text[-400:],
                                            re.sub(TAG_RE, "", left[-1])[-400:],
                                            position)
                    return left + right
                print("    ! no model could translate this block, keeping source")
                api.warnings.append(
                    f"{batch[0].key}: refused by every model, source retained")
                return finish([prepped[0]])
            # Truncation means the request was too big, so retrying it
            # unchanged just fails again. Halve the batch instead.
            if "token output limit" in str(exc) and len(batch) == 1:
                return finish([translate_oversized(
                    api, cfg, batch[0], prepped[0], system, ctx)])
            if "empty response" in str(exc) and len(batch) == 1:
                # Nothing left to split. Keep the source so the run continues;
                # the block is recorded as untranslated in the report.
                print("    ! block could not be translated, keeping source")
                api.warnings.append(
                    f"{batch[0].key}: untranslatable, source retained")
                return finish([prepped[0]])
            if (("token output limit" in str(exc) or "empty response" in str(exc))
                    and len(batch) > 1):
                mid = len(batch) // 2
                why = ("output truncated" if "token output limit" in str(exc)
                       else "no model produced output")
                print(f"    ~ {why}, splitting {len(batch)} blocks "
                      f"into {mid} + {len(batch) - mid}")
                api.warnings.append(
                    f"{batch[0].key}: batch split after output truncation")
                left = translate_batch(api, cfg, batch[:mid], bible, summary,
                                       prev_src, prev_tgt, position, names, introduced)
                right = translate_batch(api, cfg, batch[mid:], bible, summary,
                                        batch[mid - 1].text[-400:],
                                        re.sub(TAG_RE, "", left[-1])[-400:],
                                        position, names, introduced)
                return left + right
            raise
        parsed = parse_blocks(raw, len(batch))
        if parsed is None:
            api.retries += 1
            print(f"    ! block count mismatch, retry {attempt + 1}/3")
            continue
        broken = [(i, output_problem(p, o))
                  for i, (p, o) in enumerate(zip(prepped, parsed))]
        broken = [(i, why) for i, why in broken if why]
        if broken and attempt < 2:
            api.retries += 1
            kinds = ", ".join(sorted({w for _i, w in broken}))
            print(f"    ! {len(broken)} block(s) unusable ({kinds}), retry")
            continue
        if broken:
            for i, why in broken:
                api.warnings.append(f"{batch[i].key}: output {why}")
            if len(batch) > 1:
                mid = len(batch) // 2
                print(f"    ! {len(broken)} block(s) unusable, splitting "
                      f"{len(batch)} into {mid} + {len(batch) - mid}")
                left = translate_batch(api, cfg, batch[:mid], bible, summary,
                                       prev_src, prev_tgt, position, names, introduced)
                right = translate_batch(api, cfg, batch[mid:], bible, summary,
                                        batch[mid - 1].text[-400:],
                                        re.sub(TAG_RE, "", left[-1])[-400:],
                                        position, names, introduced)
                return left + right
            # A single block that still will not translate: keep the source
            # so the run continues. The report records it as untranslated.
            print("    ! block could not be translated, keeping source")
            return finish([prepped[0]])
        bad = [i for i, (p, o) in enumerate(zip(prepped, parsed))
               if not check_tags(p, o)]
        if bad and attempt < 2:
            api.retries += 1
            print(f"    ! markup lost in {len(bad)} block(s), retry")
            continue
        if bad:
            for i in bad:
                api.warnings.append(
                    f"{batch[i].key}: semantic markup differs "
                    f"(expected {tag_signature(prepped[i], True)}, "
                    f"got {tag_signature(parsed[i], True)})")
            print(f"    ~ {len(bad)} block(s) kept with markup differences")
        return finish(parsed)

    # Fall back to one block at a time; slower but always aligns.
    api.warnings.append(f"{batch[0].key}: fell back to per-block translation")
    print("    ! falling back to per-block translation")
    out: list[str] = []
    for clean in prepped:
        r = api.ask(system,
                    prompt.replace(render_blocks(batch, prepped),
                                   f"[[1]]\n{clean}")
                          .replace(f"these {len(batch)} blocks", "this 1 block"),
                    temperature=0.2)
        p = parse_blocks(r, 1)
        out.append(p[0] if p else clean)
    return finish(out)


def edit_batch(api: Gemini, cfg: Config, batch: list[Block],
               draft: list[str], bible: str) -> list[str]:
    prepped = [prepare_html(b.html)[0] for b in batch]
    src_txt = "\n".join(f"[[{i+1}]]\n{h}" for i, h in enumerate(prepped))
    dft_txt = "\n".join(f"[[{i+1}]]\n{d}" for i, d in enumerate(draft))
    editor_system = EDITOR_SYSTEM.format(src=cfg.source_lang,
                                        tgt=cfg.target_lang)
    if cfg.register in REGISTER_PRESETS:
        editor_system += "\n\n" + REGISTER_PRESETS[cfg.register]
    try:
        raw = api.ask(
            editor_system,
            EDITOR_PROMPT.format(bible=bible, src_lang=cfg.source_lang,
                                 tgt_lang=cfg.target_lang,
                                 source=src_txt, draft=dft_txt),
            temperature=0.3, thinking=cfg.translate_thinking,
        )
    except Exception as exc:                      # noqa: BLE001
        # Pass 2 is polish. The draft is already a complete translation, so
        # losing the editor must never lose the run.
        print(f"    ~ editor pass unavailable, keeping draft ({str(exc)[:60]})")
        api.warnings.append(f"{batch[0].key}: editor pass skipped - {str(exc)[:120]}")
        return draft
    parsed = parse_blocks(raw, len(batch))
    if parsed is None:
        return draft
    return [p if check_tags(h, p) else d
            for h, d, p in zip(prepped, draft, parsed)]


def bible_hash(raw: str) -> str:
    return hashlib.sha1(raw.strip().encode("utf-8")).hexdigest()[:12]


def slugify(name: str, limit: int = 48) -> str:
    """Readable, filesystem-safe folder name so the checkpoint is findable."""
    s = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
    return (s[:limit].rstrip("_") or "book")



# ---------------------------------------------------------------------------
# Input formats and PDF output
# ---------------------------------------------------------------------------

EBOOK_INPUTS = {".epub", ".mobi", ".azw3", ".azw", ".prc"}

NOTO_URL = ("https://github.com/notofonts/notofonts.github.io/raw/main/fonts/"
            "NotoSerifDevanagari/hinted/ttf/NotoSerifDevanagari-Regular.ttf")


def unpack_ebook(path: Path, workdir: Path) -> Path:
    """Return an EPUB for any supported input, converting if needed.

    Only DRM-free files work. Kindle purchases are encrypted and neither this
    nor any library it uses will open them.
    """
    if path.suffix.lower() == ".epub":
        return path
    try:
        import mobi
    except ImportError:
        raise RuntimeError(
            f"{path.suffix} input needs the 'mobi' package:\n"
            "  python -m pip install mobi")

    print(f"  converting {path.suffix} to EPUB")
    try:
        tmpdir, converted = mobi.extract(str(path))
    except Exception as exc:                          # noqa: BLE001
        raise RuntimeError(
            f"could not open {path.name}: {exc}\n"
            "  If this file came from Kindle it is probably DRM-protected, "
            "which cannot be read here.") from exc

    src = Path(converted)
    if src.suffix.lower() == ".epub":
        dest = workdir / (path.stem + ".epub")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dest)
        print(f"  -> {dest.name}")
        return dest

    # Some files unpack to loose HTML; wrap it into a minimal EPUB.
    root = src.parent
    htmls = sorted([f for f in root.rglob("*.htm*")])
    if not htmls:
        raise RuntimeError(f"{path.name} unpacked but contained no readable text")
    dest = workdir / (path.stem + ".epub")
    build_minimal_epub(htmls, root, dest, path.stem)
    print(f"  -> {dest.name} ({len(htmls)} document(s))")
    return dest


def build_minimal_epub(htmls: list[Path], root: Path, dest: Path,
                       title: str) -> None:
    """Wrap loose HTML files into a spine-ordered EPUB."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    items, refs = [], []
    with zipfile.ZipFile(dest, "w") as z:
        z.writestr("mimetype", "application/epub+zip",
                   compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" '
                   'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="content.opf" '
                   'media-type="application/oebps-package+xml"/></rootfiles>'
                   "</container>")
        for i, h in enumerate(htmls):
            name = f"text/{i:04d}.xhtml"
            z.writestr(name, h.read_text(encoding="utf-8", errors="replace"))
            items.append(f'<item id="i{i}" href="{name}" '
                         f'media-type="application/xhtml+xml"/>')
            refs.append(f'<itemref idref="i{i}"/>')
        for asset in root.rglob("*"):
            if asset.is_file() and asset.suffix.lower() in (
                    ".jpg", ".jpeg", ".png", ".gif", ".css"):
                z.writestr(f"assets/{asset.name}", asset.read_bytes())
        z.writestr("content.opf",
                   '<?xml version="1.0" encoding="utf-8"?>'
                   '<package xmlns="http://www.idpf.org/2007/opf" version="2.0" '
                   'unique-identifier="id">'
                   '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                   f"<dc:title>{title}</dc:title><dc:language>en</dc:language>"
                   '<dc:identifier id="id">booktrans</dc:identifier></metadata>'
                   f"<manifest>{''.join(items)}</manifest>"
                   f"<spine>{''.join(refs)}</spine></package>")


def ensure_font(workdir: Path) -> Path | None:
    """Fetch and cache a Devanagari font for PDF embedding."""
    dest = workdir / "NotoSerifDevanagari-Regular.ttf"
    if dest.exists() and dest.stat().st_size > 50000:
        return dest
    try:
        import urllib.request
        dest.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(NOTO_URL, dest)
        if dest.stat().st_size > 50000:
            return dest
    except Exception as exc:                          # noqa: BLE001
        print(f"  ! could not fetch the Devanagari font ({str(exc)[:60]})")
        print("    the PDF will rely on a system font and may render badly")
    return None


PDF_CSS = """
@page {{ size: {size}; margin: 20mm 18mm 22mm 18mm;
  @bottom-center {{ content: counter(page); font-size: 9pt; color: #666; }} }}
{fontface}
body {{ font-family: {family}; font-size: {fontsize}; line-height: 1.85;
  text-align: justify; hyphens: none; word-break: normal; }}
h1, h2, h3 {{ font-family: {family}; page-break-before: always;
  page-break-after: avoid; text-align: left; margin: 0 0 1.2em 0; }}
h1:first-of-type {{ page-break-before: avoid; }}
p {{ margin: 0 0 0.75em 0; orphans: 2; widows: 2; }}
img {{ max-width: 100%; }}
"""


def epub_to_pdf(epub_path: Path, pdf_path: Path, workdir: Path,
                title: str = "", page_size: str = "A5",
                font_size: str = "12pt", docs: str | None = None,
                cfg: Config | None = None) -> None:
    """Render a translated EPUB to PDF with correct Devanagari shaping."""
    try:
        from weasyprint import HTML, CSS
    except ImportError:
        raise RuntimeError(
            "PDF output needs WeasyPrint:\n"
            "  python -m pip install weasyprint\n"
            "  (Linux also needs: apt-get install libpango-1.0-0 "
            "libpangoft2-1.0-0)")

    ep = Epub(epub_path, workdir / "pdfsrc")
    # Render only what was translated. Otherwise untranslated front matter and
    # endnotes end up in the book, in the source language.
    chosen = [p for _i, p in select_docs(ep.spine, docs, cfg or Config(), None)]
    parts = []
    for doc in chosen:
        soup = BeautifulSoup(doc.read_text(encoding="utf-8", errors="replace"),
                             "html.parser")
        body = soup.find("body")
        if body and body.get_text(strip=True):
            parts.append(body.decode_contents())
    if not parts:
        raise RuntimeError("nothing to render: the EPUB has no body text")

    font = ensure_font(workdir)
    if font:
        fontface = (f"@font-face {{ font-family: BT; "
                    f"src: url('file://{font.resolve()}'); }}")
        family = "BT, serif"
    else:
        fontface = ""
        family = "'Noto Serif Devanagari', 'Nirmala UI', serif"

    html = ("<html><head><meta charset='utf-8'><title>"
            + (title or epub_path.stem) + "</title></head><body>"
            + "\n".join(parts) + "</body></html>")
    css = PDF_CSS.format(size=page_size, fontface=fontface, family=family,
                         fontsize=font_size)
    skipped = len(ep.spine) - len(chosen)
    print(f"  rendering PDF ({len(parts)} documents, {page_size}"
          + (f", {skipped} excluded" if skipped else "") + ")")
    HTML(string=html, base_url=str(ep.root)).write_pdf(
        str(pdf_path), stylesheets=[CSS(string=css)])
    mb = pdf_path.stat().st_size / 1e6
    print(f"  -> {pdf_path.name} ({mb:.1f} MB)")


def select_docs(spine: list[Path], spec: str | None, cfg: Config,
                limit: int | None) -> list[tuple[int, Path]]:
    """Resolve a --docs spec ('16', '16-22', 'book1_.*') to spine entries.

    Indices are ORIGINAL spine positions, so checkpoints stay valid when the
    selection changes. Run with --list to see them.
    """
    idx = list(enumerate(spine))
    if spec:
        picked: list[tuple[int, Path]] = []
        for part in spec.split(","):
            part = part.strip()
            if re.fullmatch(r"\d+", part):
                picked.append(idx[int(part)])
            elif re.fullmatch(r"\d+\s*-\s*\d+", part):
                a, b = (int(x) for x in part.split("-"))
                picked.extend(idx[a:b + 1])
            else:
                rx = re.compile(part, re.I)
                picked.extend((i, p) for i, p in idx if rx.search(p.name))
        seen: set[int] = set()
        out = [(i, p) for i, p in picked if not (i in seen or seen.add(i))]
        return sorted(out)

    skip = re.compile(cfg.skip_docs, re.I)
    out = [(i, p) for i, p in idx if not skip.search(p.name)]
    if idx and len(out) < len(idx) / 2:
        print(f"  ! filename filter would drop {len(idx) - len(out)} of "
              f"{len(idx)} documents; ignoring it")
        out = idx
    return out[:limit] if limit else out


def list_docs(src: Path, cfg: Config, workdir: Path) -> None:
    epub = Epub(src, workdir)
    print(f"{'#':>4} {'file':40s} {'blocks':>7} {'words':>8}  heading")
    for i, p in enumerate(epub.spine):
        soup = BeautifulSoup(p.read_text(encoding="utf-8", errors="replace"),
                             "html.parser")
        bl = extract_blocks(soup, i, cfg)
        w = sum(len(b.text.split()) for b in bl)
        h = soup.find(["h1", "h2", "h3"])
        head = h.get_text(" ", strip=True)[:40] if h else (
            bl[0].text[:40] if bl else "")
        print(f"{i:>4} {p.name:40s} {len(bl):>7} {w:>8,}  {head}")


def run(src: Path, dest: Path, cfg: Config, workdir: Path,
        limit_docs: int | None = None, docs_spec: str | None = None,
        rebuild_bible: bool = False, keep_blocks: bool = False,
        deadline: float | None = None) -> bool:
    workdir.mkdir(parents=True, exist_ok=True)
    # The extracted EPUB is a scratch copy of the source, rebuilt every run.
    # Committing it bloats history and republishes the original text.
    gi = workdir.parent / ".gitignore"
    if not gi.exists():
        gi.parent.mkdir(parents=True, exist_ok=True)
        gi.write_text("*/epub/\n*.tmp\n", encoding="utf-8")
    store = Store(workdir / "checkpoint.json")

    print(f"Opening {src.name}")
    epub = Epub(src, workdir)
    selected = select_docs(epub.spine, docs_spec, cfg, limit_docs)
    if not selected:
        sys.exit("No documents selected. Run with --list to see the spine.")
    print(f"  {len(selected)} of {len(epub.spine)} spine documents selected")

    # --- collect blocks -----------------------------------------------------
    soups: dict[int, BeautifulSoup] = {}
    all_blocks: list[Block] = []
    for di, path in selected:
        soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"),
                             "html.parser")
        soups[di] = soup
        all_blocks.extend(extract_blocks(soup, di, cfg))
    # Some publisher builds put body text in <div> rather than <p>. Retry with
    # a wider tag set only if the standard one found nothing, and remember the
    # choice so a resume uses identical block indices.
    stored = store.data.get("block_tags")
    if stored and tuple(stored) != tuple(cfg.block_tags):
        cfg = dataclasses.replace(cfg, block_tags=tuple(stored))
        soups, all_blocks = {}, []
        for di, path in selected:
            soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"),
                                 "html.parser")
            soups[di] = soup
            all_blocks.extend(extract_blocks(soup, di, cfg))
    elif not all_blocks:
        cfg = dataclasses.replace(cfg, block_tags=cfg.fallback_block_tags)
        soups, all_blocks = {}, []
        for di, path in selected:
            soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"),
                                 "html.parser")
            soups[di] = soup
            all_blocks.extend(extract_blocks(soup, di, cfg))
        if all_blocks:
            print(f"  standard tags found nothing; using the wider set "
                  f"(found {len(all_blocks)} blocks)")
            store.data["block_tags"] = list(cfg.block_tags)
            store.save()

    words = sum(len(b.text.split()) for b in all_blocks)
    anchors_total = sum(len(prepare_html(b.html)[1]) for b in all_blocks)
    print(f"  {len(all_blocks)} blocks, ~{words:,} words, "
          f"{anchors_total} structural anchors handled outside the model")

    if not all_blocks:
        sys.exit(
            "No translatable text found in the selected documents.\n"
            + diagnose_extraction(soups, cfg) + "\n"
            f"  selected: {[p.name for _i, p in selected][:6]}\n"
            "  Run with --list to see every document, its word count and its\n"
            "  heading, then pass the chapter range with --docs (e.g. --docs 8-67)."
        )

    api = Gemini(cfg)

    # Quota per model varies enormously (20/day vs 500/day for models that
    # look interchangeable), and it is only discoverable from a 429. So learn
    # it and put the biggest allowance first on later runs.
    # Quotas are per project. Limits learned under a different API key say
    # nothing about this one, and reordering the chain by them sends every
    # request to the wrong model first.
    known: dict[str, int] = store.data.get("model_limits", {}) or {}
    if store.data.get("model_limits_key") not in (None, api.key_id):
        print(f"  API key changed: discarding {len(known)} stale quota "
              f"observation(s)")
        known = {}
        store.data["model_limits"] = {}
        store.save()
    api.limits.update(known)
    if known:
        ordered = tuple(sorted(cfg.models, key=lambda m: -known.get(m, 100)))
        if ordered != tuple(cfg.models):
            cfg = dataclasses.replace(cfg, models=ordered)
            api.cfg = cfg
            top = ", ".join(f"{m} ({known[m]}/day)" for m in ordered[:4]
                            if m in known)
            if top:
                print(f"  chain reordered by observed quota: {top}")

    # --- pass 0 -------------------------------------------------------------
    if cfg.profile == "auto":
        cfg = dataclasses.replace(
            cfg, profile=detect_profile(api, cfg, all_blocks, store))

    print("\nPass 0: translation bible")
    bible = build_bible(api, cfg, all_blocks, store, rebuild_bible, keep_blocks)

    # --- passes 1 & 2 -------------------------------------------------------
    records: list[dict[str, Any]] = store.data.get("bible_records", []) or []
    if records:
        ch = sum(len(r.get("new_characters") or []) for r in records)
        fm = sum(len(r.get("formality_changes") or []) for r in records)
        print(f"  bible: {len(records)} chapter review(s) on record "
              f"({ch} character(s), {fm} address decision(s))")
    print("\nPass 1/2: translate + edit")
    prev_src = prev_tgt = ""
    summary = store.data.get("summary", "")
    by_doc: dict[int, list[Block]] = {}
    for b in all_blocks:
        by_doc.setdefault(b.doc_index, []).append(b)

    doc_order = {di: i for i, di in enumerate(sorted(by_doc))}
    doc_heading: dict[int, str] = {}
    for di in by_doc:
        h = soups[di].find(["h1", "h2", "h3"])
        if h:
            doc_heading[di] = h.get_text(" ", strip=True)[:40]
        elif by_doc[di]:
            doc_heading[di] = by_doc[di][0].text[:40]

    total_batches = 0
    for _di, _bl in sorted(by_doc.items()):
        total_batches += sum(1 for _ in batch_blocks(_bl, cfg.batch_words))
    est = total_batches * (2 if cfg.run_editor_pass else 1) + len(by_doc) + 1
    done_batches = sum(
        1 for _di, _bl in sorted(by_doc.items())
        for bt in batch_blocks(_bl, cfg.batch_words)
        if all(store.done(b.key) is not None for b in bt))
    print(f"  {total_batches} batches, ~{est} API calls estimated")
    if done_batches:
        print(f"  {done_batches} batch(es) already done, resuming")
    print()
    started = time.time()
    start_done = done_batches

    introduced: set[str] = set(store.data.get("introduced_names", []) or [])
    exhausted = False
    for di in sorted(by_doc):
        if exhausted:
            break
        doc_blocks = by_doc[di]
        pending = [b for b in doc_blocks if store.done(b.key) is None]
        if not pending:
            print(f"  doc {di}/{len(epub.spine)-1}: cached")
            continue

        print(f"  doc {di}/{len(epub.spine)-1} ({len(doc_blocks)} blocks)")
        order = doc_order.get(di, 0)
        head = doc_heading.get(di, "")
        pct = 100 * (order + 1) / max(len(by_doc), 1)
        position = (f"document {order + 1} of {len(by_doc)} in this selection "
                    f"(~{pct:.0f}% through)"
                    + (f", heading: {head}" if head else ""))
        if cfg.review_each_chapter:
            review_chapter(api, cfg, bible, records, doc_blocks, di,
                           position, summary, store)
        active_bible = compose_bible(bible, records, di)
        active_names = names_map(active_bible)
        for batch in batch_blocks(doc_blocks, cfg.batch_words):
            if all(store.done(b.key) is not None for b in batch):
                continue
            if deadline and time.time() > deadline:
                store.save()
                print(f"\n  Runtime budget reached. "
                      f"{len(store.data['blocks'])}/{len(all_blocks)} blocks saved.")
                print("  Rerun the same command to resume.")
                api.warnings.append("run stopped early: runtime budget reached")
                exhausted = True
                break
            try:
                fresh = pending_glosses(batch, active_names, introduced)
                draft = translate_batch(api, cfg, batch, active_bible, summary,
                                        prev_src, prev_tgt, position,
                                        active_names, introduced)
                for e in fresh:
                    introduced.add(e["name"])
                if fresh:
                    store.data["introduced_names"] = sorted(introduced)
                    for e in fresh:
                        tag = e.get("gloss") or f"= {e.get('alias_of')}"
                        print(f"    ~ first mention: {e['name']} ({tag})")
            except ChainExhausted as exc:
                store.save()
                done = len(store.data["blocks"])
                print(f"\n  Out of quota on every model in the chain.")
                print(f"  {str(exc)}")
                print(f"  {done} of {len(all_blocks)} blocks are saved.")
                print(f"  Rerun the same command to resume from here.\n")
                api.warnings.append("run stopped early: all models exhausted")
                exhausted = True
                break
            except RuntimeError as exc:
                if "No model could complete" not in str(exc):
                    # Unexpected, but the run holds hours of work. Save what
                    # exists, report it, and let the next run resume.
                    store.save()
                    print(f"\n  Unexpected error: {str(exc)[:200]}")
                    print(f"  {len(store.data['blocks'])} of {len(all_blocks)} "
                          f"blocks are saved. Rerun to resume.")
                    api.warnings.append(f"run stopped early: {str(exc)[:200]}")
                    exhausted = True
                    break
                store.save()
                done = len(store.data["blocks"])
                print(f"\n  Out of quota on every model in the chain.")
                print(f"  {str(exc)}")
                print(f"  {done} of {len(all_blocks)} blocks are saved.")
                print(f"  Rerun the same command to resume from here, or add "
                      f"models with --models.")
                print(f"  Run --check to see which models this key can use.\n")
                api.warnings.append("run stopped early: all models exhausted")
                exhausted = True
                break
            if cfg.run_editor_pass:
                draft = edit_batch(api, cfg, batch, draft, active_bible)
            for b, out in zip(batch, draft):
                store.put(b.key, out)
            store.save()
            prev_src = batch[-1].text[-400:]
            prev_tgt = re.sub(TAG_RE, "", draft[-1])[-400:]
            done_batches += 1
            pct = 100 * done_batches / max(total_batches, 1)
            rate = (time.time() - started) / max(done_batches - start_done, 1)
            remaining = (total_batches - done_batches) * rate
            bar = "#" * int(pct / 5) + "." * (20 - int(pct / 5))
            print(f"    [{bar}] {pct:5.1f}%  {done_batches}/{total_batches} "
                  f"batches  {api.calls} calls  eta {fmt_duration(remaining)}")

        if exhausted:
            break

        # refresh rolling summary after each document
        chap_text = " ".join(
            re.sub(TAG_RE, "", store.done(b.key) or "") for b in doc_blocks
        )[:9000]
        if len(chap_text.split()) > 120:
            nf = cfg.profile == "nonfiction"
            try:
                summary = api.ask(
                    NF_SUMMARY_SYSTEM if nf else SUMMARY_SYSTEM,
                    (NF_SUMMARY_PROMPT if nf else SUMMARY_PROMPT).format(
                        prev=summary or "(none)", chapter=chap_text),
                    temperature=0.2)
                store.data["summary"] = summary
                store.save()
            except RuntimeError as exc:
                # Continuity aid, not the translation. Losing it must not
                # lose the run, and quota here means quota everywhere.
                api.warnings.append(f"doc {di}: summary skipped - {str(exc)[:80]}")
                if "No model could complete" in str(exc):
                    print("\n  Out of quota on every model in the chain.")
                    print(f"  {len(store.data['blocks'])} of {len(all_blocks)} "
                          f"blocks are saved. Rerun to resume.")
                    exhausted = True
                    break
                print("    ~ summary skipped")

    # --- pass 3 -------------------------------------------------------------
    print("\nPass 3: reassemble")
    for di, path in selected:
        soup = soups[di]
        blocks = {b.block_index: b for b in by_doc.get(di, [])}
        for i, tag in enumerate(soup.find_all(cfg.block_tags)):
            b = blocks.get(i)
            if b is None:
                continue
            out = store.done(b.key)
            if not out:
                continue
            set_inner_html(tag, out)
        path.write_text(str(soup), encoding="utf-8")

    inject_css(epub)
    set_language(epub, "hi")
    epub.repack(dest)

    dev = lat = 0
    for b in all_blocks:
        t = re.sub(TAG_RE, "", store.done(b.key) or "")
        dev += sum(1 for c in t if "\u0900" <= c <= "\u097f")
        lat += sum(1 for c in t if c.isascii() and c.isalpha())

    store.data["introduced_names"] = sorted(introduced)
    if api.limits:
        store.data["model_limits"] = {**known, **api.limits}
        store.data["model_limits_key"] = api.key_id
        store.save()

    store.write_report({
        "source": src.name,
        "output": dest.name,
        "profile": cfg.profile,
        "models": list(cfg.models),
        "calls_per_model": api.model_calls,
        "observed_daily_limits": store.data.get("model_limits", {}),
        "editor_pass": cfg.run_editor_pass,
        "documents": [f"{i}:{p.name}" for i, p in selected],
        "blocks": len(all_blocks),
        "source_words": words,
        "api_calls": api.calls,
        "retries": api.retries,
        "anchors_restored": anchors_total,
        "devanagari_chars": dev,
        "latin_chars_remaining": lat,
        "translated_share": round(dev / max(dev + lat, 1), 4),
        "untranslated_blocks": [
            b.key for b in all_blocks
            if output_problem(b.html, store.done(b.key) or "")
        ],
        "bible_hash": store.data.get("bible_hash"),
        "chapter_reviews": len(records),
        "characters_added": sum(len(r.get("new_characters") or []) for r in records),
        "formality_decisions": sum(len(r.get("formality_changes") or []) for r in records),
        "bible_issues": [w[7:] for w in api.warnings if w.startswith("bible: ")],
        "completed": not exhausted,
        "warnings": api.warnings,
    })

    print(f"\nDone. {api.calls} API calls ({api.retries} retries) -> {dest}")
    print(f"  bible      : {store.path.parent / 'bible.json'}")
    print(f"  report     : {store.path.parent / 'report.json'}")
    print(f"  checkpoint : {store.path}  (delete to force a rerun)")
    if api.warnings:
        print(f"  {len(api.warnings)} warning(s) recorded in report.json")
    if exhausted:
        remaining = len(all_blocks) - len(store.data["blocks"])
        print(f"\n  INCOMPLETE: {remaining} block(s) still to translate.")
    return not exhausted


# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Structure-preserving EPUB translation")
    ap.add_argument("input", type=Path, nargs="?")
    ap.add_argument("-o", "--output", type=Path)
    ap.add_argument("-w", "--workdir", type=Path, default=Path("./booktrans_work"))
    ap.add_argument("--list", action="store_true",
                    help="print spine documents with headings and word counts, then exit")
    ap.add_argument("--docs", help="documents to translate: '16', '16-22', "
                                   "'14,15,16', or a filename regex")
    ap.add_argument("--limit", type=int, help="only first N docs (ignored with --docs)")
    ap.add_argument("--profile", choices=["fiction", "nonfiction", "auto"],
                    default="fiction",
                    help="auto asks the model once, then remembers the answer")
    ap.add_argument("--check", action="store_true",
                    help="test the API key, list usable models, then exit")
    ap.add_argument("--register", choices=["modern", "literary", "auto"],
                    default="auto",
                    help="modern = everyday north Indian Hindustani; "
                         "auto = let the model choose per book")
    ap.add_argument("--page-size", default="A5",
                    help="PDF page size (A5, A4, Letter). A5 suits reading.")
    ap.add_argument("--font-size", default="12pt", help="PDF body font size")
    ap.add_argument("--rebuild-bible", action="store_true",
                    help="regenerate the bible, discarding bible.json")
    ap.add_argument("--thinking", type=int, default=None,
                    help="thinking token budget for translation passes "
                         "(omit to leave unset; some models reject 0)")
    ap.add_argument("--max-runtime", type=float, default=None,
                    help="stop cleanly after N seconds and save (for CI runners "
                         "with a job time limit)")
    ap.add_argument("--gap", type=float, default=None,
                    help="minimum seconds between requests (default 6.5)")
    ap.add_argument("--next", metavar="DIR",
                    help="print the next unfinished EPUB in DIR and exit; "
                         "exit code 3 if there is nothing left")
    ap.add_argument("--wait-max", type=float, default=None,
                    help="with --wait, only sleep if the reset is within N "
                         "seconds; otherwise save and exit 75")
    ap.add_argument("--wait", action="store_true",
                    help="when all models are out of quota, sleep until the "
                         "daily reset and continue (for full books)")
    ap.add_argument("--keep-blocks", action="store_true",
                    help="keep cached blocks even if the bible changed "
                         "(produces mixed terminology)")
    ap.add_argument("--models", help="comma-separated model fallback chain, "
                                     "tried in order")
    ap.add_argument("--no-editor", action="store_true", help="skip pass 2 (halves cost)")
    ap.add_argument("--batch-words", type=int, default=900)
    args = ap.parse_args()
    if not args.input and not (args.check or args.next):
        ap.error("input EPUB is required (or use --check / --next)")

    cfg = Config(
        batch_words=args.batch_words,
        run_editor_pass=not args.no_editor,
        profile=args.profile,
        register=None if args.register == "auto" else args.register,
        wait_for_quota=args.wait,
        max_wait=args.wait_max if args.wait_max is not None else 10 ** 9,
        translate_thinking=args.thinking,
    )
    if args.gap is not None:
        cfg = dataclasses.replace(cfg, min_request_gap=args.gap)
    if args.models:
        cfg = dataclasses.replace(
            cfg, models=tuple(m.strip() for m in args.models.split(",") if m.strip()))

    if args.next:
        books = sorted(f for f in Path(args.next).iterdir()
               if f.suffix.lower() in EBOOK_INPUTS)
        if not books:
            print(f"no .epub files in {args.next}", file=sys.stderr)
            sys.exit(3)
        for bk in books:
            rep = args.workdir / slugify(bk.stem) / "report.json"
            done = False
            if rep.exists():
                try:
                    done = json.loads(rep.read_text(encoding="utf-8")).get("completed")
                except Exception:
                    done = False
            if not done:
                print(bk)
                return
        print("all books complete", file=sys.stderr)
        sys.exit(3)

    if args.check:
        api = Gemini(cfg)
        print("Key accepted. Probing available models...\n")
        try:
            usable = set(api.probe())
        except Exception as exc:
            print(f"  could not list models: {exc}")
            usable = set()
        print("Configured chain (tried in this order):")
        for m in cfg.models:
            mark = "ok" if m in usable else ("?" if not usable else "NOT AVAILABLE")
            print(f"  {m:34s} {mark}")
        chain = suggest_chain(sorted(usable))
        if chain:
            print("\nText-generation models found (newest first, "
                  "image/TTS/video excluded):")
            for m in chain:
                print(f"  {m}")
            print("\nFree quota is counted PER MODEL PER DAY, so a wide chain\n"
                  f"multiplies your budget. Suggested ({len(chain)} models):\n")
            print("  --models " + ",".join(chain))
        print("\nSending one tiny request to confirm generation works")
        print("(models.list can report a model that generateContent rejects, "
              "so this is the real test)...")
        try:
            print("  reply:", api.ask("Reply with exactly: OK", "ping",
                                      temperature=0)[:40].strip())
            print(f"  served by: {list(api.model_calls)}")
        except Exception as exc:
            print(f"  FAILED: {exc}")
        return
    wd = args.workdir / slugify(args.input.stem)

    if args.list:
        wd.mkdir(parents=True, exist_ok=True)
        list_docs(args.input, cfg, wd)
        return

    out = args.output or args.input.with_suffix(".hi.epub")
    if args.input.suffix.lower() not in EBOOK_INPUTS:
        ap.error(f"unsupported input {args.input.suffix}; "
                 f"expected one of {sorted(EBOOK_INPUTS)}")
    wd.mkdir(parents=True, exist_ok=True)
    source = unpack_ebook(args.input, wd)

    want_pdf = out.suffix.lower() == ".pdf"
    epub_out = out.with_suffix(".epub") if want_pdf else out

    deadline = (time.time() + args.max_runtime) if args.max_runtime else None
    complete = run(source, epub_out, cfg, wd, args.limit, args.docs,
                   args.rebuild_bible, args.keep_blocks, deadline)

    if want_pdf:
        try:
            epub_to_pdf(epub_out, out, wd, title=args.input.stem,
                        page_size=args.page_size, font_size=args.font_size,
                        docs=args.docs, cfg=cfg)
        except Exception as exc:                      # noqa: BLE001
            print(f"\n  PDF step failed: {str(exc)[:300]}")
            print(f"  The EPUB is still at {epub_out}")
            if complete:
                sys.exit(75)
    if not complete:
        # EX_TEMPFAIL: work remains, rerun to resume. Distinct from a crash.
        sys.exit(75)


if __name__ == "__main__":
    main()
