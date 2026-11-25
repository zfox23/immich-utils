# immich-utils
Utilities that connect to my local Immich installation.

## Tools

- [backup-other-immich-users](backup-other-immich-users/): back up other Immich users' photos and videos (shared with you via partner sharing) to a local folder, with a clickable HTML map of local files to Immich assets.
- [reset-person-faces](reset-person-faces/): remove selected people's face data from your photos in a date range and have Immich re-detect and re-recognize them, e.g. after tightening the recognition settings for look-alikes.

## Shared settings

Copy `config.example.json` to `config.json` in this folder and fill in your server URL and API key. Every tool reads it. Settings used by only one tool go in a `config.json` in that tool's folder, and that file can also override the shared settings. Both files are ignored by git.

If you use one API key for several tools, give it the permissions every one of them needs (each tool's README lists them).

## Shared code

[`shared/`](shared/) holds code the tools have in common: the Immich API client, the settings loader and a fake-server helper for tests. It uses only the Python standard library. Its own tests run with:

```sh
python -m unittest discover -s shared/tests -v
```
