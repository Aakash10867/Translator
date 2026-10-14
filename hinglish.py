#!/usr/bin/env python3
"""
hinglish - convert a Devanagari EPUB into Hinglish (Hindi in Latin script).

This is transliteration, not translation: the words stay the same, only the
script changes. No API, no quota, no waiting.

    python hinglish.py book.hi.epub -o book.hinglish.epub

The hard part is schwa deletion. Devanagari writes an inherent "a" after every
consonant, but spoken Hindi drops most of them: करता is "karta", not
"karataa"; अच्छा is "achha", not "achchhaa". Getting that wrong makes the
output unreadable, so most of this file is about deciding which schwas to keep.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import unicodedata
import sys
import zipfile
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString

# --------------------------------------------------------------------------
# Character tables
# --------------------------------------------------------------------------

VOWELS = {
    "अ": "a", "आ": "aa", "इ": "i", "ई": "i", "उ": "u", "ऊ": "oo",
    "ऋ": "ri", "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au",
    # independent ई reads short at the head of a word: imaan, not eemaan

    "ऑ": "o", "ऍ": "e",
}

# Vowel signs (matras) that attach to a consonant.
MATRAS = {
    "ा": "aa", "ि": "i", "ी": "ee", "ु": "u", "ू": "oo", "ृ": "ri",
    "े": "e", "ै": "ai", "ो": "o", "ौ": "au", "ॉ": "o", "ॅ": "e",
}
MATRAS["ी"] = "ee"     # theek, qareeb - shortened to "i" word-finally
MATRAS["ू"] = "oo"     # zaroorat

CONSONANTS = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n",
    "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "n",
    "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n",
    "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
    "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m",
    "य": "y", "र": "r", "ल": "l", "व": "v", "ळ": "l",
    "श": "sh", "ष": "sh", "स": "s", "ह": "h",
    # nukta forms, common in Hindi and Urdu-derived vocabulary
    "क़": "q", "ख़": "kh", "ग़": "gh", "ज़": "z", "ड़": "d", "ढ़": "dh",
    "फ़": "f", "य़": "y", "ऱ": "r",
}


# Words English lent to Hindi. These are written in Devanagari but they are
# English words, so Hinglish should give them back their English spelling:
# जुलाई is July, not "julaaee". Rules cannot detect these - the script gives
# no hint that a word is a loan - so the list is explicit.
LOANWORDS = {
    # months
    "जनवरी": "January", "फ़रवरी": "February", "फरवरी": "February",
    "मार्च": "March", "अप्रैल": "April", "मई": "May", "जून": "June",
    "जुलाई": "July", "अगस्त": "August", "सितंबर": "September",
    "सितम्बर": "September", "अक्टूबर": "October", "अक्तूबर": "October",
    "नवंबर": "November", "नवम्बर": "November", "दिसंबर": "December",
    "दिसम्बर": "December",
    # days as commonly borrowed
    "संडे": "Sunday", "मंडे": "Monday", "फ़्राइडे": "Friday",
    "सैटरडे": "Saturday",
    # titles and address
    "मिस्टर": "Mr.", "मिसेज़": "Mrs.", "मिसेज": "Mrs.", "मिस": "Miss",
    "डॉक्टर": "Doctor", "डाक्टर": "Doctor", "प्रोफ़ेसर": "Professor",
    "प्रोफेसर": "Professor", "कैप्टन": "Captain", "मैडम": "Madam",
    "सर": "Sir", "लेडी": "Lady",
    # places and travel
    "स्टेशन": "station", "प्लेटफ़ॉर्म": "platform", "प्लेटफार्म": "platform",
    "होटल": "hotel", "रेस्तराँ": "restaurant", "ऑफ़िस": "office",
    "ऑफिस": "office", "स्कूल": "school", "कॉलेज": "college",
    "यूनिवर्सिटी": "university", "हॉस्पिटल": "hospital", "बैंक": "bank",
    "मार्केट": "market", "पार्क": "park", "चर्च": "church",
    "कोर्ट": "court", "क्लब": "club", "थिएटर": "theatre",
    "लाइब्रेरी": "library", "फ़ैक्ट्री": "factory", "फैक्ट्री": "factory",
    # transport
    "ट्रेन": "train", "बस": "bus", "कार": "car", "टैक्सी": "taxi",
    "साइकिल": "cycle", "टिकट": "ticket", "इंजन": "engine",
    # objects
    "टेबल": "table", "सोफ़ा": "sofa", "लैंप": "lamp", "कोट": "coat",
    "शर्ट": "shirt", "जैकेट": "jacket", "हैट": "hat", "बूट": "boot",
    "ग्लास": "glass", "बोतल": "bottle", "कप": "cup", "प्लेट": "plate",
    "कॉफ़ी": "coffee", "कॉफी": "coffee", "चॉकलेट": "chocolate",
    "बिस्कुट": "biscuit", "सिगार": "cigar", "सिगरेट": "cigarette",
    "पेन": "pen", "पेंसिल": "pencil", "नोट": "note", "कार्ड": "card",
    "बॉक्स": "box", "बैग": "bag", "कैमरा": "camera", "रेडियो": "radio",
    "फ़ोन": "phone", "टेलीफ़ोन": "telephone", "फ़िल्म": "film",
    # people and roles
    "नर्स": "nurse", "पुलिस": "police", "जज": "judge", "क्लर्क": "clerk",
    "मैनेजर": "manager", "इंजीनियर": "engineer", "आर्टिस्ट": "artist",
    "स्टूडेंट": "student", "पार्टनर": "partner", "सर्वेंट": "servant",
    # measures and money
    "मिनट": "minute", "सेकंड": "second", "पाउंड": "pound",
    "डॉलर": "dollar", "रूबल": "rouble", "कोपेक": "copeck",
    "शिलिंग": "shilling", "पेंस": "pence", "गिनी": "guinea",
    "मील": "mile", "इंच": "inch", "फ़ुट": "foot", "गज़": "yard",
    # abstractions borrowed wholesale
    "नंबर": "number", "लिस्ट": "list", "प्लान": "plan", "आइडिया": "idea",
    "स्टाइल": "style", "क्लास": "class", "लेवल": "level",
}

DIGITS = {"०": "0", "१": "1", "२": "2", "३": "3", "४": "4",
          "५": "5", "६": "6", "७": "7", "८": "8", "९": "9"}

PUNCT = {"।": ".", "॥": ".", "ऽ": ""}

HALANT = "\u094D"
ANUSVARA = "\u0902"      # ं
CHANDRABINDU = "\u0901"  # ँ
VISARGA = "\u0903"       # ः
NUKTA = "\u093C"

DEV_RANGE = re.compile(r"[\u0900-\u097F]")

# Nasal assimilation: ं takes the place of articulation of what follows.
NASAL_BEFORE = {
    "k": "n", "kh": "n", "g": "n", "gh": "n",
    "ch": "n", "chh": "n", "j": "n", "jh": "n",
    "t": "n", "th": "n", "d": "n", "dh": "n", "n": "n",
    "p": "m", "ph": "m", "b": "m", "bh": "m", "m": "m",
}

# Words whose conventional spelling beats any rule.
OVERRIDES = {
    "और": "aur", "है": "hai", "हैं": "hain", "हूँ": "hun", "हूं": "hun",
    "मैं": "main", "में": "mein", "मे": "me", "नहीं": "nahin",
    "यह": "yeh", "वह": "voh", "ये": "ye", "वे": "ve",
    "को": "ko", "का": "ka", "के": "ke", "की": "ki",
    "से": "se", "पर": "par", "तक": "tak", "ही": "hi",
    "कि": "ki", "जो": "jo", "तो": "to", "भी": "bhi", "न": "na",
    "क्या": "kya", "क्यों": "kyun", "कैसे": "kaise", "कहाँ": "kahan",
    "कहां": "kahan", "कब": "kab", "कौन": "kaun", "कुछ": "kuch",
    "आप": "aap", "तुम": "tum", "तू": "tu", "हम": "hum",
    "मेरा": "mera", "मेरी": "meri", "मेरे": "mere",
    "उसने": "usne", "उन्होंने": "unhone", "उसका": "uska",
    "था": "tha", "थी": "thi", "थे": "the", "हो": "ho", "गया": "gaya",
    "अच्छा": "achha", "अच्छी": "achhi", "बहुत": "bahut",
    "लेकिन": "lekin", "क्योंकि": "kyunki", "जैसे": "jaise",
    "साथ": "saath", "बाद": "baad", "पहले": "pehle", "फिर": "phir",
    "एक": "ek", "दो": "do", "तीन": "teen",
    "नमस्ते": "namaste", "बच्चा": "bachcha", "बच्चे": "bachche",
}


# --------------------------------------------------------------------------
# Tokenising a Devanagari word into syllables
# --------------------------------------------------------------------------

class Unit:
    """One consonant (possibly a cluster) plus its vowel, or a bare vowel."""

    __slots__ = ("cons", "vowel", "explicit", "nasal", "is_vowel")

    def __init__(self, cons: str = "", vowel: str = "", explicit: bool = False,
                 nasal: str = "", is_vowel: bool = False):
        self.cons = cons          # romanised consonant cluster
        self.vowel = vowel        # romanised vowel
        self.explicit = explicit  # vowel came from a matra or standalone vowel
        self.nasal = nasal
        self.is_vowel = is_vowel


def parse_word(word: str) -> list[Unit]:
    """Split a Devanagari word into units, keeping track of implicit schwas.

    Normalise to NFD first: letters like ड़ ज़ फ़ exist both as a single
    precomposed codepoint and as base + nukta. Decomposing makes one code
    path handle both, instead of leaving half of them untransliterated.
    """
    word = unicodedata.normalize("NFD", word)
    units: list[Unit] = []
    i = 0
    n = len(word)
    while i < n:
        ch = word[i]

        # consonant, possibly with a nukta
        base = ch
        if i + 1 < n and word[i + 1] == NUKTA:
            if (ch + NUKTA) in CONSONANTS:
                base = ch + NUKTA
            i += 1                        # consume it either way
        # ज्ञ is a fixed conjunct with its own sound: gyan, not jnan.
        if word[i:i + 3] == "\u091C\u094D\u091E":
            i += 3
            vowel, explicit = "a", False
            if i < n and word[i] in MATRAS:
                vowel, explicit = MATRAS[word[i]], True
                i += 1
            nasal = ""
            while i < n and word[i] in (ANUSVARA, CHANDRABINDU, VISARGA):
                nasal = "n" if word[i] != VISARGA else "h"
                i += 1
            units.append(Unit(cons="gy", vowel=vowel, explicit=explicit,
                              nasal=nasal))
            continue

        if base in CONSONANTS:
            cluster = CONSONANTS[base]
            i += 1
            # absorb halant-joined consonants into one cluster
            while i < n and word[i] == HALANT:
                i += 1
                if i >= n:
                    # word-final halant: no vowel at all
                    units.append(Unit(cons=cluster, vowel="", explicit=True))
                    return units
                nxt = word[i]
                if i + 1 < n and word[i + 1] == NUKTA and (nxt + NUKTA) in CONSONANTS:
                    nxt = nxt + NUKTA
                    i += 1
                if nxt not in CONSONANTS:
                    break
                cluster += CONSONANTS[nxt]
                i += 1
            vowel, explicit = "a", False
            if i < n and word[i] in MATRAS:
                vowel, explicit = MATRAS[word[i]], True
                i += 1
            nasal = ""
            while i < n and word[i] in (ANUSVARA, CHANDRABINDU, VISARGA):
                nasal = "n" if word[i] != VISARGA else "h"
                i += 1
            units.append(Unit(cons=cluster, vowel=vowel, explicit=explicit,
                              nasal=nasal))
            continue

        if ch in VOWELS:
            i += 1
            nasal = ""
            while i < n and word[i] in (ANUSVARA, CHANDRABINDU, VISARGA):
                nasal = "n" if word[i] != VISARGA else "h"
                i += 1
            units.append(Unit(vowel=VOWELS[ch], explicit=True, nasal=nasal,
                              is_vowel=True))
            continue

        if ch in DIGITS:
            units.append(Unit(cons=DIGITS[ch], vowel="", explicit=True))
            i += 1
            continue
        if ch in PUNCT:
            units.append(Unit(cons=PUNCT[ch], vowel="", explicit=True))
            i += 1
            continue

        if ch in MATRAS or ch in (HALANT, ANUSVARA, CHANDRABINDU,
                                  VISARGA, NUKTA):
            i += 1                        # orphaned mark: nothing to attach to
            continue

        units.append(Unit(cons=ch, vowel="", explicit=True))
        i += 1
    return units


def shorten_final_ee(units: list[Unit]) -> None:
    """Word-final ी is written i, not ee: paani and ladki, but theek."""
    if units and units[-1].vowel == "ee":
        units[-1].vowel = "i"


def delete_schwas(units: list[Unit]) -> None:
    """Decide which inherent 'a' vowels are actually pronounced.

    The standard Hindi rule runs right to left and never deletes two adjacent
    schwas - that constraint is what separates "daldal" from "daladl" and
    "samajh" from "samjh". Word-initial schwas are always kept.
    """
    n = len(units)
    if n == 0:
        return

    deleted_at_next = False
    for idx in range(n - 1, -1, -1):
        u = units[idx]
        if u.explicit or u.is_vowel or not u.cons or not u.vowel:
            deleted_at_next = False
            continue
        if idx == 0:
            deleted_at_next = False
            continue                      # never strip a word-initial schwa
        if deleted_at_next:
            deleted_at_next = False       # no two deletions in a row
            continue
        u.vowel = ""
        deleted_at_next = True


def render(units: list[Unit]) -> str:
    out = []
    for u in units:
        piece = u.cons + u.vowel
        if u.nasal:
            if u.nasal == "h":
                piece += "h"
            else:
                piece += "n"
        out.append(piece)
    text = "".join(out)
    # tidy the seams
    text = re.sub(r"aa+", "aa", text)
    text = re.sub(r"([bcdfghjklmnpqrstvwxyz])\1{2,}", r"\1\1", text)
    return text


def fix_nasals(word: str, units: list[Unit]) -> str:
    """Use m before labials, n elsewhere."""
    out = []
    for i, u in enumerate(units):
        piece = u.cons + u.vowel
        if u.nasal == "n":
            nxt = units[i + 1].cons if i + 1 < len(units) else ""
            piece += NASAL_BEFORE.get(nxt[:2], NASAL_BEFORE.get(nxt[:1], "n"))
        elif u.nasal == "h":
            piece += "h"
        out.append(piece)
    text = "".join(out)
    text = re.sub(r"aa+", "aa", text)
    # A word ending in the long vowel is written with a single a in practice:
    # karta, ja, larka - not kartaa, jaa, larkaa.
    text = re.sub(r"aa$", "a", text)
    return text


# Filled at run time from the bible and the English original.
NAME_MAP: dict[str, str] = {}
LEXICON: dict[str, str] = {}   # learned borrowings, persisted between books
STATS = {"bible": 0, "loan": 0, "learned": 0}


WORD_CACHE: dict[str, str] = {}

DANDA = "।॥"


def translit_word(word: str, use_names: bool = True) -> str:
    """Transliterate one Devanagari token, reusing earlier decisions."""
    key = (word, use_names)
    hit = WORD_CACHE.get(key)
    if hit is not None:
        return hit
    out = _translit_word(word, use_names)
    WORD_CACHE[key] = out
    return out


def _translit_word(word: str, use_names: bool) -> str:
    core = word.rstrip(DANDA)
    tail = word[len(core):].replace("।", ".").replace("॥", ".")

    if use_names and core in NAME_MAP:
        STATS["bible"] += 1
        return NAME_MAP[core] + tail
    if core in OVERRIDES:
        return OVERRIDES[core] + tail
    if core in LOANWORDS:
        STATS["loan"] += 1
        return LOANWORDS[core] + tail
    if core in LEXICON:
        STATS["learned"] += 1
        return LEXICON[core] + tail

    units = parse_word(core)
    delete_schwas(units)
    shorten_final_ee(units)
    return fix_nasals(core, units) + tail



TITLE_WORDS = ("Mr.", "Mrs.", "Miss", "Doctor", "Dr.", "Professor",
               "Captain", "Sir", "Lady", "Madam")

SENTENCE_CAP = re.compile(r"(^|[.!?]\s+|[\n\r]\s*|[\u201c\"]\s*)([a-z])")
AFTER_TITLE = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in TITLE_WORDS) + r")\s+([a-z]\w*)")

# Titles double as ordinary nouns ("Doctor ne kaha"), so do not capitalise a
# Hindi function word just because a title precedes it.
FUNCTION_WORDS = {
    "ne", "ka", "ki", "ke", "ko", "se", "mein", "me", "par", "tak", "aur",
    "ya", "hi", "bhi", "to", "na", "nahin", "hai", "hain", "tha", "thi",
    "the", "ho", "hun", "kaha", "bola", "sahab", "sahib", "ji", "wale",
    "wali", "wala", "ne'", "kar", "karke",
}


def capitalise(text: str) -> str:
    """Sentence case, and capitals for the name after a title.

    Devanagari has no letter case, so a plain transliteration comes out
    entirely lowercase. Hinglish is written in Latin script and readers expect
    Latin conventions.
    """
    text = SENTENCE_CAP.sub(lambda m: m.group(1) + m.group(2).upper(), text)
    def title_fix(m: "re.Match") -> str:
        word = m.group(2)
        if word.lower() in FUNCTION_WORDS:
            return m.group(0)
        return f"{m.group(1)} {word[0].upper()}{word[1:]}"

    text = AFTER_TITLE.sub(title_fix, text)
    return text


TOKEN = re.compile(r"[\u0900-\u097F]+|[^\u0900-\u097F]+")


def transliterate(text: str) -> str:
    if not DEV_RANGE.search(text):
        return text
    out = []
    for tok in TOKEN.findall(text):
        if DEV_RANGE.search(tok):
            out.append(translit_word(tok))
        else:
            out.append(tok.replace("।", ".").replace("॥", "."))
    return capitalise("".join(out))



# --------------------------------------------------------------------------
# Proper nouns from the translation bible and the English original
# --------------------------------------------------------------------------

TITLES = {"mr", "mrs", "miss", "ms", "dr", "sir", "the", "uncle", "aunt",
          "lady", "lord", "master", "young", "old"}


def skeleton(word: str) -> str:
    """Consonant skeleton, for comparing spellings that sound alike."""
    w = re.sub(r"[^a-z]", "", word.lower())
    w = re.sub(r"(.)\1+", r"\1", w)
    w = w.replace("ph", "f").replace("wh", "w").replace("ck", "k")
    w = w.replace("y", "i").replace("w", "v").replace("j", "z")
    return re.sub(r"[aeiou]", "", w) or w


def sounds_alike(a: str, b: str, floor: float = 0.6) -> bool:
    from difflib import SequenceMatcher
    sa, sb = skeleton(a), skeleton(b)
    if not sa or not sb:
        return False
    if sa == sb:
        return True
    return SequenceMatcher(None, sa, sb).ratio() >= floor


def clean_english(name: str) -> str:
    name = re.sub(r"\([^)]*\)", " ", name)      # drop "(The Convict)"
    name = name.split("/")[0]                    # drop "Common / Uncommon"
    return " ".join(name.split()).strip(" .,:;")


def pairs_from_bible(paths: list[Path]) -> dict[str, str]:
    """Devanagari token -> original spelling, taken from the bible.

    Only kept where the Hindi actually SOUNDS like the English. A bible also
    records translated concepts (उम्मीदें -> Expectations); substituting those
    would push English words into Hinglish prose, which is not what we want.
    """
    out: dict[str, str] = {}
    raw: list[tuple[str, str]] = []
    for path in paths:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        blobs = data if isinstance(data, list) else [data]
        for blob in blobs:
            if not isinstance(blob, dict):
                continue
            for key in ("characters", "new_characters"):
                for c in (blob.get(key) or []):
                    if isinstance(c, dict):
                        h = (c.get("name_Hindi") or c.get("name_हिंदी") or "")
                        if h and c.get("name"):
                            raw.append((h, c["name"]))
            for key in ("glossary", "new_glossary", "terminology"):
                for g in (blob.get(key) or []):
                    if isinstance(g, dict) and g.get("hindi") and g.get("term"):
                        raw.append((g["hindi"], g["term"]))

    for hi, en in raw:
        hi = clean_english(hi)
        en = clean_english(en)
        if not hi or not en or not DEV_RANGE.search(hi):
            continue
        htoks = [w for w in hi.split() if DEV_RANGE.search(w)]
        etoks = [w for w in en.split() if w.lower().strip(".") not in TITLES]
        if not htoks or not etoks:
            continue
        if len(htoks) == len(etoks):
            for h, e in zip(htoks, etoks):
                if sounds_alike(translit_word(h, use_names=False), e):
                    out.setdefault(h, e.strip(".,"))
        elif len(htoks) == 1:
            for e in etoks:
                if sounds_alike(translit_word(htoks[0], use_names=False), e):
                    out.setdefault(htoks[0], e.strip(".,"))
                    break
    return out



# --------------------------------------------------------------------------
# Learning loanwords instead of listing them
# --------------------------------------------------------------------------

LEARN_SYSTEM = """You identify loanwords in Hindi.

