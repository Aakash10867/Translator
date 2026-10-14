# Running booktrans on GitHub Actions

Translation of a full novel needs more daily quota than one sitting provides,
so it runs as a series of short scheduled jobs instead of one long one. Your
laptop stays closed.

## Why it is shaped this way

An Actions job is capped at **6 hours**, so sleeping through a quota reset
inside a job is impossible. It also is not necessary. A day's quota is roughly
`models x 20` calls — about 160 across eight models — which takes **30-40
minutes**, not six hours.

So each run works until the quota is spent, commits its checkpoint, and exits
with code **75** meaning "more to do". A daily cron picks it up after the
reset. A 185,000-word novel finishes in four or five runs, roughly **140
minutes of compute total** — comfortably inside the 2,000 free minutes a
private repo gets each month.

## Repository layout

```
books/                  EPUBs waiting to be translated
output/                 finished Hindi EPUBs (committed by the workflow)
booktrans_work/         checkpoints, bibles, reports (committed by the workflow)
booktrans.py
.github/workflows/translate.yml
```

## Setup

1. Create a **private** repository. See the note on copyright below.
2. Add `booktrans.py` at the root and `translate.yml` under
   `.github/workflows/`.
3. Put one or more EPUBs in `books/`.
4. Settings -> Secrets and variables -> Actions -> New repository secret,
   named `GEMINI_API_KEY`.
5. Settings -> Actions -> General -> Workflow permissions -> **Read and write**.
   Without this the workflow cannot commit progress.

## Running it

Actions -> Translate book -> Run workflow. Every input is optional:

| Input | Blank means |
|---|---|
| `book` | the next unfinished EPUB in `books/` |
| `profile` | `fiction` |
| `docs` | the whole book |
| `models` | the default chain |

Leave `book` empty and it works through `books/` in order, one at a time,
skipping any whose report says complete. Add more EPUBs whenever you like.

After the first manual run the cron takes over. Each run appends a summary to
the Actions page showing progress and the full `report.json`.

## Getting the result

The finished EPUB is committed to `output/` and also attached to the run as an
artifact (90-day retention). The `bible.json`, `bible_chapters.json`, and
`report.json` are attached too — worth reading, since they show what the
translator was actually bound to.

## Things worth knowing

**Scheduled workflows are disabled after 60 days of repository inactivity.**
The workflow's own commits count as activity, so this only bites once
everything is finished.

**Private repos get 2,000 free Actions minutes a month; public repos are
unlimited.** A whole novel costs about 140 minutes, so private is fine.

**Checkpoints get large.** A full novel's checkpoint holds the entire
translated text, several megabytes, recommitted each run. Fine for a handful
of books; if the history bloats, delete finished `booktrans_work/<slug>/`
directories once the EPUB is safely in `output/`.

**Copyright.** A public repo makes both the source EPUB and the translation
public. That is fine for public-domain texts, and not fine for books still in
copyright. Use a private repo, and keep translations of in-copyright books to
personal use.

**Resuming is automatic** because progress is checkpointed per batch and
committed after every run. A cancelled or failed job loses at most one batch.

## Local runs still work

The checkpoint format is identical, so you can pull the repo and continue on
your laptop, or push a locally started checkpoint and let Actions finish it.


## Why a run must start from the branch tip

A GitHub Actions run is pinned to the SHA it had when it was **queued**, not
when it starts. With a concurrency lock, a run can sit waiting while an
earlier one pushes - and then check out the tree from before that push. It
finds no checkpoint, translates from scratch, and commits its almost-empty
result over thousands of finished blocks.

Both workflows therefore reset to the branch tip immediately after checkout,
and `guard_progress.py` runs before every commit: if the branch copy of a
checkpoint has more translated blocks than this run produced, the branch copy
wins and nothing is lost.

If a stale run ever does slip through, the work is still in git history:

```
git checkout <commit-with-more-progress> -- booktrans_work/<Book>
git commit -m "restore progress" && git push
```
