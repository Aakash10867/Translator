# booktrans

Structure-preserving literary EPUB translation, English → Hindi, via the Gemini API.

```bash
python -m pip install google-genai beautifulsoup4
export GEMINI_API_KEY=...

python booktrans.py book.epub --limit 6      # test on ~6 documents first
python booktrans.py book.epub -o book.hi.epub
```

Flags: `--docs SPEC`, `--limit N`, `--no-editor` (skip pass 2, halves cost),
`--model`, `--batch-words`, `-w` workdir.

## How it works

**Pass 0 — Book Bible.** One JSON call over a three-depth sample. Fixes the
things that drift over 300 pages: register (Sanskritised ↔ Hindustani), a
**formality matrix** (आप/तुम/तू for every speaking pair), name transliterations,
and a motif glossary. Hindi forces a formality choice English doesn't make.
Unfixed, chapter 3 has them on तुम, chapter 19 slips to आप, and the
relationship silently changes.

**Pass 1 — Translate.** Paragraphs batched to ~900 words, sent as numbered
`[[N]]` blocks with the bible, a rolling plot summary, and the previous
batch's tail in both languages.

**Pass 2 — Editor.** Source and draft side by side. Fixes calques, English
word order, and bible violations. This is where quality visibly jumps; skip
it only if quota-bound.

**Pass 3 — Reassemble.** Text written back into the original EPUB skeleton.
Line-height 1.75 for matras, hyphenation off, `dc:language` set to `hi`.

## Two things that were not obvious

**Inner HTML, not plain text.** The test EPUB had 922 paragraphs carrying
inline emphasis spans. `get_text()` → translate → write back destroys all of
them. Blocks are therefore sent as inner HTML, and the model is told to *move*
tags to the Hindi word that carries the emphasis — word order differs, so
position-matching is wrong.

**`Tag.append()` detaches from the source parent.** Iterating
`BeautifulSoup(html).contents` while appending mutates the list mid-iteration
and skips every second node, silently dropping markup. See `set_inner_html()`.
Verified: 3,338 blocks / 1,978 inline tags survive a full round trip.

## Input and output formats

Input: `.epub`, `.mobi`, `.azw3`, `.azw`, `.prc`. Non-EPUB files are converted
first via the `mobi` package. **DRM-free files only** - Kindle purchases are
encrypted and cannot be opened.

Output: `.epub` (default) or `.pdf`, chosen from the `-o` extension.

```
python booktrans.py book.azw3 -o book.hi.pdf --page-size A5 --font-size 12pt
```

PDF uses WeasyPrint, which shapes Devanagari correctly through HarfBuzz -
conjuncts and matras render properly, which the simpler HTML-to-PDF tools get
wrong. Noto Serif Devanagari is downloaded once and embedded. Only the
documents named by `--docs` are rendered, so untranslated front matter and
endnotes stay out of the book.

Linux needs `libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b` for PDF output.

EPUB reflows and lets the reader change font size; PDF does not. For reading
on a phone or e-reader, EPUB is the better format. Use PDF for printing, or
when the target device only accepts it.

## Invented names

Author-coined names are transliterated, always - names stay names. Where the
source name carries an image a reader of the original perceives, two or three
everyday words follow it in brackets, **once, at first mention**, and never
again:

```
first mention   Treebeard -> ट्रीबियर्ड (दाढ़ी वाला पेड़)
thereafter      Treebeard -> ट्रीबियर्ड
```

The test is what a present-day reader perceives, not etymology. Isengard means
"iron-enclosure" in Old English and gets no gloss, because no modern reader
sees that; glossing it would hand the translated reader authorial intent the
original reader never receives. Frodo gets none either, for the same reason.

The bracket also carries identity. When a character appears under a second,
opaque name, the gloss says so - `फैंगोर्न (= Treebeard, already met)` - because
one name in such a pair translates and the other does not, and the reader would
otherwise have no way to connect them.

Glosses obey the register preset, so with `--register modern` they read as
speech (तेज़ चलने वाला) rather than literary phrasing.

Entries live in the bible's `invented_names`, chapter reviews add coinages as
they appear, and which names have been introduced is carried in the checkpoint
so a resumed run does not gloss the same name twice.

## Register

`--register modern` (the workflow default) forces the everyday standard Hindi
read and spoken across north India today, rather than letting the model pick
per book. Without it the register drifts: one book comes out colloquial, the
next Sanskritised.

The preset names the specific choices - इस्तेमाल over प्रयोग, ज़रूरत over
आवश्यकता, मदद over सहायता - and bars the formal written connectives (अतः,
किंतु, परंतु, तथा, एवं) in favour of the spoken ones. Urdu-derived vocabulary
is treated as ordinary Hindi, because it is.

