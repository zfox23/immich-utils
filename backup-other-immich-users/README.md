# backup-other-immich-users

This script copies the original photos and videos that **other people** uploaded to your Immich server into a folder on this computer. From there, your normal backup (for example Backblaze) picks them up. Your own assets are never downloaded, because they're already in your backup.

```
<backup_root>/
  Jane Doe/
    2023/
      2023-05-06/
        IMG_0001.JPG
        IMG_0001_3f2a9c1d.JPG      <- a different photo with the same name on the same day
      2023-05-07/
        IMG_0002.HEIC
        IMG_0002.MOV               <- its Live Photo video
  backup-report.html               <- clickable map: local file <-> Immich asset
  .immich-backup/                  <- manifest, logs, past reports (don't edit)
```

It needs Python 3.9 or newer and nothing else. It runs on Windows, macOS and Linux, and was tested against Immich 3.4.2.

## How access works

Immich doesn't let an admin download other users' photos. Instead, each person adds **you** as a *partner*. The script then reads their library with your API key, the same way partner photos show up in your Immich timeline.

### 1. Each person you want to back up

In Immich: **Account Settings → Sharing → Partner sharing → Add partner →** choose your account.

That's all they need to do. They don't need to enable "show in timeline" on your side.

### 2. You: create an API key

In Immich: **Account Settings → API Keys → New API Key**. Give it these permissions:

| Permission       | Why                                                                     |
|------------------|-------------------------------------------------------------------------|
| `user.read`      | find out who you are (so your own assets are skipped)                   |
| `partner.read`   | list the people sharing with you                                        |
| `timeline.read`  | list each partner's assets                                              |
| `asset.read`     | read file names and dates                                               |
| `asset.download` | download the original files                                             |
| `adminUser.read` | *optional*: warns you about users who **aren't** sharing with you yet   |

A key with only these permissions is safer than one with `all`.

### 3. Configure

Copy `config.example.json` to `config.json` in this folder and fill it in. `config.json` is ignored by git.

```json
{
  "server_url": "https://immich.example.com",
  "api_key": "…",
  "backup_root": "D:/Immich Backups"
}
```

You can also use environment variables (`IMMICH_URL`, `IMMICH_API_KEY`, `IMMICH_BACKUP_ROOT`) or command-line flags (`--server-url`, `--api-key`, `--backup-root`). Flags override environment variables, and environment variables override the file.

### 4. Check access, then run

```sh
python backup_immich_users.py --probe     # checks the key's permissions and partner access; changes nothing
python backup_immich_users.py --dry-run   # shows what would be downloaded, moved or deleted
python backup_immich_users.py             # do it
```

Use `python3` on macOS. On Windows, `py` works too.

| Option              | What it does                                                                          |
|---------------------|---------------------------------------------------------------------------------------|
| `--user NAME`       | only back up this person (name, email or user id; can be repeated)                    |
| `--non-interactive` | never ask questions; files gone from the server are kept (good for scheduled runs)    |
| `--workers N`       | parallel downloads (default 4)                                                        |
| `--insecure`        | skip TLS certificate checks (self-signed certificates)                                |
| `--verbose`         | more detail in the console and log                                                    |

Exit codes: `0` success, `1` finished but some assets failed (run again to retry), `2` configuration or connection error, `130` interrupted.

## What each run does

1. Lists the people sharing with you, and warns about anyone who isn't (if the key has `adminUser.read`).
2. Lists every asset on their timelines, including every photo in a stack and each Live Photo's video clip.
3. Downloads new assets. Each download is written to a temporary `.part` file and checked against Immich's SHA-1 checksum before being moved into place, and the file's modified time is set to when it was captured. Ctrl+C is safe; the next run continues where it stopped.
4. Keeps the backup in step with the server:
   - **Date changed in Immich**: the file moves to its new date folder.
   - **Original replaced on the server**: the new version is downloaded, and the old one is kept as `NAME_<id>.replaced-YYYYMMDD.ext`.
   - **Local file missing or damaged**: it's downloaded again.
   - **Manifest lost**: files already in the right place are recognized by checksum and adopted, not downloaded again.
5. **Assets gone from the server** (deleted *or* trashed in Immich) are listed in the log as `REMOTE-DELETED user=… file=…`. When the run finishes, you're asked:
   ```
   What should happen to these local copies?  [k]eep all / [d]elete all / [r]eview each:
   ```
   Kept files aren't asked about again. If the asset comes back (for example it's restored from the trash), it's tracked again automatically.
6. Writes `backup-report.html` (see below) and a log in `.immich-backup/logs/`.

**Safety check:** if someone stops sharing with you, or their library couldn't be read completely, their files are **never** listed as deleted. You get a warning and their files are left alone.

## The mapping report

Open `backup-report.html` in any browser. Each row links a local file to its Immich asset:

- **Local file**: opens the backed-up file. The links are relative, so they still work if you move the whole backup folder.
- **Open in Immich**: opens the asset on your server (`/photos/<asset id>`).
- The Immich **asset id** and **SHA-1 checksum** of every file.
- A **status** for each asset: new, existing, moved, updated, restored, adopted, failed, gone from server (kept/removed), or not checked this run.
- A text filter and filters for user and status.

The last 10 reports are kept in `.immich-backup/reports/`. The same mapping, in JSON, is in `.immich-backup/manifest.json`.

Browsers may download HEIC and some video files when you click them instead of displaying them. That's normal.

## Limitations

- **Archived and Locked Folder assets aren't backed up.** Immich doesn't show these to partners. If someone wants an archived photo backed up, they need to unarchive it.
- **Trashed assets aren't backed up**, and an asset someone trashes shows up as "gone from server".
- **Only original files are backed up.** Immich edits, albums, descriptions, favorites and people/face tags are not saved.
- **External-library assets** can't be checked by checksum, because Immich stores a checksum of the file *path* for them. They're still downloaded. If the manifest is lost, they're downloaded again, not adopted.
- Folder names come from each person's display name and are fixed the first time that person is backed up, so renaming someone in Immich doesn't split their files across folders. If two people have the same name, the second gets `Name_<id>`.

## Tests

```sh
python -m unittest discover -s tests -v
```

The end-to-end tests run the real script against a small fake Immich server (`tests/fake_immich.py`) that follows the 3.4 partner-access rules.
