# WADs

Freedoom Phase 1 and Phase 2 ship inside the Doom server image, so nothing is
required here.

To play the original games, copy the WAD files you own into this folder:

| file        | scenario   |
|-------------|------------|
| `doom.wad`  | `doom`     |
| `doom2.wad` | `doom2`    |

The folder is mounted read-only into the server at `/wads`. WAD files are
git-ignored; never commit them.