It is applied in three places: the bible, every translation batch, and the
editor pass. Stating it once in the bible is not enough over a few thousand
batches.

Crucially it sets the vocabulary floor, not the voice. Narration can still be
elegant and characters can still differ from one another; what it rules out is
archaic and bookish diction. `--register literary` gives the opposite
preference, `auto` restores the old per-book choice.

## Profiles

`--profile auto` asks the model once which kind of book this is, then records
the answer in the checkpoint so later runs do not ask again. One call, and it
picks between the two profiles below.

`--profile fiction` (default) — voice, characters, formality matrix, preserved
sentence rhythm.

`--profile nonfiction` — different bible entirely. No characters or formality
matrix; instead a **terminology lock** (a key term rendered three ways across
three chapters makes the reasoning untraceable), an **archaisms** table, and an
explicit **sentence-splitting policy**. The rolling summary becomes an argument
thread rather than a plot summary.

The archaisms table matters more than it sounds. Older texts use ordinary words
in senses that have since shifted — translate one by its modern meaning and the
argument silently corrupts while still reading fluently. That is the worst
possible failure mode, because nothing looks wrong.

Sentence policy is the other inversion. In fiction you preserve the author's
rhythm. In expository prose built from long periodic sentences, Hindi cannot
carry that subordination depth, so splitting is mandatory — while preserving
the logical connectives, the order claims arrive in, and the distinction
between what is asserted and what is merely conceded.

## Selecting what to translate

```bash
python booktrans.py book.epub --list          # spine index, headings, word counts
python booktrans.py book.epub --docs 16       # one document
python booktrans.py book.epub --docs 15-22    # a range
python booktrans.py book.epub --docs 'book1_' # filename regex
```

Always `--list` first. Front matter, editorial introductions, and indexes are
often larger than the chapters, and `--limit N` will happily spend your quota
on a publisher's copyright page. Indexes are skipped by default (`skip_classes`)
— translating alphabetized entries with source-language page references is
pure waste.

Document numbers are original spine positions, so checkpoints stay valid when
you change the selection.



Sequential by design — free-tier RPM caps throughput and parallel calls just
trigger 429s. ~6.5s inter-request gap, exponential backoff with jitter, 60s+
floor on quota errors. Every batch checkpoints to `work/<hash>/checkpoint.json`,
so a crash resumes instead of re-spending quota. Delete it to force a rerun.

A ~78k-word novel costs roughly 265 calls (or 180 with `--no-editor`) —
under a day's free-tier allowance, but prompt iteration is what will actually
exhaust it. Iterate with `--limit`.

## Output files

Each run writes to `booktrans_work/<book_name>/`:

| File | What it's for |
|---|---|
| `bible.json` | The binding decisions. A few KB. Read this first — it tells you whether a run is sound faster than the translation itself will, and it's the file to share when diagnosing a problem. |
| `report.json` | Run stats: API calls, retries, anchors restored, Devanagari share, blocks that came back untranslated, and every markup warning. |
| `checkpoint.json` | Resume state. Holds the full translated text, so it grows large. Delete to force a rerun. |

## Markup handling

Not all tags should reach the model. Three classes, handled differently:

**Zero-width anchors** (`<a id="page_110">`) are page targets with no text. The
model has no word to attach them to and simply drops them. They are extracted
before translation and reinserted afterward at the equivalent fractional
position, snapped to a word boundary. Measured placement error on a test
chapter: 0.203 → 0.201.

**Case-based typography** (drop caps, small caps) is unwrapped and discarded.
Devanagari has no letter case, so `T<small>HE</small>` has no correct Hindi
rendering. Demanding it back is asking for something that cannot exist.

**Semantic tags** (`<em>`, `<sup>`, `<strong>`, real links) must survive, and
only these are checked. On Book One of the test text this cut what the model
must preserve from 143 tags to 44 — the other 99 were noise that was driving
the retry rate.

## The bible is editable

`bible.json` is a control surface, not just an artifact. Edit it by hand and
rerun — your version wins over the cached one, and the corrections bind the
translator.

```
python booktrans.py book.epub --docs 16 ...   # writes bible.json, validates it
notepad booktrans_work\<book>\bible.json      # fix a rendering
del booktrans_work\<book>\checkpoint.json     # discard blocks made under the old bible
python booktrans.py book.epub --docs 16 ...   # corrections now binding
```

`--rebuild-bible` regenerates from scratch, discarding your edits. If the
bible changes while translated blocks are cached, the run warns you: those
blocks keep the old wording until the checkpoint is deleted.

## Changing the bible invalidates translations

Blocks translated under one bible are not consistent with another. When the
bible changes and cached blocks exist, those blocks are **discarded** by
default so terminology stays uniform. `--keep-blocks` retains them and records
a `MIXED OUTPUT` warning in the report.