Hindi has borrowed heavily from English and other Latin-script languages, and
writes those borrowings in Devanagari. जुलाई is the English word July. स्टेशन
is station. When such a word is written back in Latin script it should get its
original spelling, not a phonetic respelling.

Hindi has also borrowed from Persian, Arabic, Turkish and Sanskrit. Those are
NOT what you are looking for: किताब, ज़रूरत and दुनिया are ordinary Hindi words
and must be reported as null, even though they came from elsewhere originally.
Only words whose source is written in the Latin alphabet count.

Return ONLY valid JSON. No markdown fences, no commentary."""

LEARN_PROMPT = """For each Devanagari word below, decide whether it is a \
borrowing from English or another Latin-script language.

Return a JSON object mapping each word to its original spelling, or to null if
it is an ordinary Hindi word.

  {{"जुलाई": "July", "स्टेशन": "station", "किताब": null, "दलदल": null}}

Keep the original spelling as it is normally written in that language,
including capitals for proper nouns and months. Personal and place names count
as borrowings when they come from a Latin-script language.

Include every word from the list as a key. When unsure, use null: a wrong
substitution is worse than a missed one.

WORDS (with a rough phonetic reading, which may be inaccurate):
{words}"""


def candidate_words(epub: Path, limit: int = 1200,
                    min_count: int = 2) -> list[tuple[str, int]]:
    """Frequent Devanagari words that no existing source explains."""
    import collections
    counts: collections.Counter = collections.Counter()
    with zipfile.ZipFile(epub) as z:
        for name in z.namelist():
            if not name.lower().endswith((".html", ".xhtml", ".htm")):
                continue
            text = BeautifulSoup(z.read(name).decode("utf-8", "replace"),
                                 "html.parser").get_text(" ", strip=True)
            for tok in TOKEN.findall(text):
                if DEV_RANGE.search(tok):
                    core = tok.strip(DANDA)
                    if len(core) > 1:
                        counts[core] += 1
    known = set(OVERRIDES) | set(LOANWORDS) | set(NAME_MAP) | set(LEXICON)
    out = [(w, c) for w, c in counts.most_common()
           if w not in known and c >= min_count]
    return out[:limit]


def learn_lexicon(words: list[tuple[str, int]], models: str | None = None,
                  batch: int = 80) -> dict[str, str]:
    """Ask Gemini which of these are Latin-script borrowings.

    Reuses booktrans's client, so model cascading, quota handling and backoff
    all come for free.
    """
    try:
        from booktrans import Gemini, Config, parse_bible
    except ImportError as exc:
        raise RuntimeError("learning needs booktrans.py beside this "
                           f"script: {exc}") from exc

    cfg = Config(min_request_gap=2.0)
    if models:
        import dataclasses
        cfg = dataclasses.replace(
            cfg, models=tuple(m.strip() for m in models.split(",") if m.strip()))
    api = Gemini(cfg)

    found: dict[str, str] = {}
    checked = 0
    for i in range(0, len(words), batch):
        chunk = words[i:i + batch]
        print(f"    batch {i // batch + 1}/{(len(words) + batch - 1) // batch}: "
              f"asking about {len(chunk)} word(s)...", flush=True)
        listing = "\n".join(
            f"  {w}   (reads roughly as: {translit_word(w, False)}, "
            f"appears {c}x)" for w, c in chunk)
        try:
            raw = api.ask(LEARN_SYSTEM, LEARN_PROMPT.format(words=listing),
                          temperature=0.1, json_mode=True)
        except Exception as exc:                       # noqa: BLE001
            print(f"  ! stopped learning after {checked} word(s): "
                  f"{str(exc)[:90]}", flush=True)
            break
        data = parse_bible(raw) or {}
        if not isinstance(data, dict):
            continue
        checked += len(chunk)
        for w, en in data.items():
            if not isinstance(en, str) or not en.strip():
                continue
            if DEV_RANGE.search(en):
                continue                               # must be Latin script
            if not re.fullmatch(r"[A-Za-z][A-Za-z.'\- ]{0,30}", en.strip()):
                continue
            found[w] = en.strip()
        print(f"      {min(i + batch, len(words))}/{len(words)} examined, "
              f"{len(found)} borrowing(s) found so far", flush=True)
    return found


def load_lexicon(path: Path) -> dict[str, str]:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {k: v for k, v in data.items() if isinstance(v, str)}
        except Exception:
            pass
    return {}


def save_lexicon(path: Path, data: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(sorted(data.items())),
                               ensure_ascii=False, indent=1), encoding="utf-8")


# --------------------------------------------------------------------------
# EPUB rewriting
# --------------------------------------------------------------------------


BLOCKISH = ("p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "li",
            "td", "th", "div", "dd", "dt", "figcaption")


def rewrite_container(node_list: list) -> bool:
    """Transliterate a run of text nodes as one string, then put it back.

    Inline markup splits words across text nodes - a drop cap makes जुलाई into
    "जु" plus "लाई" - and converting each node on its own means no lookup ever
    sees the whole word. So join first, transliterate, and give each word back
    to the node it started in.
    """
    spans, joined = [], []
    pos = 0
    for n in node_list:
        text = str(n)
        spans.append((n, pos, pos + len(text)))
        joined.append(text)
        pos += len(text)
    whole = "".join(joined)
    if not DEV_RANGE.search(whole):
        return False

    owner: dict[int, list[str]] = {i: [] for i in range(len(spans))}
    for m in TOKEN.finditer(whole):
        tok = m.group(0)
        out = (translit_word(tok) if DEV_RANGE.search(tok)
               else tok.replace("।", ".").replace("॥", "."))
        idx = 0
        for i, (_n, a, b) in enumerate(spans):
            if a <= m.start() < b:
                idx = i
                break
        else:
            idx = len(spans) - 1
        owner[idx].append(out)

    for i, (n, _a, _b) in enumerate(spans):
        n.replace_with(NavigableString("".join(owner[i])))
    return True


def convert_epub(src: Path, dest: Path) -> tuple[int, int]:
    """Rewrite every text node of an EPUB into Latin script."""
    work = dest.parent / (dest.stem + "_work")
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    with zipfile.ZipFile(src) as z:
        z.extractall(work)

    docs = converted = 0
    for path in sorted(work.rglob("*")):
        if path.suffix.lower() not in (".html", ".xhtml", ".htm"):
            continue
        raw = path.read_text(encoding="utf-8", errors="replace")
        if not DEV_RANGE.search(raw):
            continue
        soup = BeautifulSoup(raw, "html.parser")
        touched = False

        # Work container by container so a word split across inline tags is
        # still transliterated as one word.
        done: set[int] = set()
        containers = [t for t in soup.find_all(BLOCKISH)
                      if not t.find(BLOCKISH)]
        for tag in containers:
            nodes = [n for n in tag.descendants
                     if isinstance(n, NavigableString)
                     and n.parent.name not in ("script", "style")]
            if not nodes:
                continue
            done.update(id(n) for n in nodes)
            if rewrite_container(nodes):
                touched = True
                converted += len(nodes)

        # Anything outside a recognised container still gets converted.
        for node in list(soup.find_all(string=True)):
            if id(node) in done or node.parent.name in ("script", "style"):
                continue
            s_ = str(node)
            if not DEV_RANGE.search(s_):
                continue
            node.replace_with(NavigableString(transliterate(s_)))
            touched = True
            converted += 1

        if touched:
            docs += 1
            path.write_text(str(soup), encoding="utf-8")

    # Latin script needs no Devanagari font or extra line height
    for css in work.rglob("*.css"):
        txt = css.read_text(encoding="utf-8", errors="replace")
        txt = txt.replace("Noto Serif Devanagari", "Georgia")
        css.write_text(txt, encoding="utf-8")
    for opf in work.rglob("*.opf"):
        txt = opf.read_text(encoding="utf-8", errors="replace")
        txt = re.sub(r"<dc:language>[^<]*</dc:language>",
                     "<dc:language>hi-Latn</dc:language>", txt)
        opf.write_text(txt, encoding="utf-8")

    if dest.exists():
        dest.unlink()
    with zipfile.ZipFile(dest, "w") as z:
        mt = work / "mimetype"
        if mt.exists():
            z.write(mt, "mimetype", compress_type=zipfile.ZIP_STORED)
        for f in sorted(work.rglob("*")):
            if f.is_file() and f.name != "mimetype":
                z.write(f, f.relative_to(work), compress_type=zipfile.ZIP_DEFLATED)
    shutil.rmtree(work, ignore_errors=True)
    return docs, converted



def load_name_sources(args) -> None:
    """Collect original spellings from the bible and any extra word list."""
    lex_path = args.lexicon or (Path(args.work) / "lexicon.json")
    LEXICON.update(load_lexicon(lex_path))
    if LEXICON:
        print(f"  {len(LEXICON)} learned borrowing(s) from {lex_path}")
    else:
        # Silence here means names quietly come out worse, which is hard to
        # spot in a script you cannot read.
        print(f"  no learned lexicon at {lex_path} "
              f"(names rely on the bible and the built-in list)")

    extra = getattr(args, "loanwords", None)
    if extra and Path(extra).exists():
        try:
            LOANWORDS.update(json.loads(Path(extra).read_text(encoding="utf-8")))
            print(f"  loanword list extended from {Path(extra).name}")
        except Exception as exc:
            print(f"  ! could not read {extra}: {str(exc)[:60]}")
    bibles = list(args.bible or [])
    if not bibles and args.work and args.input:
        stem = re.sub(r"[^A-Za-z0-9]+", "_", args.input.stem).strip("_")
        for d in sorted(Path(args.work).glob("*")):
            if not d.is_dir():
                continue
            key = d.name.lower().replace("_", "")
            if key[:12] and key[:12] in stem.lower().replace("_", ""):
                bibles += [d / "bible.json", d / "bible_chapters.json"]
                break
    bibles = [b for b in bibles if b and b.exists()]
    if bibles:
        NAME_MAP.update(pairs_from_bible(bibles))
        print(f"  {len(NAME_MAP)} name(s) from "
              + ", ".join(b.name for b in bibles))


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Convert a Devanagari EPUB to Hinglish (Latin script)")
    ap.add_argument("input", type=Path, nargs="?")
    ap.add_argument("-o", "--output", type=Path)
    ap.add_argument("--text", help="transliterate one string and exit")
    ap.add_argument("--bible", type=Path, action="append", default=None,
                    help="bible.json / bible_chapters.json for original "
                         "spellings of names (repeatable)")
    ap.add_argument("--work", type=Path, default=Path("./booktrans_work"),
                    help="where to look for a matching bible automatically")
    ap.add_argument("--loanwords", type=Path,
                    help="JSON file of extra Devanagari -> English spellings, "
                         "merged over the built-in list")
    ap.add_argument("--learn", action="store_true",
                    help="ask Gemini which unknown words are English "
                         "borrowings, and remember them (needs GEMINI_API_KEY)")
    ap.add_argument("--lexicon", type=Path, default=None,
                    help="where learned borrowings live "
                         "(default <work>/lexicon.json, shared by all books)")
    ap.add_argument("--learn-limit", type=int, default=1200,
                    help="how many unknown words to examine")
    ap.add_argument("--models", default=None,
                    help="model chain for learning, comma separated")
    ap.add_argument("--no-names", action="store_true",
                    help="pure transliteration, ignore all name sources")
    args = ap.parse_args()

    if not args.no_names:
        load_name_sources(args)

    if args.text:
        print(transliterate(args.text))
        return
    if not args.input:
        ap.error("give an EPUB, or --text to try a phrase")

    if args.learn:
        lex_path = args.lexicon or (Path(args.work) / "lexicon.json")
        try:
            cands = candidate_words(args.input, args.learn_limit)
            print(f"Learning: {len(cands)} unknown word(s) to examine")
            if cands:
                found = learn_lexicon(cands, args.models)
                LEXICON.update(found)
                save_lexicon(lex_path, LEXICON)
                print(f"  {len(found)} new borrowing(s); lexicon now "
                      f"{len(LEXICON)} entries -> {lex_path}")
                WORD_CACHE.clear()
        except Exception as exc:                      # noqa: BLE001
            # Learning is a bonus. Transliteration works without it, so a
            # missing SDK or spent quota must not lose the conversion.
            print(f"  ! learning skipped: {str(exc)[:160]}")
            print("    continuing with the built-in list and the bible")

    out = args.output or args.input.with_suffix(".hinglish.epub")
    print(f"Reading {args.input.name}")
    docs, nodes = convert_epub(args.input, out)
    print(f"  {docs} documents, {nodes:,} text nodes converted")
    if any(STATS.values()):
        print(f"  original spellings used: {STATS['bible']:,} name(s), "
              f"{STATS['loan']:,} listed loanword(s), "
              f"{STATS['learned']:,} learned")
    print(f"  -> {out}")


if __name__ == "__main__":
    main()
