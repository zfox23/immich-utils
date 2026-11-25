# immich-utils
Utilities that connect to my local Immich installation.

## Tools

- [backup-other-immich-users](backup-other-immich-users/): back up other Immich users' photos and videos (shared with you via partner sharing) to a local folder, with a clickable HTML map of local files to Immich assets.
- [reset-person-faces](reset-person-faces/): remove selected people's face data from your photos in a date range and have Immich re-detect and re-recognize them, e.g. after tightening the recognition settings for look-alikes.

## Shared settings

Copy `config.example.json` to `config.json` in this folder and fill in your server URL and API key. Every tool reads it. Settings used by only one tool go in a `config.json` in that tool's folder, and that file can also override the shared settings. Both files are ignored by git.

### API key permissions

Create the key in Immich under **Account Settings → API Keys → New API Key**. A key with only the permissions you need is safer than one with `all`. To use one key for every tool, tick every row below. To use a key for just one tool, tick only that tool's column.

| Permission          | backup-other-immich-users | reset-person-faces | Why                                                                                     |
|---------------------|:-------------------------:|:------------------:|-----------------------------------------------------------------------------------------|
| `user.read`         | ✅                        | ✅                 | find out who you are                                                                     |
| `asset.read`        | ✅                        | ✅                 | read file names and dates; search photos by person, date and album                       |
| `partner.read`      | ✅                        |                    | list the people sharing with you                                                         |
| `timeline.read`     | ✅                        |                    | list each partner's assets                                                               |
| `asset.download`    | ✅                        |                    | download original files                                                                  |
| `adminUser.read`    | optional                  |                    | warn about users who aren't sharing with you yet                                         |
| `person.read`       |                           | ✅                 | list people                                                                              |
| `person.statistics` |                           | ✅                 | photo count per person, for sorting                                                      |
| `album.read`        |                           | ✅                 | list albums for the anchor-album picker                                                  |
| `face.read`         |                           | ✅                 | read the faces on each photo                                                             |
| `face.delete`       |                           | ✅                 | delete faces                                                                             |
| `job.create`        |                           | ✅                 | queue *Refresh faces*                                                                    |
| `queue.read`        |                           | optional (admin)   | wait for the face jobs to finish; without it you watch *Administration → Jobs* yourself |

The optional rows only add convenience; the tools work without them. Each tool's README explains its permissions in more detail. If you add a tool, add its column here too.

## Python environment

Tools that need third-party packages list them in their own `requirements.txt`. To install them on a system with an externally managed Python, use one virtual environment at the repo root:

```sh
python3 -m venv .venv
.venv/bin/pip install -r reset-person-faces/requirements.txt
```

## Shared code

[`shared/`](shared/) holds code the tools have in common: the Immich API client, the settings loader and a fake-server helper for tests. It uses only the Python standard library. Its own tests run with:

```sh
python -m unittest discover -s shared/tests -v
```
