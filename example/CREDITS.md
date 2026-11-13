# Credits for the example project

Everything in `footage/`, `music/` and `review/` is derived from two public-domain sources. Nothing
here needs permission to copy, change or republish; the credits are a courtesy and a record.

## Footage

All twelve clips in `footage/` are excerpts of one film:

- **Title:** *Sunrise: A Song of Two Humans* (Fox Film Corporation, United States, released 23 September 1927)
- **Director:** F. W. Murnau. **Scenario:** Carl Mayer, after Hermann Sudermann. With George O'Brien and Janet Gaynor.
- **Source file:** Wikimedia Commons, [`File:Sunrise - A Song of Two Humans (1927).webm`](https://commons.wikimedia.org/wiki/File:Sunrise_-_A_Song_of_Two_Humans_(1927).webm)
  (the 1080p VP9 transcode offered on that page; retrieved 13 November 2026)
- **Licence:** Public domain. The file page's Licensing section carries the Commons templates
  `{{PD-US-expired}}` (public domain in the United States because its copyright term has expired;
  the film was published in 1927) and `{{PD-in}}` for cinema published in 1927. The page gives the
  publication date as 1927-09-23 and the scan's origin as the Masters of Cinema Blu-ray.
- **What was done:** each clip was cut from the film at the time shown, the sound was removed, and the
  picture was scaled from 1294×1080 to 864×720 and re-encoded as H.264 (CRF 22, 24 fps).

| File | Starts at (h:mm:ss in the film) | Length |
|---|---|---|
| `footage/moon-village.mp4` | 0:15:50 | 6.5 s |
| `footage/hands-reeds.mp4` | 0:24:07 | 6.5 s |
| `footage/her-boat.mp4` | 0:27:40 | 7 s |
| `footage/her-look.mp4` | 0:29:09 | 7 s |
| `footage/oars.mp4` | 0:29:48 | 7 s |
| `footage/his-eyes-boat.mp4` | 0:30:27 | 8 s |
| `footage/her-cafe.mp4` | 0:38:06 | 8 s |
| `footage/pew.mp4` | 0:42:09 | 5.5 s |
| `footage/his-face.mp4` | 0:42:38 | 8 s |
| `footage/lantern-water.mp4` | 1:24:28 | 7 s |
| `footage/sunrise.mp4` | 1:33:16 | 8 s |
| `footage/her-waking.mp4` | 1:33:34 | 9 s |

## Music

| File | |
|---|---|
| `music/gymnopedie-1.mp3` | The first 30 seconds of the recording below |

- **Work:** Erik Satie (1866–1925), *Gymnopédie No. 1*, "Lent et douloureux", published 1888. The composition is in the public domain.
- **Recording:** Robin Alciatore, piano, about 2008, released through [Musopen](https://musopen.org).
- **Source file:** Wikimedia Commons, [`File:Erik Satie - gymnopedies - la 1 ere. lent et douloureux.ogg`](https://commons.wikimedia.org/wiki/File:Erik_Satie_-_gymnopedies_-_la_1_ere._lent_et_douloureux.ogg) (retrieved 13 November 2026)
- **Licence:** Public domain. The file page is tagged `{{PD-author|Robin Alciatore}}` and its
  Permission field reads "Public domain music from musopen.com", quoting Musopen's FAQ: "The music
  on this site is given a public domain license, therefore, there are technically no restrictions
  on what you do with it." Musopen asks, as a courtesy, to be credited and that the recordings
  themselves not be sold; this file credits them here.
- **What was done:** the 1.9 s of silence at the start was trimmed, 30.5 s kept, a 4 s fade-out added, re-encoded as MP3 (160 kbit/s).

## The cut

`timelines/demo.json`, the notes in `feedback/` and `rules.json` were made for this example and are
covered by the repository's licence. `review/demo_v1.mp4` is that timeline rendered by
`cutvelvet publish example demo --scale 0.5` (then re-encoded smaller, see the README here); it
contains only the footage and music credited above.