This matters because a mixed result is invisible in the EPUB: both renderings
are fluent Hindi, and only a term-by-term comparison against two bibles reveals
that the same word is being translated two ways in one chapter.

## Sampling

The bible is built from a sample of the selected text. That sample must span
the **whole** selection: slicing head/middle/tail and truncating the join does
not work, because on a long book the head slice alone exceeds the character
budget and the later material is silently dropped. The bible then only ever
sees the opening chapters — so half the cast never appears in the formality
matrix, and the register is inferred from an unrepresentative stretch.

Sampling therefore walks evenly spaced windows across the selection, each with
an equal share of the budget. The console prints the span; anything well under
~80% means the bible is judging the book from a fraction of it.

## Relationships that change

A formality entry stores one form, but relationships move: terror becomes
affection, an intimate becomes a social superior, a child grows up. Where that
happens, one fixed form will either destroy the opening or destroy the ending.

Fiction bibles therefore record a `changes` array on any pair that shifts,
describing the narrative event that triggers it. Each translation batch is
told where it sits (`document 47 of 60 (~78% through), heading: CHAPTER XLVII`)
so the right form can be applied at the right point.

The validator flags any listed character with no formality entry, and any
one-directional pair — asymmetry is usually deliberate, so a missing reverse
direction means that half of the relationship is unconstrained.

## The bible evolves chapter by chapter

No sample can guarantee complete cast coverage, and relationships move as a
story proceeds. Both problems have the same solution: review each chapter
before translating it.

The review receives a compact digest of what is binding right now — characters
and the address form currently in force for each pair — plus the story so far
and the chapter text. It returns new characters, address-form changes with the
narrative event that caused them, and new glossary terms.

Changes are **time-scoped, not retroactive**. A shift reported at document 27
applies from document 27 onward; recomposing the bible at document 20 still
returns the earlier form. So chapters already translated remain consistent
with the decisions that produced them, while later chapters get the evolved
relationship. A change scoped forward is not a revision of the past.

```
doc 10: Pip->Joe = तुम [opening]
doc 27: Pip->Joe = आप  [document 27]   <- Pip comes into his fortune
doc 40: Pip->Joe = आप  [document 27]
```

Records are written to `bible_chapters.json`. `bible.json` stays the
hand-editable base and its hash never changes, so no translated block is
discarded by an update. Costs one call per chapter; disable with
`review_each_chapter=False`.

## Cast coverage

A bible can omit a major character entirely, and a validator that only checks
listed characters against the formality matrix will stay silent about it. So
the sample is scanned for recurring proper nouns and any that never appear in
the character list or formality matrix are reported.

The check counts only genuine entries. A name mentioned in passing inside
another character's description binds nothing, so it does not count as known.

## Output validation

Structural checks are not enough. An empty translation passes every one of
them: the block count matches, and the tag signature (nothing) matches. It is
then stored as "done", and reassembly - which skips falsy values - silently
leaves the **source text** in place. The block looks translated, the report
looks clean, and the EPUB contains English.

So each block's output is also checked for being empty, echoing the source, or
containing no target-script characters. A failure retries, then splits the
batch, then as a last resort keeps the source and records a warning. Empty
values are never stored, and the report counts them as untranslated.

## Bible validation

Every run checks the bible before spending calls against it, because each of
these otherwise surfaces as fluent, plausible, wrong output hundreds of blocks
later:

- **Empty `archaisms`** on a non-fiction text. This is the expensive one. A
  shifted-sense word does not look like a technical term — it looks like a word
  you already know — so translating it by its modern sense produces prose with
  nothing visibly broken. The prompt now requires an `archaism_scan` listing
  what was examined, so an empty result means "checked, found none" rather
  than "never looked."
- **Conflicting renderings** — one term mapped to two different words.
- **Collisions** — one target word doing duty for three or more distinct terms.
- **Hedged entries** — where the bible's own reasoning expresses doubt. The
  model flagging its uncertainty is signal; burying it in a 40-entry list wastes it.

## Validating quality

Benchmarks won't tell you if it's good. Your mother will. Translate one
chapter, print it, hand it over, and ask where it sounds like a machine. Feed
her objections back into the bible's `pitfalls` and `register` fields — that's
the tuning loop, and it's worth more than any model upgrade.

## Scope

Built for books with **no** existing Hindi edition — that's the real gap, and
it's large. Check first whether a professional translation exists; where one
does, it will beat this, and the translator deserves the sale. Output is for
personal use. Don't distribute translations of books still in copyright.


## Choosing a book in the workflows (pick_book.py)

GitHub cannot build a dropdown from repository contents - `type: choice`
needs a list hardcoded in the YAML - so both workflows take the book name as
free text and match it forgivingly. These all find the same file:

```
Crime and Punishment.hi.epub      crime
Crime and Punishment.hi           CRIME AND PUNISHMENT
output/Crime and Punishment.hi.epub
```

Extension, directory, case and word order are all optional, and a close
misspelling still resolves. When nothing matches it prints the list of books
actually present instead of reporting "nothing to convert".

## Hinglish (hinglish.py)

Transliteration, not translation: the Hindi words stay, only the script
changes. No API, no quota, seconds rather than days.

```
python hinglish.py --text "मैं अच्छा हूँ, आप कैसे हो?"
python hinglish.py book.hi.epub -o book.hinglish.epub
```

### Original spellings for names

Point it at the bible and character and place names keep their original
spelling - Joe Gargery rather than "jo gaarjri":

```
python hinglish.py book.hi.epub --work booktrans_work
```

`--work` finds the matching bible on its own; `--bible path` names one
explicitly. `--no-names` turns it off.

The bible records both names and translated concepts, so a substitution is
only made when the Hindi actually **sounds like** the English. मैगविच is a
transliteration of Magwitch, so the original spelling wins; उम्मीदें is a
translation of "Expectations", so it stays Hinglish. Without that gate,
English words leak into the prose.

### Schwa deletion

Devanagari writes an inherent "a" after every consonant that Hindi speakers
mostly do not pronounce. Transliterating literally gives "karataa" and
"daladl". The rule is to work right to left and never delete two adjacent
schwas - that single constraint took a test set from 50% to 93%. Common words
with conventional spellings live in `OVERRIDES` near the top of the file;
adding to that list beats any rule.

### Letters that are not written the way they sound

Four Devanagari letters need a mapping that follows written Hinglish rather
than phonetics:

| letter | phonetic | Hinglish | example |
|---|---|---|---|
| ड़ | retroflex flap | **d** | लड़का = ladka, पेड़ = ped |
| ढ़ | aspirated flap | **dh** | पढ़ना = padhna |
| ज्ञ | j + nya | **gy** | ज्ञान = gyan |
| ग़ | uvular g | **gh** | ग़ज़ल = ghazal |

The first two matter most: ड़ appears constantly, and mapping it to "r" turns
सड़क into "sarak" and पहाड़ into "pahaar". Nobody writes those.

### English loanwords

Hindi borrowed a lot of English and writes it in Devanagari. जुलाई is not a
Hindi word being transliterated - it is "July" that went into Devanagari and
should come back out as July, not "julaaee". The script carries a list of
about 120 of these (months, titles, everyday borrowings, units, currency) in
`LOANWORDS`. Nothing in the script can detect them, since Devanagari gives no
hint that a word is a loan, so the list is explicit and extensible:

```
python hinglish.py book.hi.epub --loanwords my_words.json
```

where the file is `{"जुलाई": "July", "पीटर्सबर्ग": "Petersburg"}`.

### Learning loanwords instead of listing them

The built-in list cannot cover every book, so `--learn` asks Gemini which
unknown words are Latin-script borrowings and remembers the answers:

```
python hinglish.py book.hi.epub --work booktrans_work --learn
```

It collects the frequent Devanagari words that no existing source explains,
sends them in batches with their rough phonetic reading, and keeps whatever
comes back as a Latin-script original. Persian, Arabic and Sanskrit
borrowings are explicitly excluded - किताब is an ordinary Hindi word, not a
foreign one needing its "original" spelling.

Results go to `booktrans_work/lexicon.json`, shared across books, so the
second book costs fewer calls than the first and the tenth costs almost none.
It reuses booktrans's client, so model cascading and quota handling apply.

Note this is a Hinglish concern only. The Hindi output needs no such fix:
जुलाई is correct Hindi.

### Capitalisation

Devanagari has no letter case, so a plain transliteration is entirely
lowercase. Latin script readers expect sentence case, so sentences are
capitalised, as is the name after a title. Titles double as ordinary nouns -
"Doctor ne kaha" - so a small function-word list stops "ne", "ka", "se" and
friends being capitalised after one.

### Words split by markup

A drop cap puts the first letter of a chapter in its own span, so जुलाई
arrives as "जु" plus "लाई" in two text nodes. Converting each node on its own
means no lookup ever sees the whole word, and the output reads "Ju Laaee"
instead of "July". Emphasis inside a word does the same thing.

So text is joined per container, transliterated once, then handed back word by
word to the node each word started in. Inline markup is preserved.

### What did not work

Matching against the English original to recover names the bible missed. The
consonant-skeleton comparison that makes names match also makes ordinary Hindi
collide with English: विचार became "Which", अपनी became "Upon", बल्कि became
"Bleak". Measured and removed. The bible mapping is exact and is enough.
