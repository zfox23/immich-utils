# reset-person-faces

When Immich keeps mixing up two people who look alike, this script lets you give facial recognition a second try on part of your library, with stricter settings:

1. You pick one or more people, a date range and (optionally) an album of known-good photos to leave alone.
2. The script removes those people's machine-learning faces from your photos in that range.
3. It asks Immich to detect faces on exactly those photos again. Immich then recognizes each new face using your *current* facial recognition settings.
4. It waits for Immich to finish and prints a before/after table of who the faces were assigned to.

It needs Python 3.9+, the repo's `shared/` folder (so keep it inside the repo), and `questionary`. It was written against Immich 3.4.

```sh
pip install -r requirements.txt
```

## Why not just run "Face Detection → Missing"?

Because it would skip these photos. **Missing** only processes assets that have *never* had face detection run, and deleting faces doesn't change that. So the script uses Immich's per-photo **Refresh faces** job (the same as *⋯ → Refresh faces* on a photo), which re-detects faces on just the photos you changed.

It also deletes faces *permanently* (`force: true`). A normal face delete only hides the face, and face detection would then match the hidden face again instead of creating a fresh one.

## Before you run it

1. **Change the settings first.** Go to *Administration → Settings → Machine Learning → Facial Recognition*. For look-alikes, lower **Maximum recognition distance**. The Immich docs suggest staying between 0.3 and 0.7. If you lower it a lot, you may want to raise **Minimum recognized faces**.
   - Change only these thresholds. **Don't change the facial recognition model.** Faces from a new model can't be compared with the faces already in your library.
2. **Optional: make an anchor album.** Put a handful of correctly labeled photos of each look-alike into an album, for example "Face anchors". Pick it when the script asks, and those photos keep their faces as reference points. This matters most for someone who only appears in the date range. Without anchors, all their faces are removed and the re-detected faces may end up as a new unnamed person, which you'd merge back in the People view.
3. **Try `--dry-run` first.** It shows exactly which photos and faces would change.

## Settings and API key

The server URL and API key go in the **repo root** `config.json`, which every immich-utils script shares. Copy the root `config.example.json`. This script has no settings of its own. A `config.json` in this folder, environment variables (`IMMICH_URL`, `IMMICH_API_KEY`) or flags (`--server-url`, `--api-key`) override the shared file.

The API key needs these permissions:

| Permission          | Why                                                     |
|---------------------|---------------------------------------------------------|
| `user.read`         | find out who you are (only your own photos are changed) |
| `person.read`       | list people                                             |
| `person.statistics` | photo count per person, for sorting                     |
| `asset.read`        | search photos by person, date and album                 |
| `album.read`        | list albums for the anchor-album picker                 |
| `face.read`         | read the faces on each photo                            |
| `face.delete`       | delete faces                                            |
| `job.create`        | queue *Refresh faces*                                   |
| `queue.read`        | wait for the face jobs to finish (admin only; optional) |

Without `queue.read`, the script asks you to watch *Administration → Jobs* and press a key when the jobs are done. If you share one key between scripts, it needs the permissions of all of them.

## Running it

```sh
python reset_person_faces.py --dry-run
python reset_person_faces.py
```

You'll be asked for:

- **People**: named people, sorted by photo count. Type to filter, press space to select, and press enter to finish.
- **Start date** and **end date** (`YYYY-MM-DD`): both are included, using your computer's time zone. Leave the end date blank for today.
- **Anchor album** (optional).
- **Confirmation**: shows how many faces and photos will change, per person.

Flags: `--dry-run`, `--no-wait` (don't wait for Immich or print the report), `--insecure` (skip TLS verification), `--verbose`.

Exit codes: `0` success (or cancelled at the confirmation), `1` some faces couldn't be deleted or queued, `2` configuration or server error, `130` cancelled.

## What exactly changes

- Only **machine-learning** faces assigned to the selected people are deleted. Faces you drew yourself (*manual*) and faces from photo metadata (*EXIF*) stay.
  - A face you *reassigned* to someone else in the UI still counts as a machine-learning face, so it is deleted.
- Other people's faces on the same photos are untouched.
- Only **your own** photos (timeline and archive) are changed. Photos from partners or shared albums are skipped and counted, because only the owner can delete their faces.

## Things to know

- Re-recognition compares each new face with **every** face still assigned to a person, including photos outside the date range. If one look-alike's faces are mislabeled as the *other* person and you only select one of them, those mistakes stay and can still pull faces the wrong way. Selecting both look-alikes resets both.
- If every photo of a person is in the range, the summary warns that the person may be left with no faces.
- Faces left `(unassigned)` in the report didn't match anyone closely enough. Immich retries some of them in its nightly job, so check again the next day.
- "Taken" dates are compared in UTC, so a photo taken within a few hours of midnight in another time zone can land on the other side of the range boundary.
- There is no undo. The old face data is gone once deleted, but you can run the script again with different settings.

## Tests

```sh
python -m unittest discover -s tests -v
```

The tests run the real script against a small fake Immich server (`tests/fake_immich.py`) and don't need `questionary`.
